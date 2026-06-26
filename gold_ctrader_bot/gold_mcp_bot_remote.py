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

from strategy import should_enter, calc_ema, calc_atr, calc_adx

load_dotenv()

# === MCP config ===
MCP_URL = os.getenv("MCP_URL", "https://mcp.ctrader.com/trading/mcp")
MCP_BEARER_TOKEN = os.getenv("MCP_BEARER_TOKEN", "")

# === Strategy config (identical to gold_mcp_bot.py) ===
SYMBOL = os.getenv("SYMBOL_NAME", "XAUUSD")
MIN_INTERVAL_MINUTES = int(os.getenv("MIN_INTERVAL_MINUTES", "60"))
MAX_DAILY_LOSS_PERCENT = float(os.getenv("MAX_LOSS_PERCENT", "3.0"))
TIMEFRAME = os.getenv("TIMEFRAME", "M_5")  # Remote MCP uses M_5 not m5
CANDLE_COUNT = int(os.getenv("CANDLE_COUNT", "100"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
RECONNECT_DELAY = int(os.getenv("RECONNECT_DELAY", "5"))
MAX_RECONNECT_DELAY = int(os.getenv("MAX_RECONNECT_DELAY", "300"))

ENTRY_VOLUMES = [float(x) for x in os.getenv("ENTRY_VOLUMES", "0.01,0.01,0.01").split(",")]
MAX_ENTRIES = int(os.getenv("MAX_ENTRIES", "3"))
SL_ATR_MULT = float(os.getenv("SL_ATR_MULT", "2.0"))
TP1_ATR_MULT = float(os.getenv("TP1_ATR_MULT", "1.5"))
TP2_ATR_MULT = float(os.getenv("TP2_ATR_MULT", "4.0"))
TRAIL_ACTIVATE_PCT = float(os.getenv("TRAIL_ACTIVATE_PCT", "0.5"))
TIME_EXIT_HOURS = float(os.getenv("TIME_EXIT_HOURS", "4"))
BE_TRIGGER_PCT = float(os.getenv("BE_TRIGGER_PCT", "0.5"))
BE_OFFSET_ATR = float(os.getenv("BE_OFFSET_ATR", "0.0"))

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
    """Check if XAUUSD market is open (PipFarm cTrader hours, MSK timezone).
    
    Trading hours (with safety buffer):
    - Mon-Thu: 01:15 - 23:45 MSK
    - Fri: 01:15 - 23:45 MSK (earlier close for safety)
    - Sat, Sun: closed
    
    Note: Real market hours are 01:01-23:59, but we use 01:15-23:45
    to avoid edge cases near session breaks.
    """
    now = datetime.now(MSK)
    weekday = now.weekday()  # 0=Mon, 6=Sun
    
    # Saturday (5) or Sunday (6) — closed
    if weekday == 5 or weekday == 6:
        return False
    
    # Mon-Fri: check time
    hour_min = now.hour * 60 + now.minute
    open_min = 1 * 60 + 15    # 01:15 = 75 minutes
    close_min = 23 * 60 + 45  # 23:45 = 1425 minutes (same for all weekdays)
    
    return open_min <= hour_min <= close_min


def market_status_str() -> str:
    """Return human-readable market status."""
    now = datetime.now(MSK)
    weekday = now.weekday()
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    if not is_market_open():
        if weekday == 5:  # Saturday
            return f"CLOSED (weekend, opens Mon 01:15 MSK)"
        elif weekday == 6:  # Sunday
            return f"CLOSED (weekend, opens Mon 01:15 MSK)"
        elif weekday == 4 and now.hour >= 23:  # Friday after close
            return f"CLOSED (weekend, opens Mon 01:15 MSK)"
        else:
            # Weekday night pause (23:45 - 01:15)
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

        # Challenge tracking
        self.total_pnl = 0.0
        self.trading_days = set()
        self.target_hit = False

        # SL tracker
        self.current_sl = 0.0
        self.initial_balance = None
        self.last_scale_in_time = 0

        # Current balance (updated each tick from cTrader)
        self.current_balance = 0.0

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
        """Send MCP request, parse SSE response, return parsed JSON or None."""
        req_id = int(time.time() * 1000) % 100000
        body = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params:
            body["params"] = params
        headers = self._mcp_headers()
        try:
            resp = await self.client.post(MCP_URL, json=body, headers=headers)
            resp.raise_for_status()
        except Exception as e:
            print(f"[Remote] HTTP error in {method}: {e}")
            return None

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

        print(f"[Remote] Bot | {SYMBOL} | entries={ENTRY_VOLUMES} lots "
              f"| max={MAX_ENTRIES} | SL={SL_ATR_MULT}atr | TP1={TP1_ATR_MULT}atr TP2={TP2_ATR_MULT}atr")
        print(f"[Remote] BE trigger={BE_TRIGGER_PCT}% | Time exit={TIME_EXIT_HOURS}h | Scale-in cooldown=5min")

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
                "sl_price": sl_price
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
        if now - self.entry_time < MIN_INTERVAL_MINUTES * 60 * 1000 and self.entry_time > 0:
            return

        # 7. Strategy signal
        enter, reason = should_enter(self.close_prices, self.high_prices, self.low_prices)
        if not enter:
            return
        if self.adx > 0 and self.adx < 20:
            print(f"[Remote] ADX {self.adx:.1f} < 20 — skip")
            return

        # 8. First entry
        side = "sell" if "SHORT" in reason else "buy"
        await self.open_entry(side, price, balance)

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
        # 100 M5 candles = 500 minutes ≈ 8.3 hours
        from_dt = now - timedelta(hours=10)
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
        # Parse pipettes → display prices
        self.close_prices = [pipettes_to_price(int(b.get("close", b.get("c", 0))), self.pip_digits) for b in bars]
        self.high_prices = [pipettes_to_price(int(b.get("high", b.get("h", 0))), self.pip_digits) for b in bars]
        self.low_prices = [pipettes_to_price(int(b.get("low", b.get("l", 0))), self.pip_digits) for b in bars]
        print(f"[Remote] Fetched {len(bars)} candles | last close=${self.close_prices[-1]:.2f}")

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
        - BTCUSD: max 3 digits after decimal"""
        sl_tp_digits = getattr(self, 'money_digits', 2) or 2
        args = {"positionId": position_id}
        if stop_loss is not None:
            args["stopLoss"] = round(stop_loss, sl_tp_digits)
        if take_profit is not None:
            args["takeProfit"] = round(take_profit, sl_tp_digits)

        if DRY_RUN:
            print(f"[Remote] DRY RUN — would call amend_position: {json.dumps(args)}")
            return {"dry_run": True}

        return await self.call("amend_position", args)

    async def close_position(self, position_id, volume_cents=None):
        """Close position (full or partial by volume)."""
        args = {"positionId": position_id}
        if volume_cents is not None:
            args["volume"] = volume_cents

        if DRY_RUN:
            print(f"[Remote] DRY RUN — would call close_position: {json.dumps(args)}")
            return {"dry_run": True}

        return await self.call("close_position", args)

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
                 "target_hit": self.target_hit, "current_sl": self.current_sl,
                 "initial_balance": self.initial_balance,
                 "last_scale_in_time": self.last_scale_in_time}
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
            self.current_sl = state.get("current_sl", 0.0)
            self.initial_balance = state.get("initial_balance")
            self.last_scale_in_time = state.get("last_scale_in_time", 0)
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
            exit_price = self.close_prices[-1] if self.close_prices else 0
            bal_now = await self.get_balance_raw()
            if bal_now and self.daily_start_balance:
                bal_val = bal_now.get("balance", 0)
                pnl = bal_val - self.daily_start_balance - self.total_pnl
            else:
                pnl = 0
            if entry_price > 0:
                self.total_pnl += pnl
                self._write_trade("EXTERNAL_CLOSE", entry_price, exit_price, pnl, entries_used)
                print(f"[Remote] External close recorded | PnL: ${pnl:+.2f} | total: ${self.total_pnl:+.2f}")
            self.entries = []
            self.closed_half = False
            self.current_sl = 0.0
            self.extreme_price = 0.0
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
        else:
            # Scale-in: preserve extreme_price and current_sl, do NOT reset them
            pass

        entry_idx = len(self.entries)
        if entry_idx >= MAX_ENTRIES:
            print(f"[Remote] Max entries ({MAX_ENTRIES}) reached")
            return

        vol = self.entry_volume(entry_idx)
        print(f"[Remote] Entry #{entry_idx + 1}: {side.upper()} {vol} lots @ ${price:.2f}")

        # Every entry gets SL; last entry gets NO TP (ride the trend)
        atr_for_sl = self.atr if self.atr > 0 else (self.entry_atr if self.entry_atr > 0 else 10)
        is_last_entry = (entry_idx == MAX_ENTRIES - 1)
        if not self.is_short:
            sl_price = price - atr_for_sl * SL_ATR_MULT
            tp_price = price + atr_for_sl * TP2_ATR_MULT
        else:
            sl_price = price + atr_for_sl * SL_ATR_MULT
            tp_price = price - atr_for_sl * TP2_ATR_MULT

        # Last entry: no TP — ride the trend with trailing SL only
        if is_last_entry:
            print(f"[Remote] Last entry — no TP, riding trend with trailing SL")
            order = await self.place_market_order(side, vol, sl_price, None)
        else:
            order = await self.place_market_order(side, vol, sl_price, tp_price)

        if order:
            position_id = None
            if isinstance(order, dict) and not order.get("dry_run"):
                position_id = order.get("positionId") or order.get("position_id")
            # Store tp_price=0 for last entry (no TP, rides trend)
            stored_tp = tp_price if not is_last_entry else 0
            self.entries.append({"price": price, "volume_lots": vol, "position_id": position_id, "tp_price": stored_tp, "sl_price": sl_price})
            self.last_entry_minute = datetime.now().minute
            tp_str = f"${tp_price:.2f}" if not is_last_entry else "NONE (ride trend)"
            print(f"[Remote] Entry #{entry_idx + 1} done | avg={self.avg_price:.2f} vol={self.total_volume:.2f} "
                  f"pos_id={position_id} SL=${sl_price:.2f} TP={tp_str}")
            await asyncio.sleep(2)
            # Check if TP/SL were actually set, amend if not
            if position_id and not DRY_RUN:
                pos = order.get("position", {}) if isinstance(order, dict) else {}
                actual_tp = pos.get("takeProfit")
                actual_sl = pos.get("stopLoss")
                needs_amend = False
                if not actual_tp or actual_tp == 0:
                    if not is_last_entry and (not actual_tp or actual_tp == 0):
                        print(f"[Remote] TP missing ??? will amend to ${tp_price:.3f}")
                        needs_amend = True
                    elif is_last_entry and (not actual_tp or actual_tp == 0):
                        print(f"[Remote] Last entry ??? TP skipped intentionally, trailing SL only")
                    needs_amend = True
                if not actual_sl or actual_sl == 0:
                    print(f"[Remote] SL missing — will amend to ${sl_price:.3f}")
                    needs_amend = True
                if needs_amend:
                    await self.amend_position(position_id, stop_loss=sl_price, take_profit=tp_price)
                    print(f"[Remote] Amended SL=${sl_price:.3f} TP=${tp_price:.3f} on pos {position_id}")
            # Set initial current_sl ??? only if higher (LONG) or lower (SHORT) than existing
            if not self.entries or len(self.entries) <= 1:
                self.current_sl = sl_price
            elif self.is_short:
                if sl_price < self.current_sl:
                    self.current_sl = sl_price
            else:
                if sl_price > self.current_sl:
                    self.current_sl = sl_price
            print(f"[Remote] Initial SL set: ${self.current_sl:.2f}")
            if not DRY_RUN:
                await self.sync_position_ids()
        else:
            print(f"[Remote] Entry #{entry_idx + 1} failed")

    async def manage_position(self, price, balance=0):
        if not self.has_position:
            return
        avg = self.avg_price

        if not all(e.get("position_id") for e in self.entries) and not DRY_RUN:
            await self.sync_position_ids()

        # Trailing SL
        if not self.is_short:
            if price > self.extreme_price:
                self.extreme_price = price
                td = self.atr * SL_ATR_MULT
                new_sl = self.extreme_price - td
                if self.current_sl == 0 or new_sl > self.current_sl:
                    await self.amend_sl_on_all_positions(new_sl)
        else:
            if price < self.extreme_price:
                self.extreme_price = price
                td = self.atr * SL_ATR_MULT
                new_sl = self.extreme_price + td
                if self.current_sl == 0 or new_sl < self.current_sl:
                    await self.amend_sl_on_all_positions(new_sl)

        # Break-even
        pnl_pct = self.get_current_pnl_pct(price)
        if pnl_pct >= BE_TRIGGER_PCT:
            if not self.is_short:
                be_sl = avg + self.atr * BE_OFFSET_ATR
                if self.current_sl == 0 or be_sl > self.current_sl:
                    await self.amend_sl_on_all_positions(be_sl)
                    print(f"[Remote] BREAK-EVEN: PnL {pnl_pct:.2f}% >= {BE_TRIGGER_PCT}% SL=${be_sl:.2f} (entry=${avg:.2f})")
            else:
                be_sl = avg - self.atr * BE_OFFSET_ATR
                if self.current_sl == 0 or be_sl < self.current_sl:
                    await self.amend_sl_on_all_positions(be_sl)
                    print(f"[Remote] BREAK-EVEN: PnL {pnl_pct:.2f}% >= {BE_TRIGGER_PCT}% SL=${be_sl:.2f} (entry=${avg:.2f})")

        # TP1: close first position fully (Hedged mode — each position is separate)
        tp1_price = self.get_tp1_price(price)
        if not self.closed_half:
            hit = (not self.is_short and price >= tp1_price) or (self.is_short and price <= tp1_price)
            if hit:
                print(f"[Remote] TP1 hit @ ${price:.2f} — closing first position")
                if not DRY_RUN:
                    # Close first position fully (can't close 50% across positions in Hedged mode)
                    if self.entries:
                        first_pid = self.entries[0].get("position_id")
                        first_vol = self.entries[0].get("volume_lots", 0)
                        if first_pid:
                            await self.close_position_partial(first_pid, first_vol)
                            print(f"[Remote] Closed pos {first_pid} ({first_vol} lots)")
                self.closed_half = True
                self._write_trade("TP1", self.avg_price, price, 0, len(self.entries))
                self._save_state()
                return

        # TP2: close entries that HAVE TP (not the last entry which rides the trend)
        tp2_price = self.get_tp2_price(price)
        hit_tp = (not self.is_short and price >= tp2_price) or (self.is_short and price <= tp2_price)
        if hit_tp:
            # Close entries with tp_price > 0, keep entries without TP (last entry)
            entries_with_tp = [e for e in self.entries if e.get("tp_price", 0) > 0]
            entries_without_tp = [e for e in self.entries if e.get("tp_price", 0) <= 0]
            if entries_with_tp:
                print(f"[Remote] TP2 hit @ ${price:.2f} — closing {len(entries_with_tp)} entries with TP")
                if not DRY_RUN:
                    for entry in entries_with_tp:
                        pid = entry.get("position_id")
                        vol = entry.get("volume_lots", 0)
                        if pid:
                            await self.close_position_partial(pid, vol)
                            print(f"[Remote] Closed pos {pid} ({vol} lots)")
                self._write_trade("TP2", self.avg_price, price, 0, len(entries_with_tp))
                self.entries = entries_without_tp  # keep only last entry (no TP)
                self._save_state()
                if not self.entries:
                    # No entries left — all closed
                    self.has_position = False
                    self.closed_half = False
                    self.current_sl = 0.0
                    self.extreme_price = 0.0
                    self.last_scale_in_time = 0
                    self._save_state()
                    return
                print(f"[Remote] {len(self.entries)} entry(ies) remaining — riding trend")
            return

        # Scale-in with cooldown
        SCALE_IN_COOLDOWN_SEC = 300
        if len(self.entries) < MAX_ENTRIES and not self.closed_half:
            now_ms = int(time.time() * 1000)
            time_since_last_scale = (now_ms - self.last_scale_in_time) / 1000 if self.last_scale_in_time > 0 else 999
            if time_since_last_scale >= SCALE_IN_COOLDOWN_SEC:
                pnl_pct = self.get_current_pnl_pct(price)
                if pnl_pct > -0.5:
                    distance_ok = abs(price - avg) >= 0.5 * self.atr
                    not_overextended = abs(price - self.ema) < 1.5 * self.atr
                    can_scale = (self.is_short and price >= self.ema and price > avg) or \
                                (not self.is_short and price <= self.ema and price < avg)
                    if can_scale and distance_ok and not_overextended:
                        self.last_scale_in_time = now_ms
                        await self.open_entry("sell" if self.is_short else "buy", price, balance)

        # Time exit — skip for last entry (ride the trend)
        has_tp_entries = any(e.get("tp_price", 0) > 0 for e in self.entries)
        if has_tp_entries:
            hrs = (int(time.time() * 1000) - self.entry_time) / 3600000
            if hrs >= TIME_EXIT_HOURS and abs(pnl_pct) < 1:
                print(f"[Remote] Time exit ({hrs:.1f}h, PnL {pnl_pct:.2f}%)")
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
        print(f"[Remote] Closing all — {reason}")
        bal_before = await self.get_balance_raw()
        await self.close_all_positions()
        if not DRY_RUN:
            await asyncio.sleep(2)
        bal_after = await self.get_balance_raw()
        exit_price = self.close_prices[-1] if self.close_prices else 0
        pnl = 0.0
        if bal_before and bal_after:
            pnl = bal_after.get("balance", 0) - bal_before.get("balance", 0)
        elif bal_after:
            pnl = bal_after.get("balance", 0) - (self.daily_start_balance or 0) - self.total_pnl
        if entry_price > 0:
            self.total_pnl += pnl
            self._write_trade(reason, entry_price, exit_price, pnl, entries_used)
            print(f"[Remote] Closed {entries_used} entries | PnL: ${pnl:+.2f} | total: ${self.total_pnl:+.2f}")
        self.entries = []
        self.closed_half = False
        self.current_sl = 0.0
        self.extreme_price = 0.0
        self.last_scale_in_time = 0
        self._save_state()


async def main():
    bot = GoldMCPRemoteBot()
    try:
        await bot.run()
    finally:
        await bot.client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
