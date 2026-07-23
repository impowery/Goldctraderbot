#!/usr/bin/env python3
"""Telegram alerts для GOLD-CTRADER бота.

Использование:
    python3 ctrader_alerts.py                          # режим watchdog (разовый чек)
    python3 ctrader_alerts.py --watch                  # постоянный мониторинг (cron)
    python3 ctrader_alerts.py --test                   # тестовое сообщение
    python3 ctrader_alerts.py --log /path/trades.jsonl # свой путь

Возможности:
1. Trade alerts — уведомление при новой закрытой сделке (entry/exit/PnL/reason)
2. Status alerts — если бот не делал сделки > 4 часов
3. Daily summary — в 20:00 MSK отправляет сводку дня
4. Anomaly alerts — 3 consecutive losses / large loss / balance low
"""
import json
import os
import sys
import argparse
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import OrderedDict

# === Telegram config ===
BOT_TOKEN = "8664275234:AAHUHIdruK4FWjIioRwtqU1PGQeceyeBk-g"
CHAT_ID = os.environ.get("TG_CHAT_ID", "")  # установим после /start от пользователя

# === Bot state — support multiple trade logs ===
TRADE_LOGS = [
    os.environ.get("TRADE_LOG_GOLD", "/root/bots/trades_gold_ctrader.jsonl"),
    os.environ.get("TRADE_LOG_BTC", "/root/bots/trades_btc_ctrader.jsonl"),
]
# Backwards compat: if TRADE_LOG_PATH set, use only that
_single_log = os.environ.get("TRADE_LOG_PATH", "")
if _single_log:
    TRADE_LOGS = [_single_log]
STATE_FILE = os.environ.get("ALERT_STATE", "alert_state.json")

# MSK timezone
MSK = timezone(timedelta(hours=3))


def tg_send(text: str, parse_mode: str = "HTML") -> bool:
    """Send Telegram message with HTML formatting by default."""
    if not CHAT_ID:
        print(f"[ALERT] No CHAT_ID set, can't send: {text[:100]}")
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": text, "parse_mode": parse_mode}
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            result = json.loads(r.read())
        if not result.get("ok"):
            print(f"[ALERT] TG error: {result}")
            return False
        return True
    except Exception as e:
        print(f"[ALERT] TG send failed: {e}")
        return False


def _esc(s) -> str:
    """Escape HTML special characters."""
    if s is None:
        return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt_money(v: float, sign: bool = True) -> str:
    """Format money with thousand separators and sign. E.g. +$1 234.56 / -$50.00"""
    if v is None:
        return "$0.00"
    sign_str = "+" if (sign and v >= 0) else ("-" if v < 0 else "")
    abs_v = abs(v)
    # Thousand separator: space (French style, aligns nicely in monospace)
    if abs_v >= 1000:
        s = f"{abs_v:,.2f}".replace(",", " ")
    else:
        s = f"{abs_v:.2f}"
    return f"{sign_str}${s}"


def _fmt_price(p: float) -> str:
    """Format price with thousand separators. E.g. $4 022.29"""
    if p is None:
        return "$0.00"
    abs_p = abs(p)
    if abs_p >= 1000:
        s = f"{abs_p:,.2f}".replace(",", " ")
    else:
        s = f"{abs_p:.2f}"
    return f"${s}"


def load_trades(path: str) -> list:
    """Load trades from a single file."""
    p = Path(path)
    if not p.exists():
        return []
    trades = []
    with open(p) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                trades.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return trades


def load_all_trades() -> list:
    """Load trades from ALL configured trade logs (gold + btc)."""
    all_trades = []
    for log_path in TRADE_LOGS:
        trades = load_trades(log_path)
        # Tag each trade with bot name for identification
        bot_name = "GOLD" if "gold" in log_path else "BTC" if "btc" in log_path else "?"
        for t in trades:
            t["_bot"] = bot_name
        all_trades.extend(trades)
    # Sort by timestamp
    all_trades.sort(key=lambda t: t.get("ts", ""))
    return all_trades


def parse_ts(ts):
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def load_alert_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"last_trade_ts": None, "last_alert_day": None, "consec_losses": 0}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"last_trade_ts": None, "last_alert_day": None, "consec_losses": 0}


def save_alert_state(state: dict):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"[ALERT] State save failed: {e}")


def fmt_trade_alert(trade: dict, trade_num: int, total_pnl: float = None) -> str:
    """Format trade alert message with HTML + monospace blocks."""
    pnl = trade.get("pnl", 0)
    pnl_emoji = "🟢" if pnl >= 0 else "🔴"
    direction = trade.get("type", "?")
    dir_arrow = "▲" if direction in ("BUY", "LONG") else "▼"
    dir_word = "LONG" if direction in ("BUY", "LONG") else "SHORT"
    reason = _esc(trade.get("reason", "?"))
    entries = trade.get("entries", 1)
    entry_p = trade.get("entry_price", 0)
    exit_p = trade.get("exit_price", 0)
    bot_name = _esc(trade.get("_bot", "?"))
    max_entries = 3  # hardcoded for display, matches MAX_ENTRIES in .env

    dt = parse_ts(trade.get("ts"))
    time_str = dt.strftime("%H:%M MSK") if dt else "?"

    pnl_str = _fmt_money(pnl)
    entry_str = _fmt_price(entry_p)
    exit_str = _fmt_price(exit_p)

    # Handle OPEN positions differently from CLOSED trades
    # (backfill_trades.py logs open positions with reason=OPEN and entry=exit price)
    is_open = reason.upper() in ("OPEN", "OPENED", "EXT_OPEN")
    if is_open:
        header = f"<b>🆕 {bot_name} · Trade #{trade_num} OPENED</b>"
        body = (
            f"<pre>Direction   {dir_arrow} {dir_word}\n"
            f"Entries     {entries} / {max_entries}\n"
            f"Entry       {entry_str}\n"
            f"SL/TP       set by bot\n"
            f"Time        {time_str}</pre>"
        )
        msg = header + "\n\n" + body
        # Don't show PnL for open positions (it's just floating)
        return msg

    header = f"<b>{pnl_emoji} {bot_name} · Trade #{trade_num} closed</b>"
    body = (
        f"<pre>Direction   {dir_arrow} {dir_word}\n"
        f"Reason      {reason}\n"
        f"Entries     {entries} / {max_entries}\n"
        f"Entry       {entry_str}\n"
        f"Exit        {exit_str}\n"
        f"PnL         {pnl_str}\n"
        f"Time        {time_str}</pre>"
    )

    msg = header + "\n\n" + body
    if total_pnl is not None:
        msg += f"\n\n<b>💰 Total PnL:   {_fmt_money(total_pnl)}</b>"
    return msg


def fmt_daily_summary(trades_today: list, balance: float = None, total_pnl_all: float = None) -> str:
    """Format daily summary message with HTML + monospace blocks."""
    today_str = datetime.now(MSK).strftime("%Y-%m-%d")
    header = f"<b>📊 DAILY SUMMARY · {today_str}</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━\n"

    if not trades_today:
        msg = header + "\n<i>No trades today. Bot is waiting for signals (ADX filter active).</i>"
        if balance is not None:
            msg += f"\n\n<b>💵 Balance       {_fmt_money(balance, sign=False)}</b>"
        if total_pnl_all is not None:
            msg += f"\n<b>📈 Total PnL     {_fmt_money(total_pnl_all)}</b>"
        return msg

    total = sum(t["pnl"] for t in trades_today)
    wins = [t for t in trades_today if t["pnl"] > 0]
    losses = [t for t in trades_today if t["pnl"] < 0]
    wr = len(wins) / len(trades_today) * 100 if trades_today else 0

    # Profit factor
    gross_w = sum(t["pnl"] for t in wins)
    gross_l = abs(sum(t["pnl"] for t in losses))
    pf = gross_w / gross_l if gross_l > 0 else float('inf')
    pf_str = f"{pf:.2f}" if pf != float('inf') else "∞"

    progress_pct = (total / 600) * 100  # progress to $600 target
    best = max(t["pnl"] for t in trades_today)
    worst = min(t["pnl"] for t in trades_today)
    avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0

    trades_line = f"{len(trades_today)}  ({len(wins)}W / {len(losses)}L)"
    body = (
        f"<pre>Trades           {trades_line}\n"
        f"Win rate         {wr:.0f}%\n"
        f"Profit Factor    {pf_str}\n"
        f"Best             {_fmt_money(best)}\n"
        f"Worst            {_fmt_money(worst)}\n"
        f"Avg win          {_fmt_money(avg_win)}\n"
        f"Avg loss         {_fmt_money(avg_loss)}</pre>"
    )

    msg = header + "\n" + body
    msg += f"\n\n<b>💰 Daily PnL     {_fmt_money(total)}</b>"
    msg += f"\n<b>🎯 Target $600   {progress_pct:.1f}%</b>"
    if balance is not None:
        msg += f"\n<b>💵 Balance       {_fmt_money(balance, sign=False)}</b>"
    if total_pnl_all is not None:
        msg += f"\n<b>📈 Total PnL     {_fmt_money(total_pnl_all)}</b>"
    return msg


def fmt_anomaly_alert(anomaly_type: str, **kw) -> str:
    """Format anomaly alert with HTML + monospace blocks."""
    sep = "━" * 27
    if anomaly_type == "consec_losses":
        count = kw['count']
        total_loss = kw['total_loss']
        return (
            f"<b>⚠️ ANOMALY · {count} CONSECUTIVE LOSSES</b>\n{sep}\n\n"
            f"<pre>Total loss    -${total_loss:,.2f}".replace(",", " ") + "\n"
            f"Bot status    Still running</pre>\n\n"
            f"<i>Check strategy — bot may need cooldown tuning.</i>"
        )
    elif anomaly_type == "large_loss":
        loss = kw['loss']
        pct = kw['pct']
        reason = _esc(kw.get('reason', '?'))
        return (
            f"<b>⚠️ ANOMALY · LARGE LOSS</b>\n{sep}\n\n"
            f"<pre>Loss          -${loss:,.2f}".replace(",", " ") + "\n"
            f"% of balance  {pct:.1f}%\n"
            f"Reason        {reason}</pre>"
        )
    elif anomaly_type == "no_trades":
        hours = kw.get("hours", 4)
        last_time = _esc(kw.get('last_trade_time', '?'))
        return (
            f"<b>⚠️ ANOMALY · NO TRADES {hours}h+</b>\n{sep}\n\n"
            f"<pre>Last trade    {last_time}\n"
            f"Hours idle    {hours}h\n"
            f"Bot status    ADX filter / MCP check</pre>\n\n"
            f"<i>Bot may be stuck — check MCP connection.</i>"
        )
    elif anomaly_type == "stale_state":
        hours = kw.get("hours", 2)
        return (
            f"<b>⚠️ ANOMALY · STALE STATE</b>\n{sep}\n\n"
            f"<pre>state.json not updated for {hours}h\n"
            f"Bot may be frozen.</pre>"
        )
    return f"<b>⚠️ ANOMALY</b>\n{sep}\n\n<pre>{_esc(anomaly_type)}</pre>"


def check_anomalies(trades: list, state: dict) -> list:
    """Return list of anomalies detected."""
    anomalies = []
    if not trades:
        return anomalies

    # 3 consecutive losses
    recent = trades[-3:] if len(trades) >= 3 else []
    if len(recent) == 3 and all(t["pnl"] < 0 for t in recent):
        total_loss = abs(sum(t["pnl"] for t in recent))
        anomalies.append({
            "type": "consec_losses",
            "count": 3,
            "total_loss": total_loss,
        })

    # Large single loss (> $50 on demo, > $150 on challenge)
    last = trades[-1]
    large_threshold = 50  # demo threshold
    if last["pnl"] < -large_threshold:
        anomalies.append({
            "type": "large_loss",
            "loss": abs(last["pnl"]),
            "pct": abs(last["pnl"]) / 5000 * 100,  # % of $5K
            "reason": last.get("reason", "?"),
        })

    return anomalies


def watch_once(args):
    """One-shot check: send alerts for new trades + anomalies + daily summary."""
    trades = load_all_trades()
    state = load_alert_state()

    # Check for new trades
    new_alerts_sent = 0
    last_known_ts = state.get("last_trade_ts")

    # SAFETY: if state was reset (last_trade_ts is None), do NOT re-send all historical trades.
    # Instead, just set last_trade_ts to the latest trade and skip sending alerts for old trades.
    # This prevents spamming the user with 100+ old trade alerts after a state reset.
    if last_known_ts is None and trades:
        latest_ts = parse_ts(trades[-1].get("ts"))
        if latest_ts:
            state["last_trade_ts"] = latest_ts.isoformat()
            save_alert_state(state)
            print(f"[ALERT] State was reset — set last_trade_ts to {latest_ts.isoformat()} (skipped {len(trades)} old trades)")
            return 0

    new_trades = []
    for t in trades:
        t_ts = parse_ts(t.get("ts"))
        if t_ts is None:
            continue
        if last_known_ts is None or t_ts.isoformat() > last_known_ts:
            new_trades.append((len(trades) - trades[::-1].index(t), t))  # (trade_num, trade)

    # Compute total PnL across all trades (for footer in trade alerts)
    total_pnl_all = sum(t.get("pnl", 0) for t in trades)

    # Send alert for each new trade
    for trade_num, trade in new_trades:
        msg = fmt_trade_alert(trade, trade_num, total_pnl=total_pnl_all)
        if tg_send(msg):
            new_alerts_sent += 1

    # Update state with latest trade ts
    if trades:
        latest_ts = parse_ts(trades[-1].get("ts"))
        if latest_ts:
            state["last_trade_ts"] = latest_ts.isoformat()

    # Check anomalies
    # IMPORTANT: only send anomalies for NEW trades (when last_trade_ts changed).
    # Without this, large_loss would be sent every cron run (15 min) for the same losing trade.
    is_new_trade = bool(new_trades) or last_known_ts is None
    anomalies = check_anomalies(trades, state)
    for anomaly in anomalies:
        if anomaly["type"] == "consec_losses":
            # Only alert once per streak (track in state)
            if state.get("consec_losses_alerted") != anomaly["count"]:
                msg = fmt_anomaly_alert("consec_losses", count=anomaly["count"], total_loss=anomaly["total_loss"])
                tg_send(msg)
                state["consec_losses_alerted"] = anomaly["count"]
        elif anomaly["type"] == "large_loss":
            # Dedup: only send if this is a NEW trade (not the same one we already alerted)
            # Track the ts of the last trade we sent a large_loss alert for.
            last_large_loss_ts = state.get("last_large_loss_ts")
            current_last_ts = trades[-1].get("ts") if trades else None
            if current_last_ts and current_last_ts != last_large_loss_ts and is_new_trade:
                msg = fmt_anomaly_alert("large_loss", loss=anomaly["loss"], pct=anomaly["pct"], reason=anomaly["reason"])
                tg_send(msg)
                state["last_large_loss_ts"] = current_last_ts

    # Reset consec_losses tracking if streak broken
    if trades and trades[-1]["pnl"] >= 0:
        state["consec_losses_alerted"] = 0

    # Check no-trades-too-long (only if we have history)
    if trades and last_known_ts:
        latest = parse_ts(trades[-1].get("ts"))
        if latest:
            hours_since = (datetime.now(timezone.utc) - latest).total_seconds() / 3600
            if hours_since > 4:
                msg = fmt_anomaly_alert("no_trades", hours=int(hours_since),
                                        last_trade_time=latest.astimezone(MSK).strftime("%H:%M MSK"))
                tg_send(msg)

    # Daily summary — send once per day, after 23:50 MSK (before market close at 23:57/23:59)
    # Fires on first cron run at/after 23:50 MSK (cron */15 catches 23:45, next at 00:00)
    # Plus separate cron entry at 55 20 * * * (23:55 MSK) as backup
    now_msk = datetime.now(MSK)
    today_str = now_msk.strftime("%Y-%m-%d")
    if state.get("last_alert_day") != today_str and now_msk.hour == 23 and now_msk.minute >= 50:
        # Get today's trades
        today_trades = []
        for t in trades:
            dt = parse_ts(t.get("ts"))
            if dt and dt.astimezone(MSK).strftime("%Y-%m-%d") == today_str:
                today_trades.append(t)
        # Get current balance from VPS state
        balance = None
        try:
            import urllib.request
            req = urllib.request.Request("http://127.0.0.1:8089/api/state")
            with urllib.request.urlopen(req, timeout=5) as r:
                state_data = json.loads(r.read())
                balance = state_data.get("daily_start_balance")
                total_pnl_all = state_data.get("total_pnl", 0)
        except Exception:
            total_pnl_all = sum(t.get("pnl", 0) for t in trades)
        msg = fmt_daily_summary(today_trades, balance, total_pnl_all)
        if tg_send(msg):
            state["last_alert_day"] = today_str
            print(f"[ALERT] Daily summary sent for {today_str}")

    save_alert_state(state)
    return new_alerts_sent


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--log", default="", help="Path to single trades log (default: all bots)")
    p.add_argument("--watch", action="store_true", help="Continuous monitoring (not implemented yet)")
    p.add_argument("--test", action="store_true", help="Send test message")
    p.add_argument("--chat-id", help="Override CHAT_ID")
    args = p.parse_args()

    global CHAT_ID
    if args.chat_id:
        CHAT_ID = args.chat_id

    if args.test:
        sep = "━" * 27
        msg = (
            f"<b>🤖 AITradingAlertPNLBot · TEST</b>\n{sep}\n\n"
            f"<pre>Bot           @AITradingAlertPNLBot\n"
            f"Chat ID       {_esc(CHAT_ID or 'NOT SET')}\n"
            f"Time          {datetime.now(MSK).strftime('%Y-%m-%d %H:%M:%S MSK')}</pre>\n\n"
            f"<b>✅ Alerts are working.</b> Will send:\n"
            f"  • Trade notifications (entry/exit/PnL)\n"
            f"  • Daily summary at 23:50 MSK\n"
            f"  • Anomaly alerts (3 consec losses, large loss, no trades 4h+)\n\n"
            f"<i>Format upgraded: HTML + monospace blocks for alignment.</i>"
        )
        ok = tg_send(msg)
        print("Test message sent" if ok else "Failed to send test message")
        sys.exit(0 if ok else 1)

    if args.watch:
        print("Continuous monitoring not implemented. Use cron: */15 * * * * python3 ctrader_alerts.py")
        sys.exit(0)

    # Default: one-shot check
    sent = watch_once(args)
    print(f"Alerts sent: {sent}")


if __name__ == "__main__":
    main()
