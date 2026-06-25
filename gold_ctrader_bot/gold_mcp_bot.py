"""GoldBot for cTrader MCP — EMA+ADX+ATR strategy with scaled entries (port of VPS gold_bot sizing).

VPS sync: pushes trades + state to VPS HTTP endpoint for dashboard integration.
Configure via .env: VPS_SYNC_URL, VPS_AUTH_TOKEN, VPS_SYNC_ENABLED.
"""

import asyncio
import json
import time
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

import httpx
from dotenv import load_dotenv

from strategy import should_enter, calc_ema, calc_atr, calc_adx

load_dotenv()

MCP_URL = os.getenv("MCP_URL", "http://127.0.0.1:9876/mcp/")
SYMBOL = os.getenv("SYMBOL_NAME", "XAUUSD")
MIN_INTERVAL_MINUTES = int(os.getenv("MIN_INTERVAL_MINUTES", "60"))
MAX_DAILY_LOSS_PERCENT = float(os.getenv("MAX_LOSS_PERCENT", "3.0"))
TIMEFRAME = os.getenv("TIMEFRAME", "m5")
CANDLE_COUNT = int(os.getenv("CANDLE_COUNT", "100"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
VOLUME_TYPE = "lots"
RECONNECT_DELAY = int(os.getenv("RECONNECT_DELAY", "5"))
MAX_RECONNECT_DELAY = int(os.getenv("MAX_RECONNECT_DELAY", "300"))

# Scale-in params (port from gold_bot.py v10.2 sizing concept)
ENTRY_VOLUMES = [float(x) for x in os.getenv("ENTRY_VOLUMES", "0.01,0.01,0.01").split(",")]
MAX_ENTRIES = int(os.getenv("MAX_ENTRIES", "3"))
SL_ATR_MULT = float(os.getenv("SL_ATR_MULT", "4.0"))
TP1_ATR_MULT = float(os.getenv("TP1_ATR_MULT", "0.8"))
TP2_ATR_MULT = float(os.getenv("TP2_ATR_MULT", "1.5"))
TRAIL_ACTIVATE_PCT = float(os.getenv("TRAIL_ACTIVATE_PCT", "0.3"))
TIME_EXIT_HOURS = float(os.getenv("TIME_EXIT_HOURS", "2"))

TRADE_LOG_PATH = os.getenv("TRADE_LOG_PATH", "trades_gold_ctrader.jsonl")
STATE_FILE_PATH = os.getenv("STATE_FILE_PATH", "state.json")
TARGET_PROFIT = float(os.getenv("TARGET_PROFIT", "0"))

# VPS sync config
VPS_SYNC_URL = os.getenv("VPS_SYNC_URL", "").rstrip("/")
VPS_AUTH_TOKEN = os.getenv("VPS_AUTH_TOKEN", "")
VPS_SYNC_ENABLED = os.getenv("VPS_SYNC_ENABLED", "false").lower() == "true"

import numpy as np


def iso_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class GoldMCPBot:
    def __init__(self):
        self.client = httpx.AsyncClient()
        self.session_id = None
        self.tool_names = {}
        self.lot_size = 100
        self.pip_size = 0.01
        self.digits = 2
        self.volume_step = 1

        # Entry state (port from gold_bot.py)
        self.entries = []        # list of {price, volume_lots}
        self.is_short = False
        self.entry_time = 0
        self.extreme_price = 0.0
        self.closed_half = False
        self.entry_atr = 0.0
        self.entry_ema = 0.0

        # Risk state
        self.last_entry_minute = -1
        self.daily_start_balance = None
        self.daily_loss_hit = False
        self.consecutive_tp = 0
        self.tp_cooldown_until = 0
        self.daily_pnl = 0.0
        self.daily_pnl_day = ""

        # Candle cache
        self.close_prices = []
        self.high_prices = []
        self.low_prices = []
        self.atr = 0.0
        self.adx = 0.0
        self.ema = 0.0
        self.last_candle_fetch = 0

        # Challenge tracking
        self.total_pnl = 0.0
        self.trading_days = set()
        self.target_hit = False

        # VPS sync state
        self.last_state_push = 0
        self.vps_sync_failures = 0

    # ─── MCP helpers ────────────────────────────────────────────────

    async def mcp_request(self, method, params=None):
        req_id = int(time.time() * 1000) % 100000
        body = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params:
            body["params"] = params
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        resp = await self.client.post(MCP_URL, json=body, headers=headers)
        resp.raise_for_status()
        ct = resp.headers.get("content-type", "")
        if "application/json" in ct:
            return resp.json()
        return None

    async def call(self, tool_name, args=None):
        resp = await self.mcp_request("tools/call", {"name": tool_name, "arguments": args or {}})
        if resp and "error" in resp:
            print(f"[GoldMCP] Error {tool_name}: {resp['error']}")
            return None
        if resp:
            return resp.get("result", {}).get("content")
        return None

    def _parse_text(self, result):
        if result and isinstance(result, list):
            for item in result:
                if item.get("type") == "text":
                    return json.loads(item.get("text", "{}"))
        return result

    def _write_trade(self, reason, entry_price, exit_price, pnl, entries_used):
        entry = {"ts": iso_now(), "type": "SHORT" if self.is_short else "LONG",
                 "entry_price": round(entry_price, self.digits),
                 "exit_price": round(exit_price, self.digits),
                 "pnl": round(pnl, 2), "reason": reason,
                 "entries": entries_used, "version": "mcp-v1"}
        try:
            with open(TRADE_LOG_PATH, "a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[GoldMCP] Trade log write failed: {e}")
        # Push to VPS (fire-and-forget, sync to avoid blocking on Windows)
        self._push_trade_to_vps(entry)

    # ─── VPS sync ───────────────────────────────────────────────────

    def _push_trade_to_vps(self, trade_data: dict):
        """Push closed trade to VPS HTTP endpoint. Fire-and-forget."""
        if not VPS_SYNC_ENABLED or not VPS_SYNC_URL:
            return
        try:
            req = urllib.request.Request(
                f"{VPS_SYNC_URL}/api/trade",
                data=json.dumps(trade_data).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {VPS_AUTH_TOKEN}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                result = json.loads(r.read())
            if result.get("ok"):
                print(f"[GoldMCP] VPS sync: trade pushed (pnl={trade_data.get('pnl'):+.2f})")
                self.vps_sync_failures = 0
            else:
                print(f"[GoldMCP] VPS sync failed: {result}")
                self.vps_sync_failures += 1
        except urllib.error.HTTPError as e:
            print(f"[GoldMCP] VPS trade push HTTP {e.code}: {e.reason}")
            self.vps_sync_failures += 1
        except Exception as e:
            print(f"[GoldMCP] VPS trade push error: {e}")
            self.vps_sync_failures += 1

    def _push_state_to_vps(self):
        """Push current bot state to VPS HTTP endpoint. Throttled to 1/min."""
        if not VPS_SYNC_ENABLED or not VPS_SYNC_URL:
            return
        now_ms = int(time.time() * 1000)
        if now_ms - self.last_state_push < 60000:  # 1 min throttle
            return
        self.last_state_push = now_ms
        state = {
            "entries": self.entries,
            "is_short": self.is_short,
            "entry_time": self.entry_time,
            "extreme_price": self.extreme_price,
            "closed_half": self.closed_half,
            "entry_atr": self.entry_atr,
            "entry_ema": self.entry_ema,
            "daily_start_balance": self.daily_start_balance,
            "daily_loss_hit": self.daily_loss_hit,
            "consecutive_tp": self.consecutive_tp,
            "tp_cooldown_until": self.tp_cooldown_until,
            "daily_pnl": self.daily_pnl,
            "daily_pnl_day": self.daily_pnl_day,
            "last_entry_minute": self.last_entry_minute,
            "total_pnl": self.total_pnl,
            "trading_days": list(self.trading_days),
            "target_hit": self.target_hit,
            "current_price": self.close_prices[-1] if self.close_prices else 0,
            "ema": self.ema,
            "atr": self.atr,
            "adx": self.adx,
            "timestamp": now_ms,
        }
        try:
            req = urllib.request.Request(
                f"{VPS_SYNC_URL}/api/state",
                data=json.dumps(state).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {VPS_AUTH_TOKEN}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                result = json.loads(r.read())
            if not result.get("ok"):
                print(f"[GoldMCP] VPS state push failed: {result}")
                self.vps_sync_failures += 1
            else:
                self.vps_sync_failures = 0
        except urllib.error.HTTPError as e:
            print(f"[GoldMCP] VPS state push HTTP {e.code}: {e.reason}")
            self.vps_sync_failures += 1
        except Exception as e:
            # Silent on connection errors (VPS may be down) — don't spam log
            self.vps_sync_failures += 1

    def _save_state(self):
        state = {"entries": self.entries, "is_short": self.is_short,
                 "entry_time": self.entry_time, "extreme_price": self.extreme_price,
                 "closed_half": self.closed_half, "entry_atr": self.entry_atr,
                 "entry_ema": self.entry_ema, "daily_start_balance": self.daily_start_balance,
                 "daily_loss_hit": self.daily_loss_hit, "consecutive_tp": self.consecutive_tp,
                 "tp_cooldown_until": self.tp_cooldown_until, "daily_pnl": self.daily_pnl,
                 "daily_pnl_day": self.daily_pnl_day, "last_entry_minute": self.last_entry_minute,
                 "total_pnl": self.total_pnl, "trading_days": list(self.trading_days),
                 "target_hit": self.target_hit}
        tmp = STATE_FILE_PATH + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, STATE_FILE_PATH)
        except Exception as e:
            print(f"[GoldMCP] State save failed: {e}")

    def _load_state(self):
        if not os.path.exists(STATE_FILE_PATH):
            return
        try:
            with open(STATE_FILE_PATH) as f:
                state = json.load(f)
            self.entries = state.get("entries", [])
            self.is_short = state.get("is_short", False)
            self.entry_time = state.get("entry_time", 0)
            self.extreme_price = state.get("extreme_price", 0.0)
            self.closed_half = state.get("closed_half", False)
            self.entry_atr = state.get("entry_atr", 0.0)
            self.entry_ema = state.get("entry_ema", 0.0)
            self.daily_start_balance = state.get("daily_start_balance")
            self.daily_loss_hit = state.get("daily_loss_hit", False)
            self.consecutive_tp = state.get("consecutive_tp", 0)
            self.tp_cooldown_until = state.get("tp_cooldown_until", 0)
            self.daily_pnl = state.get("daily_pnl", 0.0)
            self.daily_pnl_day = state.get("daily_pnl_day", "")
            self.last_entry_minute = state.get("last_entry_minute", -1)
            self.total_pnl = state.get("total_pnl", 0.0)
            self.trading_days = set(state.get("trading_days", []))
            self.target_hit = state.get("target_hit", False)
            if self.entries:
                print(f"[GoldMCP] State restored: {len(self.entries)} entries, "
                      f"{'SHORT' if self.is_short else 'LONG'} @ {self.avg_price}")
        except Exception as e:
            print(f"[GoldMCP] State load failed: {e}")

    # ─── Connection ─────────────────────────────────────────────────

    async def connect(self):
        print(f"[GoldMCP] Connecting to {MCP_URL}...")
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "GoldBot-MCP", "version": "2.0"}}}
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        resp = await self.client.post(MCP_URL, json=body, headers=headers)
        resp.raise_for_status()
        self.session_id = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
        if not self.session_id:
            print(f"[GoldMCP] No session ID")
            return False
        print(f"[GoldMCP] Session: {self.session_id}")

        await self.mcp_request("notifications/initialized")

        tools_resp = await self.mcp_request("tools/list")
        tools = tools_resp.get("result", {}).get("tools", [])
        self.tool_names = {t["name"]: t for t in tools}
        print(f"[GoldMCP] Connected. {len(self.tool_names)} tools")

        sym = self._parse_text(await self.call("get_symbol_details", {"symbolName": SYMBOL}))
        if sym:
            self.lot_size = int(sym.get("lotSize", 100))
            self.pip_size = float(sym.get("pipSize", 0.01))
            self.digits = int(sym.get("digits", 2))
            self.volume_step = int(sym.get("volumeStep", 1))
            print(f"[GoldMCP] {SYMBOL}: lotSize={self.lot_size} pipSize={self.pip_size} "
                  f"minVol={sym.get('minVolume')} step={self.volume_step}")
        return True

    async def get_positions_raw(self):
        return self._parse_text(await self.call("get_positions"))

    async def get_balance_raw(self):
        return self._parse_text(await self.call("get_balance"))

    async def get_trendbars(self, symbol, timeframe, count):
        to_ts = iso_now()
        from_dt = datetime.now(timezone.utc) - timedelta(minutes=count * 5)
        from_ts = from_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        result = self._parse_text(await self.call("get_trendbars", {
            "symbolName": symbol, "timeframe": timeframe,
            "from": from_ts, "to": to_ts, "limit": count
        }))
        return result

    async def place_market_order(self, side, volume, sl_pips=None, tp_pips=None):
        args = {"symbolName": SYMBOL, "side": side,
                "volume": volume, "volumeType": VOLUME_TYPE, "label": "GoldBot-MCP"}
        if sl_pips is not None:
            args["stopLossPips"] = sl_pips
        if tp_pips is not None:
            args["takeProfitPips"] = tp_pips
        return self._parse_text(await self.call("place_market_order", args))

    async def amend_position(self, position_id, stop_loss=None, take_profit=None):
        args = {"positionId": position_id}
        if stop_loss is not None:
            args["stopLoss"] = stop_loss
        if take_profit is not None:
            args["takeProfit"] = take_profit
        return await self.call("amend_position", args)

    async def close_position(self, position_id):
        return await self.call("close_position", {"positionId": position_id})

    async def close_position_partial(self, position_id, volume):
        return await self.call("close_position_partial", {
            "positionId": position_id, "volume": volume, "volumeType": VOLUME_TYPE
        })

    # ─── Sizing ────────────────────────────────────────────────────

    def entry_volume(self, entry_idx):
        if entry_idx < len(ENTRY_VOLUMES):
            return ENTRY_VOLUMES[entry_idx]
        return ENTRY_VOLUMES[-1]

    @property
    def avg_price(self):
        if not self.entries:
            return 0
        total_vol = sum(e["volume_lots"] for e in self.entries)
        if total_vol == 0:
            return 0
        return sum(e["price"] * e["volume_lots"] for e in self.entries) / total_vol

    @property
    def total_volume(self):
        return sum(e["volume_lots"] for e in self.entries)

    @property
    def has_position(self):
        return len(self.entries) > 0

    def get_sl_price(self, price, atr):
        td = max(atr * SL_ATR_MULT, price * 0.025)
        if not self.is_short:
            return round(self.extreme_price - td, self.digits)
        else:
            return round(self.extreme_price + td, self.digits)

    def get_tp1_price(self, price):
        td = max(self.entry_atr * TP1_ATR_MULT, self.avg_price * 0.005)
        if not self.is_short:
            return round(self.avg_price + td, self.digits)
        else:
            return round(self.avg_price - td, self.digits)

    def get_tp2_price(self, price):
        td = max(self.entry_atr * TP2_ATR_MULT, self.avg_price * 0.01)
        if not self.is_short:
            return round(self.avg_price + td, self.digits)
        else:
            return round(self.avg_price - td, self.digits)

    def get_current_pnl_pct(self, price):
        if not self.has_position:
            return 0
        avg = self.avg_price
        if self.is_short:
            return (avg - price) / avg * 100
        else:
            return (price - avg) / avg * 100

    # ─── Core tick ──────────────────────────────────────────────────

    async def reconnect(self):
        delay = RECONNECT_DELAY
        while True:
            print(f"[GoldMCP] Reconnecting in {delay}s...")
            await asyncio.sleep(delay)
            try:
                await self.client.aclose()
            except Exception:
                pass
            self.client = httpx.AsyncClient()
            self.session_id = None
            ok = await self.connect()
            if ok:
                print("[GoldMCP] Reconnected")
                return
            delay = min(delay * 2, MAX_RECONNECT_DELAY)

    async def run(self):
        ok = await self.connect()
        if not ok:
            await self.reconnect()
        print(f"[GoldMCP] Bot v2 | {SYMBOL} | entries={ENTRY_VOLUMES} lots "
              f"| max={MAX_ENTRIES} | SL={SL_ATR_MULT}atr | TP1={TP1_ATR_MULT}atr TP2={TP2_ATR_MULT}atr")

        self._load_state()

        # Restore cTrader position on restart
        pos_data = await self.get_positions_raw()
        if pos_data and isinstance(pos_data, dict):
            for p in pos_data.get("positions", []):
                if p.get("symbolName") == SYMBOL:
                    self.entries = [{"price": float(p.get("entryPrice", 0)),
                                     "volume_lots": float(p.get("volumeInLots", 0.01))}]
                    self.is_short = p.get("tradeSide", "Buy").lower() == "sell"
                    self.entry_time = int(time.time() * 1000)
                    self.closed_half = False
                    self.extreme_price = float(p.get("currentPrice", 0))
                    print(f"[GoldMCP] Restored position: {'SHORT' if self.is_short else 'LONG'} "
                          f"{self.total_volume} lots @ {self.avg_price}")

        while True:
            try:
                await self.tick()
            except (httpx.HTTPError, httpx.TimeoutException, httpx.ConnectError,
                    httpx.RemoteProtocolError, ConnectionError) as e:
                print(f"[GoldMCP] Connection lost: {e}")
                await self.reconnect()
            except Exception as e:
                print(f"[GoldMCP] Error: {e}")
                await asyncio.sleep(CHECK_INTERVAL)
            await asyncio.sleep(CHECK_INTERVAL)

    async def tick(self):
        now = int(time.time() * 1000)

        # 1. Balance
        bal = await self.get_balance_raw()
        balance = float(bal.get("balance") or 0) if bal else 0
        equity = float(bal.get("equity") or 0) if bal else 0

        # 2. Daily loss check
        if self.daily_start_balance is None:
            self.daily_start_balance = balance
        daily_pnl = balance - self.daily_start_balance
        daily_limit = -self.daily_start_balance * (MAX_DAILY_LOSS_PERCENT / 100)
        if daily_pnl < daily_limit:
            if not self.daily_loss_hit:
                print(f"[GoldMCP] DAILY LOSS: {daily_pnl:.2f} < {daily_limit:.2f} — paused")
                self.daily_loss_hit = True
            if self.has_position:
                await self.close_all("DAILY_LOSS")
            return
        today = datetime.now().strftime("%Y-%m-%d")
        if self.daily_pnl_day != today:
            if self.daily_pnl_day:
                print(f"[GoldMCP] Day {self.daily_pnl_day} PnL: ${self.daily_pnl:.2f}")
            self.daily_pnl_day = today
            self.daily_pnl = daily_pnl

        # Target profit check
        if TARGET_PROFIT > 0 and self.total_pnl >= TARGET_PROFIT and not self.target_hit:
            self.target_hit = True
            print(f"[GoldMCP] TARGET REACHED: ${self.total_pnl:.2f} >= ${TARGET_PROFIT:.2f} — stopped")
            if self.has_position:
                await self.close_all("TARGET")
            return

        # 3. Sync position from cTrader
        await self.sync_position()

        # 4. Candles (every 60s)
        if now - self.last_candle_fetch > 60000:
            await self.fetch_candles()
            self.last_candle_fetch = now

        if not self.close_prices or len(self.close_prices) < 50:
            return

        price = self.close_prices[-1]
        self.atr = calc_atr(self.high_prices, self.low_prices, self.close_prices, 14)
        self.adx = calc_adx(self.high_prices, self.low_prices, self.close_prices, 14)
        self.ema = calc_ema(self.close_prices, 20)

        print(f"[GoldMCP] ${price:.2f} | EMA={self.ema:.1f} ADX={self.adx:.1f} ATR={self.atr:.2f} "
              f"| Balance={balance:.0f} Pos={self.total_volume:.2f}lots")

        # Periodic state save (every 60s via this tick)
        self._save_state()
        self._push_state_to_vps()

        # Track trading days
        if self.has_position or self.entries:
            self.trading_days.add(today)

        # Log challenge progress
        if TARGET_PROFIT > 0:
            print(f"[GoldMCP] Progress: ${self.total_pnl:.2f} / ${TARGET_PROFIT:.2f} "
                  f"| days={len(self.trading_days)} PnL/day={self.daily_pnl:.2f}")

        # 5. Manage existing position
        if self.has_position:
            await self.manage_position(price)
            return

        # 6. Check cooldown / interval
        if self.tp_cooldown_until > int(time.time()):
            return
        if now - self.entry_time < MIN_INTERVAL_MINUTES * 60 * 1000 and self.entry_time > 0:
            return

        # 7. Strategy signal
        enter, reason = should_enter(self.close_prices, self.high_prices, self.low_prices)
        if not enter:
            return
        if self.adx > 0 and self.adx < 20:
            print(f"[GoldMCP] ADX {self.adx:.1f} < 20 — skip")
            return

        # 8. First entry
        side = "sell" if "SHORT" in reason else "buy"
        await self.open_entry(side, price, balance)

    async def sync_position(self):
        """Sync self.entries with actual cTrader positions."""
        pos_data = await self.get_positions_raw()
        our_positions = []
        if pos_data and isinstance(pos_data, dict):
            for p in pos_data.get("positions", []):
                if p.get("symbolName") == SYMBOL and p.get("label", "").startswith("GoldBot"):
                    our_positions.append(p)

        if not our_positions and self.has_position:
            print(f"[GoldMCP] Position closed externally — resetting")
            self.entries = []
            self.closed_half = False

    async def fetch_candles(self):
        bars_resp = await self.get_trendbars(SYMBOL, TIMEFRAME, CANDLE_COUNT)
        if not bars_resp:
            return
        bars = bars_resp.get("bars") or bars_resp.get("trendbars") or []
        if len(bars) < 50:
            return
        self.close_prices = [float(b.get("close", 0)) for b in bars if isinstance(b, dict)]
        self.high_prices = [float(b.get("high", 0)) for b in bars if isinstance(b, dict)]
        self.low_prices = [float(b.get("low", 0)) for b in bars if isinstance(b, dict)]

    # ─── Entry management ──────────────────────────────────────────

    async def open_entry(self, side, price, balance):
        if not self.entries:
            self.is_short = side == "sell"
            self.entry_time = int(time.time() * 1000)
            self.extreme_price = price
            self.entry_atr = self.atr
            self.entry_ema = self.ema
            self.closed_half = False
            self.entries = []

        entry_idx = len(self.entries)
        if entry_idx >= MAX_ENTRIES:
            print(f"[GoldMCP] Max entries ({MAX_ENTRIES}) reached")
            return

        vol = self.entry_volume(entry_idx)
        print(f"[GoldMCP] Entry #{entry_idx + 1}: {side.upper()} {vol} lots @ ${price:.2f}")

        if not self.entries:
            # First entry: set SL/TP
            sl_pips = int(self.entry_atr * SL_ATR_MULT / self.pip_size) if self.entry_atr > 0 else 200
            tp_pips = int(self.entry_atr * TP2_ATR_MULT / self.pip_size) if self.entry_atr > 0 else 400
            order = await self.place_market_order(side, vol, sl_pips, tp_pips)
        else:
            order = await self.place_market_order(side, vol)

        if order:
            self.entries.append({"price": price, "volume_lots": vol})
            self.last_entry_minute = datetime.now().minute
            print(f"[GoldMCP] Entry #{entry_idx + 1} done | avg={self.avg_price:.2f} vol={self.total_volume:.2f}")
            await asyncio.sleep(2)
        else:
            print(f"[GoldMCP] Entry #{entry_idx + 1} failed")

    async def manage_position(self, price):
        if not self.has_position:
            return
        avg = self.avg_price

        # Extreme price tracking
        if not self.is_short:
            if price > self.extreme_price:
                self.extreme_price = price
                td = max(self.atr * SL_ATR_MULT, price * 0.025)
                new_sl = self.extreme_price - td
                if new_sl > self.get_sl_price(price, self.atr):
                    print(f"[GoldMCP] Trail SL → ${new_sl:.2f}")
        else:
            if price < self.extreme_price:
                self.extreme_price = price
                td = max(self.atr * SL_ATR_MULT, price * 0.025)
                new_sl = self.extreme_price + td
                if new_sl < self.get_sl_price(price, self.atr):
                    print(f"[GoldMCP] Trail SL → ${new_sl:.2f}")

        # Break-even after activation pct
        pnl_pct = self.get_current_pnl_pct(price)
        if abs(pnl_pct) >= TRAIL_ACTIVATE_PCT:
            if not self.is_short:
                be_sl = max(avg, avg - self.atr * 0.5)
                if be_sl > self.get_sl_price(price, self.atr):
                    print(f"[GoldMCP] SL to break-even ${be_sl:.2f}")
            else:
                be_sl = min(avg, avg + self.atr * 0.5)
                if be_sl < self.get_sl_price(price, self.atr):
                    print(f"[GoldMCP] SL to break-even ${be_sl:.2f}")

        # TP1: close 50%
        tp1_price = self.get_tp1_price(price)
        if not self.closed_half:
            hit = (not self.is_short and price >= tp1_price) or (self.is_short and price <= tp1_price)
            if hit:
                half_vol = round(self.total_volume / 2, 2)
                print(f"[GoldMCP] TP1 hit @ ${price:.2f} — closing {half_vol} lots (50%)")
                await self.close_position_partial(0, half_vol)
                self.closed_half = True
                self._write_trade("TP1", self.avg_price, price, 0, len(self.entries))
                self._save_state()
                return

        # TP2: full close
        tp2_price = self.get_tp2_price(price)
        hit_tp = (not self.is_short and price >= tp2_price) or (self.is_short and price <= tp2_price)
        if hit_tp:
            print(f"[GoldMCP] TP2 hit @ ${price:.2f} — closing all")
            await self.close_all("TP")
            return

        # Scale-in
        if len(self.entries) < MAX_ENTRIES and not self.closed_half:
            pnl_pct = self.get_current_pnl_pct(price)
            if pnl_pct > -0.5:
                can_scale = (self.is_short and price <= self.ema and price < avg) or \
                            (not self.is_short and price >= self.ema and price > avg)
                # For 3rd entry: don't if price reversed past first entry
                if len(self.entries) == 2:
                    if self.is_short and price >= self.entries[0]["price"]:
                        can_scale = False
                    if not self.is_short and price <= self.entries[0]["price"]:
                        can_scale = False
                if can_scale:
                    bal = await self.get_balance_raw()
                    balance = float(bal.get("balance") or 0) if bal else 0
                    await self.open_entry("sell" if self.is_short else "buy", price, balance)

        # Time exit
        hrs = (int(time.time() * 1000) - self.entry_time) / 3600000
        if hrs >= TIME_EXIT_HOURS and abs(pnl_pct) < 1:
            print(f"[GoldMCP] Time exit ({hrs:.1f}h, PnL {pnl_pct:.2f}%)")
            await self.close_all("TIME")

    async def close_all(self, reason):
        entry_price = self.avg_price if self.entries else 0
        entries_used = len(self.entries)
        if reason == "TP":
            self.consecutive_tp += 1
            if self.consecutive_tp >= 3:
                self.tp_cooldown_until = int(time.time()) + 30 * 60
        else:
            self.consecutive_tp = 0
        print(f"[GoldMCP] Closing all — {reason}")
        bal_before = await self.get_balance_raw()
        await self.call("close_all_positions", {"symbolName": SYMBOL})
        bal_after = await self.get_balance_raw()
        if bal_before and bal_after and entry_price > 0:
            exit_price = self.close_prices[-1] if self.close_prices else 0
            pnl = float(bal_after.get("balance", 0)) - float(bal_before.get("balance", 0))
            self.total_pnl += pnl
            self._write_trade(reason, entry_price, exit_price, pnl, entries_used)
        self.entries = []
        self.closed_half = False
        self._save_state()


async def main():
    bot = GoldMCPBot()
    try:
        await bot.run()
    finally:
        await bot.client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
