#!/usr/bin/env python3
"""GoldBot for cTrader Remote MCP — works on Linux VPS without cTrader Desktop.

DIFFERENCES from gold_mcp_bot.py (Local MCP):
- Connects to https://mcp.ctrader.com/trading/mcp (Remote MCP)
- Uses Bearer token auth
- Parses SSE responses (event: message\ndata: {...})
- Uses Remote MCP tool names: create_order, amend_position, close_position, etc.
- Volume in "cents of base" (lots × lotSize × 100), not lots
- Period format "M_5" not "m5"
- symbolId (number) not symbolName (string)

DRY RUN MODE:
- DRY_RUN=true (default): bot fetches data, prints what it WOULD do, but does NOT place orders
- DRY_RUN=false: real trading

STRATEGY (identical to gold_mcp_bot.py):
- EMA20 + ADX(14) Wilder + ATR(14) on M5
- Scale-in 3 entries with 5 min cooldown
- SL=2×ATR, TP1=1.5×ATR (50% close), TP2=4×ATR
- Trailing SL (extreme - 2×ATR, only up)
- Break-even at +0.5% PnL (SL = entry price)
- Time exit after 4h if |PnL| < 1%
"""
import asyncio
import json
import time
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_DOWN

# Windows console fix (no-op on Linux)
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

import httpx
from dotenv import load_dotenv

from strategy_rsi import should_enter, calc_rsi, calc_stochastic, calc_ema, should_exit_rsi
from strategy import calc_atr, calc_adx
from news_filter import is_news_blackout

load_dotenv()

# === MCP config ===
MCP_URL = os.getenv("MCP_URL", "https://mcp.ctrader.com/trading/mcp")
MCP_BEARER_TOKEN = os.getenv("MCP_BEARER_TOKEN", "")

# === RSI Strategy config ===
FIXED_SL_USD = float(os.getenv("FIXED_SL_USD", "25"))
FIXED_TP_USD = float(os.getenv("FIXED_TP_USD", "35"))
RSI_OVERSOLD = float(os.getenv("RSI_OVERSOLD", "20"))
RSI_OVERBOUGHT = float(os.getenv("RSI_OVERBOUGHT", "80"))
STOCH_OVERSOLD = float(os.getenv("STOCH_OVERSOLD", "20"))
STOCH_OVERBOUGHT = float(os.getenv("STOCH_OVERBOUGHT", "80"))
RSI_EXIT = float(os.getenv("RSI_EXIT", "50"))
MAX_DAILY_TRADES = int(os.getenv("MAX_DAILY_TRADES", "3"))

# Voice bot pause flag — when exists, auto-bot does NOT trade (let voice bot handle)
VOICE_PAUSE_FLAG = "/root/bots/voice_pause.flag"

def is_voice_paused():
    """Check if voice bot has requested pause. Returns (paused: bool, expires_in: int seconds)."""
    if not os.path.exists(VOICE_PAUSE_FLAG):
        return False, 0
    try:
        with open(VOICE_PAUSE_FLAG) as f:
            expiry = int(f.read().strip())
        now = int(time.time())
        if now >= expiry:
            # Expired — remove stale flag
            os.remove(VOICE_PAUSE_FLAG)
            return False, 0
        return True, expiry - now
    except (ValueError, IOError):
        return False, 0
SYMBOL = os.getenv("SYMBOL_NAME", "XAUUSD")
MIN_INTERVAL_MINUTES = int(os.getenv("MIN_INTERVAL_MINUTES", "60"))
MAX_DAILY_LOSS_PERCENT = float(os.getenv("MAX_LOSS_PERCENT", "3.0"))
TIMEFRAME = os.getenv("TIMEFRAME", "M_5")  # Remote MCP uses M_5 not m5
CANDLE_COUNT = int(os.getenv("CANDLE_COUNT", "100"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
COOLDOWN_AFTER_SL = int(os.getenv("COOLDOWN_AFTER_SL", "0"))
RECONNECT_DELAY = int(os.getenv("RECONNECT_DELAY", "5"))
MAX_RECONNECT_DELAY = int(os.getenv("MAX_RECONNECT_DELAY", "300"))

ENTRY_VOLUMES = [float(x) for x in os.getenv("ENTRY_VOLUMES", "0.7").split(",")]
MAX_ENTRIES = int(os.getenv("MAX_ENTRIES", "1"))
SL_ATR_MULT = float(os.getenv("SL_ATR_MULT", "2.0"))
TP1_ATR_MULT = float(os.getenv("TP1_ATR_MULT", "1.5"))
TP2_ATR_MULT = float(os.getenv("TP2_ATR_MULT", "4.0"))
TRAIL_ACTIVATE_PCT = float(os.getenv("TRAIL_ACTIVATE_PCT", "0.5"))
TIME_EXIT_HOURS = float(os.getenv("TIME_EXIT_HOURS", "4"))
BE_TRIGGER_PCT = float(os.getenv("BE_TRIGGER_PCT", "0.5"))
BE_OFFSET_ATR = float(os.getenv("BE_OFFSET_ATR", "0.0"))
# Scale-in cooldown (seconds) between consecutive entries. Default 300s = 5 min.
SCALE_IN_COOLDOWN_SEC = int(os.getenv("SCALE_IN_COOLDOWN_SEC", "300"))
# Scale-in distance filter: how far price must pull back from avg before adding entry.
# Default 1.0 × ATR (was 0.5, which was too loose — bot added entries on noise, not real pullbacks).
SCALE_IN_DISTANCE_MULT = float(os.getenv("SCALE_IN_DISTANCE_MULT", "1.0"))
# Pullback filter for FIRST entry: max distance (in ATR multiples) price can be from EMA
# to allow entry. If price > PULLBACK_MAX_MULT * ATR above EMA → skip LONG (buying too high).
# If price > PULLBACK_MAX_MULT * ATR below EMA → skip SHORT (selling too low).
# Default 1.0 × ATR. Today's bad LONG at $3984 with EMA $3981 (distance $3, ATR $5 = 0.6xATR)
# would have passed. But LONG at $3990 (distance $9 = 1.8xATR) would be blocked.
PULLBACK_MAX_MULT = float(os.getenv("PULLBACK_MAX_MULT", "1.0"))
# Consecutive loss pause: if last N trades all closed in loss, pause for X seconds.
# Default: 2 losses → 1800s (30 min) pause. Stops series like today's 4 LONG losses.
CONSEC_LOSS_COUNT = int(os.getenv("CONSEC_LOSS_COUNT", "2"))
CONSEC_LOSS_PAUSE_SEC = int(os.getenv("CONSEC_LOSS_PAUSE_SEC", "1800"))
# Trend filter: check EMA on higher timeframe (M30) to confirm trend direction.
# If M30 EMA is falling → no LONG entries on M5 (even if M5 price > EMA).
# If M30 EMA is rising → no SHORT entries on M5 (even if M5 price < EMA).
# Default: enabled (true). Set to false to disable.
TREND_FILTER_ENABLED = os.getenv("TREND_FILTER_ENABLED", "true").lower() == "true"
TREND_FILTER_TF = os.getenv("TREND_FILTER_TF", "M_30")  # M_15, M_30, H_1

# === Dry run mode ===
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

# === VPS sync ===
TRADE_LOG_PATH = os.getenv("TRADE_LOG_PATH", "trades_gold_ctrader.jsonl")
STATE_FILE_PATH = os.getenv("STATE_FILE_PATH", "state_remote.json")
TARGET_PROFIT = float(os.getenv("TARGET_PROFIT", "0"))
VPS_SYNC_URL = os.getenv("VPS_SYNC_URL", "").rstrip("/")
VPS_AUTH_TOKEN = os.getenv("VPS_AUTH_TOKEN", "")
VPS_SYNC_ENABLED = os.getenv("VPS_SYNC_ENABLED", "false").lower() == "true"

# === Symbol metadata (will be fetched from cTrader) ===
# XAUUSD on PipFarm: price $4031.78 = 403178000 pipettes → pipDigits=5
SYMBOL_ID = int(os.getenv("SYMBOL_ID", "41"))  # 41 for XAUUSD on PipFarm demo
LOT_SIZE = int(os.getenv("LOT_SIZE", "100"))    # 100 oz per 1 lot
PIP_DIGITS = int(os.getenv("PIP_DIGITS", "5"))  # XAUUSD: 5 digits (price × 100000 = pipettes)
MONEY_DIGITS = int(os.getenv("MONEY_DIGITS", "2"))  # USD: 2 (cents)


def iso_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# MSK timezone for market hours
MSK = timezone(timedelta(hours=3))


def is_market_open() -> bool:
    """Check if market is open.
    
    BTCUSD: 24/7 (crypto never sleeps)
    XAUUSD: Mon-Thu 01:15-23:45, Fri 01:15-23:45, Sat-Sun closed (MSK)
    """
    # BTC trades 24/7 — always open
    if "BTC" in SYMBOL:
        return True
    
    # XAUUSD — market hours
    now = datetime.now(MSK)
    weekday = now.weekday()  # 0=Mon, 6=Sun
    
    if weekday == 5 or weekday == 6:
        return False
    
    hour_min = now.hour * 60 + now.minute
    open_min = 1 * 60 + 15    # 01:15
    close_min = 23 * 60 + 45  # 23:45
    
    return open_min <= hour_min <= close_min


def market_status_str() -> str:
    """Return human-readable market status."""
    if "BTC" in SYMBOL:
        return "OPEN (24/7 crypto)"
    
    now = datetime.now(MSK)
    weekday = now.weekday()
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    if not is_market_open():
        if weekday == 5 or weekday == 6:
            return f"CLOSED (weekend, opens Mon 01:15 MSK)"
        elif weekday == 4 and now.hour >= 23:
            return f"CLOSED (weekend, opens Mon 01:15 MSK)"
        else:
            return f"CLOSED (night pause, opens 01:15 MSK)"
    return f"OPEN ({days[weekday]} {now.strftime('%H:%M')} MSK)"


def lots_to_volume_cents(lots: float, lot_size: int = LOT_SIZE) -> int:
    """Convert lots to cTrader volume (cents of base asset).
    XAUUSD: 0.02 lots × 100 oz × 100 = 200 cents
    """
    return int(lots * lot_size * 100)


def cents_to_dollars(cents: int, money_digits: int = MONEY_DIGITS) -> float:
    """Convert cents to dollars. 507958 cents / 100 = $5079.58"""
    return cents / (10 ** money_digits)


def pipettes_to_price(pipettes: int, pip_digits: int = PIP_DIGITS) -> float:
    """Convert pipettes (smallest price unit) to display price.
    403797 pipettes / 100 = $4037.97
    """
    return pipettes / (10 ** pip_digits)


def price_to_pipettes(price: float, pip_digits: int = PIP_DIGITS) -> int:
    """Convert display price to pipettes. $4037.97 → 403797"""
    return int(price * (10 ** pip_digits))


class GoldMCPRemoteBot:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=30)
        self.session_id = None
        self.tool_names = {}

        # Entry state
        self.entries = []
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
        # M30 trend filter state
        self.m30_close_prices = []
        self.m30_ema = 0.0
        self.m30_ema_prev = 0.0  # previous M30 EMA value (to detect rising/falling)
        self.m30_ema_history = []  # last 5 M30/M15 EMA values for 3-bar trend confirmation (bug #58)
        self.last_m30_fetch = 0
        # Daily open (from H1 candle) for daily trend filter
        self.today_open = 0.0
        self.last_daily_open_fetch = 0
        # Consecutive loss pause tracking
        self.recent_trades = []  # list of recent trade dicts {ts, pnl, ...}
        self._consec_pause_until = 0  # timestamp when consecutive loss pause ends
        self.today_high = 0.0
        self.today_low = 0.0

        # Challenge tracking
        self.total_pnl = 0.0
        self.trading_days = set()
        self.target_hit = False

        # SL tracker
        self.current_sl = 0.0
        self.initial_balance = None
        self.last_scale_in_time = 0
        self.sl_cooldown_until = 0
        self.consecutive_losses = 0

        # Current balance (updated each tick from cTrader)
        self.current_balance = 0.0

        # Balance snapshot at position open — used for accurate per-trade PnL
        # (fixes bug where total_pnl included historical losses)
        self.balance_at_position_open = 0.0

        # VPS sync state
        self.last_state_push = 0
        self.vps_sync_failures = 0

        # Symbol metadata (fetched at connect)
        self.symbol_id = SYMBOL_ID
        self.lot_size = LOT_SIZE
        self.pip_digits = PIP_DIGITS
        self.money_digits = MONEY_DIGITS

    @property
    def digits(self):
        return self.pip_digits

    # ─── MCP helpers ────────────────────────────────────────────────

    def _mcp_headers(self):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if MCP_BEARER_TOKEN:
            headers["Authorization"] = f"Bearer {MCP_BEARER_TOKEN}"
        return headers

    async def mcp_request(self, method, params=None):
        """Send MCP request, parse SSE response, return parsed JSON or None.
        If HTTP 404 (session expired), increment error counter and reconnect after 3 failures."""
        req_id = int(time.time() * 1000) % 100000
        body = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params:
            body["params"] = params
        headers = self._mcp_headers()
        try:
            resp = await self.client.post(MCP_URL, json=body, headers=headers)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status == 404:
                # Session expired — cTrader returns 404 on tools/call when session is dead
                if not hasattr(self, '_consecutive_404'):
                    self._consecutive_404 = 0
                self._consecutive_404 += 1
                print(f"[Remote] HTTP 404 in {method} (session expired?) — count={self._consecutive_404}")
                if self._consecutive_404 >= 3:
                    print(f"[Remote] 3 consecutive 404s — reconnecting...")
                    self._consecutive_404 = 0
                    await self.reconnect()
                return None
            else:
                print(f"[Remote] HTTP {status} error in {method}: {e}")
                return None
        except Exception as e:
            print(f"[Remote] HTTP error in {method}: {e}")
            return None

        # Success — reset 404 counter
        if hasattr(self, '_consecutive_404'):
            self._consecutive_404 = 0

        # Parse SSE: lines starting with "data: "
        for line in resp.text.split("\n"):
            if line.startswith("data: "):
                data = line[6:].strip()
                try:
                    return json.loads(data)
                except json.JSONDecodeError:
                    continue
        return None

    async def call(self, tool_name, args=None):
        """Call MCP tool, return parsed content (JSON object) or None."""
        resp = await self.mcp_request("tools/call", {"name": tool_name, "arguments": args or {}})
        if not resp:
            return None
        if "error" in resp:
            print(f"[Remote] Error {tool_name}: {resp['error']}")
            return None
        result = resp.get("result", {})
        if result.get("isError"):
            content = result.get("content", [])
            for item in content:
                if item.get("type") == "text":
                    print(f"[Remote] Tool error {tool_name}: {item.get('text', '')[:200]}")
            return None
        content = result.get("content", [])
        for item in content:
            if item.get("type") == "text":
                text = item.get("text", "")
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text
        return None

    # ─── Connection ─────────────────────────────────────────────────

    async def connect(self):
        auth_str = "Bearer token (Remote MCP)" if MCP_BEARER_TOKEN else "NO AUTH"
        dry_str = "DRY RUN (no orders)" if DRY_RUN else "LIVE TRADING"
        print(f"[Remote] Connecting to {MCP_URL}")
        print(f"[Remote] Auth: {auth_str} | Mode: {dry_str}")

        if not MCP_BEARER_TOKEN:
            print("[Remote] FATAL: MCP_BEARER_TOKEN not set")
            return False

        # initialize
        resp = await self.mcp_request("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "GoldBot-Remote", "version": "1.0"}
        })
        if not resp or "result" not in resp:
            print(f"[Remote] Initialize failed: {resp}")
            return False

        self.session_id = resp.get("result", {}).get("sessionId") or resp.get("result", {}).get("_meta", {}).get("sessionId")
        # Session ID is in response headers, not body — need to re-fetch
        # Actually we need to do a raw request to get headers
        r = await self.client.post(MCP_URL, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "GoldBot-Remote", "version": "1.0"}}
        }, headers=self._mcp_headers())
        self.session_id = r.headers.get("Mcp-Session-Id") or r.headers.get("mcp-session-id")

        if not self.session_id:
            print("[Remote] No session ID in response headers")
            return False
        print(f"[Remote] Session: {self.session_id}")

        # notifications/initialized
        await self.mcp_request("notifications/initialized")

        # tools/list
        tools_resp = await self.mcp_request("tools/list")
        if not tools_resp:
            print("[Remote] tools/list failed")
            return False
        tools = tools_resp.get("result", {}).get("tools", [])
        self.tool_names = {t["name"]: t for t in tools}
        print(f"[Remote] Connected. {len(self.tool_names)} tools available")

        # Fetch symbol metadata
        await self.fetch_symbol_metadata()

        return True

    async def fetch_symbol_metadata(self):
        """Fetch symbolId + lotSize for our SYMBOL."""
        syms = await self.call("get_symbols")
        if not syms:
            print(f"[Remote] WARNING: get_symbols failed, using defaults symbolId={SYMBOL_ID}")
            return
        sym_list = syms.get("symbols", [])
        for s in sym_list:
            name = s.get("symbolName", "") or s.get("name", "")
            if name.upper() == SYMBOL.upper():
                self.symbol_id = s.get("symbolId")
                print(f"[Remote] {SYMBOL}: symbolId={self.symbol_id}")
                break
        # Note: get_symbols does NOT return lotSize per Remote MCP docs
        # We use LOT_SIZE from env (default 100 for XAUUSD)

    async def reconnect(self):
        delay = RECONNECT_DELAY
        while True:
            print(f"[Remote] Reconnecting in {delay}s...")
            await asyncio.sleep(delay)
            try:
                await self.client.aclose()
            except Exception:
                pass
            self.client = httpx.AsyncClient(timeout=30)
            self.session_id = None
            ok = await self.connect()
            if ok:
                print("[Remote] Reconnected")
                return
            delay = min(delay * 2, MAX_RECONNECT_DELAY)

    async def run(self):
        ok = await self.connect()
        if not ok:
            await self.reconnect()

        print(f"[Remote] Bot | {SYMBOL} | RSI strategy | vol={ENTRY_VOLUMES} | SL=${FIXED_SL_USD} TP=${FIXED_TP_USD}")
        print(f"[Remote] RSI oversold={RSI_OVERSOLD} overbought={RSI_OVERBOUGHT} exit={RSI_EXIT} | Stoch {STOCH_OVERSOLD}/{STOCH_OVERBOUGHT}")
        print(f"[Remote] Max {MAX_DAILY_TRADES} trades/day | M30 EMA41 trend filter | News filter | Friday close 23:40")

        self._load_state()

        # Restore positions from cTrader
        await self.restore_positions()

        # Sync total_pnl with balance on startup
        bal_init = await self.get_balance_raw()
        init_bal = bal_init.get("balance_usd", 0) if bal_init else 0
        if self.initial_balance is None:
            self.initial_balance = init_bal
        self.total_pnl = init_bal - self.initial_balance
        print(f"[Remote] initial_balance=${self.initial_balance:.2f} total_pnl=${self.total_pnl:.2f}")

        while True:
            try:
                await self.tick()
            except (httpx.HTTPError, httpx.TimeoutException, httpx.ConnectError,
                    httpx.RemoteProtocolError, ConnectionError) as e:
                print(f"[Remote] Connection lost: {e}")
                await self.reconnect()
            except Exception as e:
                print(f"[Remote] Error: {e}")
                import traceback
                traceback.print_exc()
                await asyncio.sleep(CHECK_INTERVAL)
            await asyncio.sleep(CHECK_INTERVAL)

    async def restore_positions(self):
        """Restore open positions from cTrader on restart."""
        pos_data = await self.get_positions_raw()
        if not pos_data:
            return
        positions = [p for p in pos_data.get("positions", [])
                     if p.get("symbolId") == self.symbol_id]
        if not positions:
            return
        positions.sort(key=lambda p: p.get("createdTimestamp", p.get("positionId", 0)))
        self.entries = []
        for p in positions:
            pid = p.get("positionId")
            entry_price = float(p.get("entryPrice", 0))
            vol_cents = int(p.get("volume", 0))
            vol_lots = vol_cents / (self.lot_size * 100)
            sl_raw = p.get("stopLoss")
            sl_price = float(sl_raw) if sl_raw else 0
            tp_raw = p.get("takeProfit")
            tp_price = float(tp_raw) if tp_raw else 0
            self.entries.append({
                "price": entry_price,
                "volume_lots": vol_lots,
                "position_id": pid,
                "tp_price": tp_price,
                "sl_price": sl_price,
                "extreme_price": entry_price,  # init per-entry extreme for independent trailing SL
                "be_triggered": False
            })
            if sl_price > 0:
                self.current_sl = sl_price
            if tp_price > 0:
                print(f"[Remote] Restored pos {pid}: entry=${entry_price:.2f} SL=${sl_price:.2f} TP=${tp_price:.2f}")
        self.is_short = positions[0].get("tradeSide", "BUY").upper() == "SELL"
        self.entry_time = int(time.time() * 1000)
        self.closed_half = False
        cur_pipettes = positions[0].get("currentPrice", 0)
        self.extreme_price = pipettes_to_price(int(cur_pipettes), self.pip_digits) if cur_pipettes else 0
        print(f"[Remote] Restored {len(self.entries)} position(s): "
              f"{'SHORT' if self.is_short else 'LONG'} {self.total_volume:.2f} lots @ ${self.avg_price:.2f} | SL=${self.current_sl:.2f}")

    async def tick(self):
        now = int(time.time() * 1000)

        # 0. Voice bot pause check — if voice bot wants to trade manually, do NOT open/close positions
        voice_paused, expires_in = is_voice_paused()
        if voice_paused:
            # Log every ~1 min so we don't spam log
            if now % 60000 < CHECK_INTERVAL * 1000:
                print(f"[Remote] VOICE PAUSE active — auto-bot idle ({expires_in}s remaining)")
            # Still fetch candles to keep RSI/Stoch fresh, but do NOT call strategy/manage
            if now - self.last_candle_fetch > 60000:
                await self.fetch_candles()
                self.last_candle_fetch = now
            # Still sync positions to detect voice bot's external closes
            if not DRY_RUN:
                await self.sync_position()
            self._save_state()
            return

        # 1. Balance
        bal = await self.get_balance_raw()
        balance = bal.get("balance_usd", 0) if bal else 0
        self.current_balance = balance  # track current balance for VPS sync
        equity = bal.get("equity_usd", balance) if bal else balance

        # 2. Daily loss check
        if self.daily_start_balance is None:
            self.daily_start_balance = balance
        daily_pnl = balance - self.daily_start_balance
        daily_limit = -self.daily_start_balance * (MAX_DAILY_LOSS_PERCENT / 100)
        if daily_pnl < daily_limit:
            if not self.daily_loss_hit:
                print(f"[Remote] DAILY LOSS: ${daily_pnl:.2f} < ${daily_limit:.2f} — paused")
                self.daily_loss_hit = True
            if self.has_position:
                await self.close_all("DAILY_LOSS")
            # Log every 5 minutes so watchdog doesn't kill us + user sees we're alive
            if now % 300000 < CHECK_INTERVAL * 1000:
                print(f"[Remote] DAILY LOSS paused — waiting for new day (balance=${balance:.2f}, loss=${daily_pnl:.2f})")
            return
        today = datetime.now().strftime("%Y-%m-%d")
        if self.daily_pnl_day != today:
            if self.daily_pnl_day:
                print(f"[Remote] Day {self.daily_pnl_day} PnL: ${self.daily_pnl:.2f}")
            self.daily_pnl_day = today
            self.daily_pnl = daily_pnl

        # Target profit check
        if TARGET_PROFIT > 0 and self.total_pnl >= TARGET_PROFIT and not self.target_hit:
            self.target_hit = True
            print(f"[Remote] TARGET REACHED: ${self.total_pnl:.2f} >= ${TARGET_PROFIT:.2f} — stopped")
            if self.has_position:
                await self.close_all("TARGET")
            return

        # 3. Sync position (skip in DRY_RUN — bot has no real positions)
        if not DRY_RUN:
            await self.sync_position()

        # 3.4 Friday close — close all positions at 23:40 MSK (20:40 UTC) to avoid weekend gap
        now_msk = datetime.now(MSK)
        if "BTC" not in SYMBOL and now_msk.weekday() == 4:  # Friday
            hour_min_msk = now_msk.hour * 60 + now_msk.minute
            if hour_min_msk >= 23 * 60 + 40:  # 23:40 MSK
                if self.has_position:
                    print(f"[Remote] FRIDAY CLOSE: {now_msk.strftime('%H:%M')} MSK — closing all positions before weekend")
                    await self.close_all("FRIDAY_CLOSE")
                return

        # 3.5 Market hours check — skip trading when market closed
        market_open = is_market_open()
        if not market_open:
            # Market closed — only fetch candles occasionally (every 5 min) to save API calls
            if now - self.last_candle_fetch > 300000:  # 5 min
                await self.fetch_candles()
                self.last_candle_fetch = now
            # Log market status every 5 min
            if now % 300000 < 60000:
                print(f"[Remote] Market {market_status_str()} — waiting")
            # If we have open position, still need to manage it (SL/TP might trigger)
            if self.has_position and self.close_prices:
                price = self.close_prices[-1]
                await self.manage_position(price, balance)
            self._save_state()
            self._push_state_to_vps()
            return

        # 4. Candles (every 60s when market is open)
        if now - self.last_candle_fetch > 60000:
            await self.fetch_candles()
            self.last_candle_fetch = now

        if not self.close_prices or len(self.close_prices) < 50:
            print(f"[Remote] Waiting for candles... ({len(self.close_prices)}/50)")
            return

        price = self.close_prices[-1]
        self.atr = calc_atr(self.high_prices, self.low_prices, self.close_prices, 14)
        self.adx = calc_adx(self.high_prices, self.low_prices, self.close_prices, 14)
        self.ema = calc_ema(self.close_prices, 20)

        print(f"[Remote] ${price:.2f} | EMA={self.ema:.1f} ADX={self.adx:.1f} ATR={self.atr:.2f} "
              f"| Balance=${balance:.2f} Pos={self.total_volume:.2f}lots")

        # Periodic state save + VPS push
        self._save_state()
        self._push_state_to_vps()

        # Track trading days
        if self.has_position or self.entries:
            self.trading_days.add(today)

        # Log challenge progress
        if TARGET_PROFIT > 0:
            print(f"[Remote] Progress: ${self.total_pnl:.2f} / ${TARGET_PROFIT:.2f} "
                  f"| days={len(self.trading_days)} PnL/day=${self.daily_pnl:.2f}")

        # 5. Manage existing position
        if self.has_position:
            await self.manage_position(price, balance)
            return

        # 6. Check cooldown / interval
        if self.tp_cooldown_until > int(time.time()):
            return
        if self.sl_cooldown_until > int(time.time()):
            remaining = self.sl_cooldown_until - int(time.time())
            print(f"[Remote] SL cooldown {remaining}s remaining")
            return
        # Consecutive loss pause: if last N trades all closed in loss, pause
        if CONSEC_LOSS_COUNT > 0 and len(self.recent_trades) >= CONSEC_LOSS_COUNT:
            recent = self.recent_trades[-CONSEC_LOSS_COUNT:]
            if all(t.get("pnl", 0) < 0 for t in recent):
                if not hasattr(self, '_consec_pause_until'):
                    self._consec_pause_until = 0
                if int(time.time()) < self._consec_pause_until:
                    # Pause still active — wait
                    remaining = self._consec_pause_until - int(time.time())
                    print(f"[Remote] Consec loss pause: {remaining}s remaining ({CONSEC_LOSS_COUNT} losses in a row)")
                    return
                # Pause expired — reset and continue to strategy signal.
                # Bot resumes FULL operation: can open entries, scale-in, manage positions.
                # A new pause will only trigger if we get CONSEC_LOSS_COUNT NEW losses after this point.
                if self._consec_pause_until > 0:
                    print(f"[Remote] Consec loss pause EXPIRED — resuming full operation")
                    self._consec_pause_until = 0
                    self._save_state()
            else:
                # Streak broken (at least one recent trade was profitable) — clear pause
                if getattr(self, '_consec_pause_until', 0) > 0:
                    self._consec_pause_until = 0
                    self._save_state()
        if now - self.entry_time < MIN_INTERVAL_MINUTES * 60 * 1000 and self.entry_time > 0:
            return

        # 6.5 News filter — no trading 15 min before/after high-impact USD events
        news_blocked, news_msg = is_news_blackout()
        if news_blocked:
            print(f"[Remote] {news_msg} — skip trading")
            return

        # 6.6 Max daily trades
        today_str = datetime.now(MSK).strftime("%Y-%m-%d")
        if not hasattr(self, 'daily_trade_count'):
            self.daily_trade_count = {}
            self.daily_trade_date = today_str
        if self.daily_trade_date != today_str:
            self.daily_trade_count = {}
            self.daily_trade_date = today_str
        trades_today = self.daily_trade_count.get(today_str, 0)
        if trades_today >= MAX_DAILY_TRADES:
            if now % 300000 < CHECK_INTERVAL * 1000:
                print(f"[Remote] Max daily trades ({MAX_DAILY_TRADES}) reached — waiting for new day")
            return

        # 7. RSI Strategy signal
        # Fetch M30 for trend filter
        if int(time.time()) - self.last_m30_fetch > 300:
            await self.fetch_m30_candles()
        enter, reason, rsi_val, stoch_val = should_enter(
            self.close_prices, self.high_prices, self.low_prices,
            today_high=self.today_high, today_low=self.today_low,
            m30_ema=self.m30_ema, m30_ema_prev=self.m30_ema_prev,
            rsi_oversold=RSI_OVERSOLD, rsi_overbought=RSI_OVERBOUGHT,
            stoch_oversold=STOCH_OVERSOLD, stoch_overbought=STOCH_OVERBOUGHT,
            m30_ema_history=self.m30_ema_history
        )
        if not enter:
            if now % 60000 < CHECK_INTERVAL * 1000:
                print(f"[Remote] {reason} | price=${price:.2f}")
            return

        # 8. Single entry — full volume
        side = "sell" if "SHORT" in reason else "buy"
        print(f"[Remote] SIGNAL: {reason} — opening {side.upper()} {ENTRY_VOLUMES} lot")
        await self.open_entry(side, price, balance)
        self.daily_trade_count[today_str] = trades_today + 1

    # ─── Remote MCP tool wrappers ──────────────────────────────────

    async def get_balance_raw(self):
        """Returns {balance_usd, equity_usd, free_margin_usd, balance_cents, money_digits}."""
        data = await self.call("get_balance")
        if not data:
            return None
        # Detect money_digits from response
        md = data.get("moneyDigits", MONEY_DIGITS)
        self.money_digits = md
        bal_cents = int(data.get("balance", 0))
        eq_cents = int(data.get("equity", bal_cents))
        fm_cents = int(data.get("freeMargin", bal_cents))
        return {
            "balance": cents_to_dollars(bal_cents, md),
            "equity": cents_to_dollars(eq_cents, md),
            "freeMargin": cents_to_dollars(fm_cents, md),
            "balance_cents": bal_cents,
            "money_digits": md,
            "balance_usd": cents_to_dollars(bal_cents, md),
            "equity_usd": cents_to_dollars(eq_cents, md),
        }

    async def get_positions_raw(self):
        """Returns positions for our symbol (filtered)."""
        data = await self.call("get_positions")
        if not data:
            return {"positions": []}
        return data

    async def get_spot_price(self):
        """Get current bid/ask for our symbol."""
        data = await self.call("get_spot_prices", {"symbolId": [self.symbol_id]})
        if not data:
            return None
        prices = data.get("prices", []) or data.get("spotPrices", [])
        if not prices:
            return None
        p = prices[0]
        bid = pipettes_to_price(int(p.get("bid", 0)), self.pip_digits)
        ask = pipettes_to_price(int(p.get("ask", 0)), self.pip_digits)
        return {"bid": bid, "ask": ask, "mid": (bid + ask) / 2}

    async def fetch_candles(self):
        """Fetch M5 candles using fromTimestamp + toTimestamp range.
        Remote MCP docs: {fromTimestamp, toTimestamp} for ≤720h range query.
        """
        now = datetime.now(timezone.utc)
        # 100 M15 candles ≈ 25 hours. Need enough for EMA14 + ADX14 + ATR14 (min 42 candles).
        # Was hours=10 for M5; changed to hours=30 for M15.
        from_dt = now - timedelta(hours=72)
        from_iso = from_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        to_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        data = await self.call("get_trendbars", {
            "symbolId": self.symbol_id,
            "period": TIMEFRAME,
            "fromTimestamp": from_iso,
            "toTimestamp": to_iso,
        })
        if not data:
            print("[Remote] fetch_candles: no data")
            return
        bars = data.get("trendbars", []) or data.get("bars", [])
        if len(bars) < 50:
            print(f"[Remote] fetch_candles: only {len(bars)} bars")
            return
        today_str = datetime.now(MSK).strftime("%Y-%m-%d")
        tl = float('inf')
        th = float('-inf')
        self.close_prices = []
        self.high_prices = []
        self.low_prices = []
        for b in bars:
            close = pipettes_to_price(int(b.get("close", b.get("c", 0))), self.pip_digits)
            high = pipettes_to_price(int(b.get("high", b.get("h", 0))), self.pip_digits)
            low = pipettes_to_price(int(b.get("low", b.get("l", 0))), self.pip_digits)
            self.close_prices.append(close)
            self.high_prices.append(high)
            self.low_prices.append(low)
            ts_raw = b.get("utcBeginInMinutes") or b.get("timestamp") or b.get("t")
            if ts_raw is not None:
                if isinstance(ts_raw, (int, float)):
                    if ts_raw > 1e12:
                        ts_sec = ts_raw / 1000
                    elif ts_raw > 1e10:
                        ts_sec = ts_raw
                    else:
                        ts_sec = ts_raw * 60
                    candle_dt = datetime.fromtimestamp(ts_sec, tz=MSK)
                    if candle_dt.strftime("%Y-%m-%d") == today_str:
                        tl = min(tl, low)
                        th = max(th, high)
        self.today_low = tl if tl != float('inf') else 0
        self.today_high = th if th != float('-inf') else 0
        print(f"[Remote] Fetched {len(bars)} candles | last close=${self.close_prices[-1]:.2f} | today range=${self.today_low:.2f}-${self.today_high:.2f}")

    async def fetch_m30_candles(self):
        """Fetch M30 candles for trend filter. Calculates EMA20 on M30 closes.
        Stores current and previous EMA to detect rising/falling trend."""
        now = datetime.now(timezone.utc)
        # 100 M30 candles ≈ 50 hours ≈ 2 days. Enough for EMA20 + history.
        from_dt = now - timedelta(hours=55)
        from_iso = from_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        to_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        data = await self.call("get_trendbars", {
            "symbolId": self.symbol_id,
            "period": TREND_FILTER_TF,
            "fromTimestamp": from_iso,
            "toTimestamp": to_iso,
        })
        if not data:
            print(f"[Remote] fetch_m30: no data for {TREND_FILTER_TF}")
            return
        bars = data.get("trendbars", []) or data.get("bars", [])
        if len(bars) < 25:
            print(f"[Remote] fetch_m30: only {len(bars)} bars for {TREND_FILTER_TF}")
            return
        closes = []
        for b in bars:
            close = pipettes_to_price(int(b.get("close", b.get("c", 0))), self.pip_digits)
            closes.append(close)
        # Calculate EMA41 on M30 closes (41 × 30min = 20.5 hours ≈ 1 day)
        # Was EMA20 (10 hours, too short — reacted to intraday pullbacks as trend changes)
        from strategy import calc_ema
        new_ema = calc_ema(closes, 41)
        # Store previous EMA (for trend direction)
        self.m30_ema_prev = self.m30_ema if self.m30_ema > 0 else new_ema
        self.m30_ema = new_ema
        self.m30_close_prices = closes
        # Track history for 3-bar trend confirmation (bug #58)
        if new_ema > 0:
            self.m30_ema_history.append(new_ema)
            self.m30_ema_history = self.m30_ema_history[-5:]  # keep last 5
        self.last_m30_fetch = int(time.time())
        trend = "rising" if self.m30_ema > self.m30_ema_prev else ("falling" if self.m30_ema < self.m30_ema_prev else "flat")
        print(f"[Remote] M30 EMA41=${self.m30_ema:.2f} (prev=${self.m30_ema_prev:.2f}, {trend})")

    async def fetch_daily_open(self):
        """Fetch today's open price from H1 candles.
        Finds the first H1 candle of the current MSK day and takes its open price.
        This is the benchmark for daily trend direction (variant D).
        Logged so user can verify against cTrader Web."""
        now = datetime.now(timezone.utc)
        # Fetch last 48 H1 candles (2 days) to ensure we get today's first candle
        from_dt = now - timedelta(hours=48)
        from_iso = from_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        to_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        data = await self.call("get_trendbars", {
            "symbolId": self.symbol_id,
            "period": "H_1",
            "fromTimestamp": from_iso,
            "toTimestamp": to_iso,
        })
        if not data:
            print(f"[Remote] fetch_daily_open: no H1 data")
            return
        bars = data.get("trendbars", []) or data.get("bars", [])
        if not bars:
            print(f"[Remote] fetch_daily_open: no H1 bars")
            return
        today_str = datetime.now(MSK).strftime("%Y-%m-%d")
        daily_open = None
        daily_open_time = None
        for b in bars:
            open_p = pipettes_to_price(int(b.get("open", b.get("o", 0))), self.pip_digits)
            ts_raw = b.get("utcBeginInMinutes") or b.get("timestamp") or b.get("t")
            if ts_raw is not None:
                if isinstance(ts_raw, (int, float)):
                    if ts_raw > 1e12:
                        ts_sec = ts_raw / 1000
                    elif ts_raw > 1e10:
                        ts_sec = ts_raw
                    else:
                        ts_sec = ts_raw * 60
                    candle_dt = datetime.fromtimestamp(ts_sec, tz=MSK)
                    if candle_dt.strftime("%Y-%m-%d") == today_str:
                        if daily_open is None:
                            daily_open = open_p
                            daily_open_time = candle_dt.strftime("%H:%M MSK")
                            break  # first H1 candle of the day = daily open
        if daily_open is not None:
            self.today_open = daily_open
            print(f"[Remote] Daily open: ${daily_open:.2f} (from H1 candle at {daily_open_time}) — benchmark for daily trend")
        else:
            print(f"[Remote] fetch_daily_open: no H1 candle found for today ({today_str})")

    async def place_market_order(self, side, volume_lots, sl_price=None, tp_price=None):
        """Place MARKET order. volume_lots → cents. SL/TP as relative points.
        For MARKET orders, Remote MCP requires relativeStopLoss/relativeTakeProfit (points).
        """
        volume_cents = lots_to_volume_cents(volume_lots, self.lot_size)
        trade_side = side.upper()
        if trade_side not in ("BUY", "SELL"):
            print(f"[Remote] Invalid side: {side}")
            return None

        args = {
            "symbolId": self.symbol_id,
            "orderType": "MARKET",
            "tradeSide": trade_side,
            "volume": volume_cents,
        }

        # Convert absolute SL/TP prices to relative PIPETTES
        # relativeStopLoss/TakeProfit are in PIPETTES (price × 10^pip_digits)
        # Must be rounded to step — cTrader requires different steps per symbol:
        # XAUUSD: step 1000 pipettes, BTCUSD: step 100 pipettes
        step = 1000 if "XAU" in SYMBOL else 100
        cur_price = self.close_prices[-1] if self.close_prices else 0
        if sl_price and cur_price:
            price_diff = abs(cur_price - sl_price)
            sl_pipettes = int(round(price_diff * (10 ** self.pip_digits) / step) * step)
            args["relativeStopLoss"] = max(sl_pipettes, step)
        if tp_price and cur_price:
            price_diff = abs(tp_price - cur_price)
            tp_pipettes = int(round(price_diff * (10 ** self.pip_digits) / step) * step)
            args["relativeTakeProfit"] = max(tp_pipettes, step)

        if DRY_RUN:
            print(f"[Remote] DRY RUN — would call create_order: {json.dumps(args)}")
            return {"dry_run": True, "args": args}

        print(f"[Remote] create_order args: {json.dumps(args)}")
        result = await self.call("create_order", args)
        print(f"[Remote] create_order result: {result}")
        # Check if TP/SL were actually set
        if isinstance(result, dict):
            pos = result.get("position", {})
            actual_sl = pos.get("stopLoss")
            actual_tp = pos.get("takeProfit")
            if actual_tp is None or actual_tp == 0:
                print(f"[Remote] WARNING: TP not set by cTrader! Will amend manually.")
            if actual_sl is None or actual_sl == 0:
                print(f"[Remote] WARNING: SL not set by cTrader! Will amend manually.")
        return result

    async def amend_position(self, position_id, stop_loss=None, take_profit=None):
        """Amend position SL/TP. stop_loss/take_profit are DISPLAY prices.
        cTrader allows different digits per symbol:
        - XAUUSD: max 2 digits after decimal
        - BTCUSD: max 3 digits after decimal
        If position returns 404 (closed externally), removes it from self.entries."""
        sl_tp_digits = getattr(self, 'money_digits', 2) or 2
        args = {"positionId": position_id}
        if stop_loss is not None:
            args["stopLoss"] = round(stop_loss, sl_tp_digits)
        if take_profit is not None:
            args["takeProfit"] = round(take_profit, sl_tp_digits)

        if DRY_RUN:
            print(f"[Remote] DRY RUN — would call amend_position: {json.dumps(args)}")
            return {"dry_run": True}

        result = await self.call("amend_position", args)
        if result is None:
            # Amend failed — verify if position was closed externally (404)
            await self._remove_stale_position(position_id)
        return result

    async def close_position(self, position_id, volume_cents=None):
        """Close position (full or partial by volume).
        If position returns 404 (already closed externally), removes it from self.entries."""
        args = {"positionId": position_id}
        if volume_cents is not None:
            args["volume"] = volume_cents

        if DRY_RUN:
            print(f"[Remote] DRY RUN — would call close_position: {json.dumps(args)}")
            return {"dry_run": True}

        result = await self.call("close_position", args)
        if result is None:
            # Close failed — verify if position was already closed externally (404)
            await self._remove_stale_position(position_id)
        return result

    async def _remove_stale_position(self, position_id):
        """Check if position_id still exists in cTrader. If not, remove from self.entries.
        This handles the case where a position was closed externally (by broker TP/SL)
        but bot state still references it. Without this, bot would keep trying to amend
        closed positions, generating 404 errors every tick."""
        if not position_id:
            return
        try:
            pos_data = await self.get_positions_raw()
            if not pos_data:
                # Can't verify (network error?) — leave state as is, will retry next tick
                return
            ctrader_positions = [p for p in pos_data.get("positions", [])
                                 if p.get("symbolId") == self.symbol_id]
            live_pids = {p.get("positionId") for p in ctrader_positions}
            if position_id not in live_pids:
                # Position was closed externally — remove from state
                old_len = len(self.entries)
                self.entries = [e for e in self.entries if e.get("position_id") != position_id]
                if len(self.entries) < old_len:
                    print(f"[Remote] Position {position_id} not in cTrader — removed from state "
                          f"({len(self.entries)} entries left)")
                    if not self.entries:
                        # All positions closed — reset position state
                        self.closed_half = False
                        self.current_sl = 0.0
                        self.extreme_price = 0.0
                        self.last_scale_in_time = 0
                        print(f"[Remote] No entries left — position state reset")
                    self._save_state()
        except Exception as e:
            print(f"[Remote] _remove_stale_position check failed for {position_id}: {e}")

    async def close_position_partial(self, position_id, volume_lots):
        """Close partial position. volume_lots → cents."""
        volume_cents = lots_to_volume_cents(volume_lots, self.lot_size)
        return await self.close_position(position_id, volume_cents)

    async def close_all_positions(self):
        """Close all our positions for symbol."""
        if DRY_RUN:
            print(f"[Remote] DRY RUN — would close all {len(self.entries)} positions")
            return {"dry_run": True}
        for entry in self.entries:
            pid = entry.get("position_id")
            if pid:
                vol_cents = lots_to_volume_cents(entry.get("volume_lots", 0), self.lot_size)
                await self.call("close_position", {"positionId": pid, "volume": vol_cents})

    # ─── Sizing (identical to gold_mcp_bot.py) ─────────────────────

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
        td = atr * SL_ATR_MULT
        if not self.is_short:
            return round(self.extreme_price - td, self.digits)
        else:
            return round(self.extreme_price + td, self.digits)

    def get_tp1_price(self, price):
        td = self.entry_atr * TP1_ATR_MULT
        if not self.is_short:
            return round(self.avg_price + td, self.digits)
        else:
            return round(self.avg_price - td, self.digits)

    def get_tp2_price(self, price):
        td = self.entry_atr * TP2_ATR_MULT
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

    # ─── VPS sync (identical to gold_mcp_bot.py) ───────────────────

    def _write_trade(self, reason, entry_price, exit_price, pnl, entries_used):
        entry = {"ts": iso_now(), "type": "SHORT" if self.is_short else "LONG",
                 "entry_price": round(entry_price, self.digits),
                 "exit_price": round(exit_price, self.digits),
                 "pnl": round(pnl, 2), "reason": reason,
                 "entries": entries_used, "version": "remote-mcp-v1"}
        try:
            with open(TRADE_LOG_PATH, "a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[Remote] Trade log write failed: {e}")
        self._push_trade_to_vps(entry)
        # Track recent trades for consecutive loss pause
        self.recent_trades.append(entry)
        # Keep only last 10 trades
        if len(self.recent_trades) > 10:
            self.recent_trades = self.recent_trades[-10:]

    def _push_trade_to_vps(self, trade_data: dict):
        if not VPS_SYNC_ENABLED or not VPS_SYNC_URL:
            return
        try:
            req = urllib.request.Request(
                f"{VPS_SYNC_URL}/api/trade",
                data=json.dumps(trade_data).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {VPS_AUTH_TOKEN}"},
                method="POST")
            with urllib.request.urlopen(req, timeout=5) as r:
                result = json.loads(r.read())
            if result.get("ok"):
                print(f"[Remote] VPS sync: trade pushed (pnl={trade_data.get('pnl'):+.2f})")
        except Exception as e:
            print(f"[Remote] VPS trade push error: {e}")

    def _load_recent_trades(self, count: int = 10) -> list:
        """Load last N trades from trade log file. Used for consecutive loss pause."""
        if not os.path.exists(TRADE_LOG_PATH):
            return []
        try:
            with open(TRADE_LOG_PATH) as f:
                lines = f.readlines()
            trades = []
            for line in lines[-count * 2:]:  # read last 2x count lines to be safe
                line = line.strip()
                if not line:
                    continue
                try:
                    trades.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            return trades[-count:]
        except Exception as e:
            print(f"[Remote] _load_recent_trades failed: {e}")
            return []

    def _push_state_to_vps(self):
        if not VPS_SYNC_ENABLED or not VPS_SYNC_URL:
            return
        now_ms = int(time.time() * 1000)
        if now_ms - self.last_state_push < 60000:
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
            "current_balance": self.current_balance,
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
            "sl_cooldown_until": self.sl_cooldown_until,
            "consecutive_losses": self.consecutive_losses,
            "current_sl": self.current_sl,
            "ema": self.ema,
            "atr": self.atr,
            "adx": self.adx,
            "symbol_name": SYMBOL,  # for multi-bot routing on VPS server
            "timestamp": now_ms,
        }
        try:
            req = urllib.request.Request(
                f"{VPS_SYNC_URL}/api/state",
                data=json.dumps(state).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {VPS_AUTH_TOKEN}"},
                method="POST")
            with urllib.request.urlopen(req, timeout=5) as r:
                result = json.loads(r.read())
            if not result.get("ok"):
                self.vps_sync_failures += 1
        except Exception:
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
                 "target_hit": self.target_hit, "sl_cooldown_until": self.sl_cooldown_until, "consecutive_losses": self.consecutive_losses, "current_sl": self.current_sl,
                 "initial_balance": self.initial_balance,
                 "last_scale_in_time": self.last_scale_in_time,
            "balance_at_position_open": self.balance_at_position_open,
                 "consec_pause_until": self._consec_pause_until}
        tmp = STATE_FILE_PATH + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, STATE_FILE_PATH)
        except Exception as e:
            print(f"[Remote] State save failed: {e}")

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
            self.entry_atr = state.get("entry_atr", 0.0) or self.atr
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
            self.sl_cooldown_until = state.get("sl_cooldown_until", 0)
            self.consecutive_losses = state.get("consecutive_losses", 0)
            self.current_sl = state.get("current_sl", 0.0)
            self.initial_balance = state.get("initial_balance")
            self.last_scale_in_time = state.get("last_scale_in_time", 0)
            self.balance_at_position_open = state.get("balance_at_position_open", 0.0)
            self._consec_pause_until = state.get("consec_pause_until", 0)
            # Load recent trades from trade log file (for consecutive loss pause)
            self.recent_trades = self._load_recent_trades(10)
            if self.entries:
                print(f"[Remote] State restored: {len(self.entries)} entries, "
                      f"{'SHORT' if self.is_short else 'LONG'} @ {self.avg_price:.2f}")
        except Exception as e:
            print(f"[Remote] State load failed: {e}")

    # ─── Position sync ─────────────────────────────────────────────

    async def sync_position(self):
        """Sync self.entries with actual cTrader positions."""
        pos_data = await self.get_positions_raw()
        our_positions = []
        if pos_data:
            for p in pos_data.get("positions", []):
                if p.get("symbolId") == self.symbol_id:
                    our_positions.append(p)

        if not our_positions and self.has_position:
            print(f"[Remote] Position closed externally — recording trade")
            entry_price = self.avg_price if self.entries else 0
            entries_used = len(self.entries)
            total_vol_lots = sum(e.get("volume_lots", 0) for e in self.entries) if self.entries else 0
            exit_price = self.close_prices[-1] if self.close_prices else 0
            bal_now = await self.get_balance_raw()
            bal_val = bal_now.get("balance", 0) if bal_now else 0
            # Fixed PnL calc (bug #57, 2026-07-20):
            # Primary: use balance snapshot at position open (includes commissions)
            # Fallback: compute from entry/exit prices * volume * lot_size
            if self.balance_at_position_open > 0:
                pnl = bal_val - self.balance_at_position_open
                print(f"[Remote] PnL calc: ${bal_val:.2f} - ${self.balance_at_position_open:.2f} (snapshot)")
            elif entry_price > 0 and exit_price > 0 and total_vol_lots > 0:
                if self.is_short:
                    pnl = (entry_price - exit_price) * total_vol_lots * self.lot_size
                else:
                    pnl = (exit_price - entry_price) * total_vol_lots * self.lot_size
                print(f"[Remote] PnL calc: price-based fallback (entry=${entry_price:.2f} exit=${exit_price:.2f} vol={total_vol_lots} lot_size={self.lot_size})")
            else:
                pnl = 0
                print(f"[Remote] PnL calc: no data, recording 0")
            if entry_price > 0:
                self.total_pnl += pnl
                self._write_trade("EXTERNAL_CLOSE", entry_price, exit_price, pnl, entries_used)
                print(f"[Remote] External close recorded | PnL: ${pnl:+.2f} | total: ${self.total_pnl:+.2f}")
                if pnl < 0 and COOLDOWN_AFTER_SL > 0:
                    self.consecutive_losses = getattr(self, 'consecutive_losses', 0) + 1
                    cooldown = min(COOLDOWN_AFTER_SL * (2 ** (self.consecutive_losses - 1)), 3600)  # 30m → 60m, max 60m
                    self.sl_cooldown_until = int(time.time()) + cooldown
                    print(f"[Remote] SL cooldown {cooldown}s (consecutive loss #{self.consecutive_losses})")
                elif pnl >= 0:
                    self.consecutive_losses = 0
            self.entries = []
            self.closed_half = False
            self.current_sl = 0.0
            self.extreme_price = 0.0
            self.balance_at_position_open = 0.0  # reset for next trade
            self._save_state()

    async def sync_position_ids(self):
        """Update position_id in self.entries from cTrader."""
        pos_data = await self.get_positions_raw()
        if not pos_data:
            return
        ctrader_positions = [p for p in pos_data.get("positions", [])
                             if p.get("symbolId") == self.symbol_id]
        if not ctrader_positions:
            return
        ctrader_positions.sort(key=lambda p: p.get("positionId", 0))
        for i, entry in enumerate(self.entries):
            if i < len(ctrader_positions):
                pid = ctrader_positions[i].get("positionId")
                if pid and entry.get("position_id") != pid:
                    entry["position_id"] = pid
                    print(f"[Remote] Synced position_id for entry #{i+1}: {pid}")

    async def get_position_ids(self):
        ids = []
        for e in self.entries:
            pid = e.get("position_id")
            if pid:
                ids.append(pid)
        return ids

    async def amend_sl_on_all_positions(self, new_sl_price: float):
        """Legacy: amend SL to one price for all positions. Kept for backward compat.
        New code should use amend_sl_per_position() instead — gives each entry its own SL."""
        if DRY_RUN:
            print(f"[Remote] DRY RUN — would amend SL to ${new_sl_price:.2f} on all positions")
            self.current_sl = new_sl_price
            return
        pids = await self.get_position_ids()
        if not pids:
            print(f"[Remote] amend_sl: no position_ids, can't amend")
            return
        # Use stored TP per entry — if not stored, calculate from entry price
        tp_offset = self.entry_atr * TP2_ATR_MULT if self.entry_atr > 0 else self.atr * TP2_ATR_MULT
        for i, entry in enumerate(self.entries):
            pid = entry.get("position_id")
            if not pid:
                continue
            tp_price = entry.get("tp_price", 0)
            is_last = (i == len(self.entries) - 1)
            # If no stored TP, calculate it (skip for last entry ??? rides trend)
            if tp_price <= 0 and not is_last:
                entry_price = entry.get("price", 0)
                if entry_price > 0:
                    if not self.is_short:
                        tp_price = entry_price + tp_offset
                    else:
                        tp_price = entry_price - tp_offset
                    entry["tp_price"] = tp_price  # save for next time
                    print(f"[Remote] Calculated missing TP=${tp_price:.3f} for pos {pid}")
            if is_last and tp_price <= 0:
                print(f"[Remote] Last entry (pos {pid}) ??? no TP, riding trend")
            try:
                if tp_price > 0:
                    result = await self.amend_position(pid, stop_loss=new_sl_price, take_profit=tp_price)
                    if result is None:
                        print(f"[Remote] amend_position returned error for {pid} - SL/TP not updated")
                    else:
                        print(f"[Remote] Amended SL=${new_sl_price:.3f} TP=${tp_price:.3f} on pos {pid}")
                else:
                    result = await self.amend_position(pid, stop_loss=new_sl_price, take_profit=None)
                    if result is None:
                        print(f"[Remote] amend_position returned error for {pid} - SL not updated")
                    else:
                        print(f"[Remote] Amended SL=${new_sl_price:.3f} (no TP ??? ride trend)")
            except Exception as e:
                print(f"[Remote] amend_position failed for {pid}: {e}")
                success = False
        self.current_sl = new_sl_price

    async def amend_sl_per_position(self, sl_per_entry: dict):
        """Amend SL independently for each entry. sl_per_entry = {position_id: sl_price}.
        Each entry has its own extreme_price and SL — prevents all stops from clustering
        at one level (which is what market makers target)."""
        if DRY_RUN:
            print(f"[Remote] DRY RUN — would amend SL per position: {sl_per_entry}")
            return
        # Use stored TP per entry
        tp_offset = self.entry_atr * TP2_ATR_MULT if self.entry_atr > 0 else self.atr * TP2_ATR_MULT
        max_iter = len(self.entries)
        for i, entry in enumerate(self.entries):
            pid = entry.get("position_id")
            if not pid:
                continue
            new_sl = sl_per_entry.get(pid)
            if new_sl is None:
                continue
            # Update stored sl_price in entry
            entry["sl_price"] = new_sl
            tp_price = entry.get("tp_price", 0)
            is_last = (i == max_iter - 1)
            # If no stored TP, calculate it (skip for last entry — rides trend)
            if tp_price <= 0 and not is_last:
                entry_price = entry.get("price", 0)
                if entry_price > 0:
                    if not self.is_short:
                        tp_price = entry_price + tp_offset
                    else:
                        tp_price = entry_price - tp_offset
                    entry["tp_price"] = tp_price
                    print(f"[Remote] Calculated missing TP=${tp_price:.3f} for pos {pid}")
            if is_last and tp_price <= 0:
                print(f"[Remote] Last entry (pos {pid}) — no TP, riding trend")
            try:
                if tp_price > 0:
                    result = await self.amend_position(pid, stop_loss=new_sl, take_profit=tp_price)
                    if result is None:
                        print(f"[Remote] amend_position returned error for {pid} - SL/TP not updated")
                    else:
                        print(f"[Remote] Amended SL=${new_sl:.3f} TP=${tp_price:.3f} on pos {pid}")
                else:
                    result = await self.amend_position(pid, stop_loss=new_sl, take_profit=None)
                    if result is None:
                        print(f"[Remote] amend_position returned error for {pid} - SL not updated")
                    else:
                        print(f"[Remote] Amended SL=${new_sl:.3f} (no TP — ride trend) on pos {pid}")
            except Exception as e:
                print(f"[Remote] amend_position failed for {pid}: {e}")
        # Update current_sl to the TIGHTEST SL across all entries (for tracking)
        if sl_per_entry.values():
            if not self.is_short:
                self.current_sl = max(sl_per_entry.values())  # LONG: highest SL = tightest
            else:
                self.current_sl = min(sl_per_entry.values())  # SHORT: lowest SL = tightest

    # ─── Entry management ──────────────────────────────────────────

    async def open_entry(self, side, price, balance):
        """Single entry with fixed SL/TP. No scale-in, no last-entry logic."""
        self.last_scale_in_time = int(time.time() * 1000)
        self.is_short = side == "sell"
        self.entry_time = int(time.time() * 1000)
        self.extreme_price = price
        self.closed_half = False
        self.entries = []

        vol = ENTRY_VOLUMES if isinstance(ENTRY_VOLUMES, (int, float)) else ENTRY_VOLUMES[0]
        print(f"[Remote] Entry: {side.upper()} {vol} lots @ ${price:.2f}")

        # Fixed SL/TP in USD (not ATR-based)
        if not self.is_short:
            sl_price = price - FIXED_SL_USD
            tp_price = price + FIXED_TP_USD
        else:
            sl_price = price + FIXED_SL_USD
            tp_price = price - FIXED_TP_USD

        order = await self.place_market_order(side, vol, sl_price, tp_price)

        if order:
            position_id = None
            if isinstance(order, dict) and not order.get("dry_run"):
                position_id = order.get("positionId") or order.get("position_id")
            self.entries.append({"price": price, "volume_lots": vol, "position_id": position_id,
                                 "tp_price": tp_price, "sl_price": sl_price,
                                 "extreme_price": price, "be_triggered": False})
            self.current_sl = sl_price
            self.balance_at_position_open = self.current_balance  # snapshot for PnL calc
            self.last_entry_minute = datetime.now().minute
            print(f"[Remote] Entry done | vol={vol} pos_id={position_id} SL=${sl_price:.2f} TP=${tp_price:.2f}")
            await asyncio.sleep(2)
            # Amend if TP/SL not set
            if position_id and not DRY_RUN:
                pos = order.get("position", {}) if isinstance(order, dict) else {}
                if not pos.get("takeProfit") or pos.get("takeProfit") == 0:
                    await self.amend_position(position_id, stop_loss=sl_price, take_profit=tp_price)
                    print(f"[Remote] Amended SL=${sl_price:.3f} TP=${tp_price:.3f} on pos {position_id}")
            if not DRY_RUN:
                await self.sync_position_ids()
        else:
            print(f"[Remote] Entry failed")

    async def manage_position(self, price, balance=0):
        if not self.has_position:
            return
        avg = self.avg_price

        if not all(e.get("position_id") for e in self.entries) and not DRY_RUN:
            await self.sync_position_ids()

        # RSI EXIT — close when RSI crosses back to 50
        if len(self.close_prices) >= 15:
            if should_exit_rsi(self.close_prices, self.is_short, RSI_EXIT):
                rsi = calc_rsi(self.close_prices, 14)
                print(f"[Remote] RSI EXIT: rsi={rsi:.1f} crossed {RSI_EXIT} — closing position")
                await self.close_all("RSI_EXIT")
                return

        # Fixed SL/TP handled by cTrader (set at entry). No trailing, no BE.
        # Just sync positions to detect external closes (SL/TP hit by broker).

    async def close_all(self, reason):
        entry_price = self.avg_price if self.entries else 0
        entries_used = len(self.entries)
        if reason == "TP":
            self.consecutive_tp += 1
            if self.consecutive_tp >= 3:
                self.tp_cooldown_until = int(time.time()) + 30 * 60
        else:
            self.consecutive_tp = 0
        print(f"[Remote] Closing all — {reason}")
        bal_before = await self.get_balance_raw()
        await self.close_all_positions()
        if not DRY_RUN:
            await asyncio.sleep(2)
        bal_after = await self.get_balance_raw()
        exit_price = self.close_prices[-1] if self.close_prices else 0
        # Calculate PnL from entry/exit prices (reliable, not dependent on balance update delay)
        pnl = 0.0
        if entry_price > 0 and exit_price > 0:
            for e in self.entries:
                e_price = e.get("price", 0)
                e_vol = e.get("volume_lots", 0)
                if self.is_short:
                    pnl += (e_price - exit_price) * e_vol * self.lot_size
                else:
                    pnl += (exit_price - e_price) * e_vol * self.lot_size
        # Fallback to balance delta if price calc failed
        if pnl == 0.0 and bal_before and bal_after:
            pnl = bal_after.get("balance", 0) - bal_before.get("balance", 0)
        if entry_price > 0:
            self.total_pnl += pnl
            self._write_trade(reason, entry_price, exit_price, pnl, entries_used)
            print(f"[Remote] Closed {entries_used} entries | PnL: ${pnl:+.2f} | total: ${self.total_pnl:+.2f}")
        self.entries = []
        self.closed_half = False
        self.current_sl = 0.0
        self.extreme_price = 0.0
        self.last_scale_in_time = 0
        # SL cooldown: trigger on negative PnL, with escalation for consecutive losses
        if pnl < 0 and COOLDOWN_AFTER_SL > 0:
            self.consecutive_losses = getattr(self, 'consecutive_losses', 0) + 1
            cooldown = min(COOLDOWN_AFTER_SL * (2 ** (self.consecutive_losses - 1)), 3600)  # 30m → 60m, max 60m
            self.sl_cooldown_until = int(time.time()) + cooldown
            print(f"[Remote] SL cooldown {cooldown}s (consecutive loss #{self.consecutive_losses})")
        elif pnl >= 0:
            self.consecutive_losses = 0
            # Reset consecutive loss pause on profitable trade
            self._consec_pause_until = 0
            print(f"[Remote] Profitable trade — consec loss pause reset")
        self._save_state()


async def main():
    bot = GoldMCPRemoteBot()
    try:
        await bot.run()
    finally:
        await bot.client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
