#!/usr/bin/env python3
"""Voice Trade Bot for GOLD cTrader.

Telegram voice message → STT (faster-whisper local) → command → cTrader MCP API.

Commands (RU/EN):
- "купи" / "buy" / "long"  → if has SHORT, close it. If flat, open LONG.
- "продай" / "sell" / "short" → if has LONG, close it. If flat, open SHORT.
- "закрой" / "close" / "flat" → close any open position.
- "статус" / "status" → show current position.

Security:
- Only authorized chat IDs can send commands.
- Confirmation required before opening new position.
- All commands logged to /root/bots/logs/voice_trade.log

Position params (from GOLD .env):
- Volume: 0.7 lot
- SL: $25 USD from entry
- TP: $35 USD from entry
"""
import os
import sys
import json
import time
import asyncio
import logging
import tempfile
import urllib.request
from pathlib import Path
from datetime import datetime, timezone, timedelta

import httpx
from dotenv import load_dotenv

# Load GOLD .env for MCP config
GOLD_ENV_PATH = "/root/Goldctraderbot/gold_ctrader_bot/.env"
load_dotenv(GOLD_ENV_PATH)

# === Config ===
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8664275234:AAHUHIdruK4FWjIioRwtqU1PGQeceyeBk-g")
AUTHORIZED_CHAT_IDS = [int(x) for x in os.getenv("AUTHORIZED_CHAT_IDS", "354703083").split(",")]

MCP_URL = os.getenv("MCP_URL", "https://mcp.ctrader.com/trading/mcp")
MCP_BEARER_TOKEN = os.getenv("MCP_BEARER_TOKEN", "")
SYMBOL_ID = int(os.getenv("SYMBOL_ID", "41"))  # XAUUSD
LOT_SIZE = int(os.getenv("LOT_SIZE", "100"))
PIP_DIGITS = int(os.getenv("PIP_DIGITS", "5"))

# Position params
DEFAULT_VOLUME = float(os.getenv("VOICE_TRADE_VOLUME", "0.7"))
SL_USD = float(os.getenv("FIXED_SL_USD", "25"))
TP_USD = float(os.getenv("FIXED_TP_USD", "35"))

# Whisper model (small: 244 MB, fast on CPU, supports RU+EN)
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE = os.getenv("WHISPER_COMPUTE", "int8")

# === Logging ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/root/bots/logs/voice_trade.log"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("voice_trade_bot")

# === Moscow timezone ===
MSK = timezone(timedelta(hours=3))


# === cTrader MCP client (minimal, reuses GOLD bot logic) ===
class CTraderMCP:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=60)
        self.session_id = None
        self.tool_names = {}
        self.accept_header = "application/json, text/event-stream"

    async def connect(self):
        """Initialize MCP session."""
        # Initialize
        resp = await self.client.post(
            MCP_URL,
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2024-11-05",
                             "capabilities": {},
                             "clientInfo": {"name": "voice-trade-bot", "version": "1.0"}}},
            headers={"Authorization": f"Bearer {MCP_BEARER_TOKEN}",
                     "Accept": self.accept_header,
                     "Content-Type": "application/json"},
        )
        if resp.status_code != 200:
            log.error(f"Initialize failed: HTTP {resp.status_code}")
            return False
        self.session_id = resp.headers.get("mcp-session-id")
        if not self.session_id:
            log.error("No mcp-session-id in response headers")
            return False

        # Send initialized notification
        await self.client.post(
            MCP_URL,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={"Authorization": f"Bearer {MCP_BEARER_TOKEN}",
                     "Accept": self.accept_header,
                     "Content-Type": "application/json",
                     "mcp-session-id": self.session_id},
        )

        # List tools
        resp = await self.client.post(
            MCP_URL,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            headers={"Authorization": f"Bearer {MCP_BEARER_TOKEN}",
                     "Accept": self.accept_header,
                     "Content-Type": "application/json",
                     "mcp-session-id": self.session_id},
        )
        # Parse SSE response
        for line in resp.text.split("\n"):
            if line.startswith("data: "):
                data = json.loads(line[6:])
                if "result" in data and "tools" in data["result"]:
                    for tool in data["result"]["tools"]:
                        self.tool_names[tool["name"]] = tool
                break

        log.info(f"Connected to cTrader MCP. {len(self.tool_names)} tools available.")
        return True

    async def call(self, tool_name, args=None):
        """Call MCP tool."""
        resp = await self.client.post(
            MCP_URL,
            json={"jsonrpc": "2.0", "id": int(time.time() * 1000) % 1000000,
                  "method": "tools/call",
                  "params": {"name": tool_name, "arguments": args or {}}},
            headers={"Authorization": f"Bearer {MCP_BEARER_TOKEN}",
                     "Accept": self.accept_header,
                     "Content-Type": "application/json",
                     "mcp-session-id": self.session_id},
        )
        # Parse SSE
        for line in resp.text.split("\n"):
            if line.startswith("data: "):
                data = json.loads(line[6:])
                if "result" in data:
                    content = data["result"].get("content", [])
                    if content and len(content) > 0:
                        try:
                            return json.loads(content[0]["text"])
                        except (json.JSONDecodeError, KeyError):
                            return content[0].get("text")
        return None

    async def get_positions(self):
        """Get open XAUUSD positions."""
        data = await self.call("get_positions", {"symbolId": SYMBOL_ID})
        if data and "positions" in data:
            return data["positions"]
        return []

    async def get_balance(self):
        """Get account balance in USD. cTrader returns cents + moneyDigits."""
        data = await self.call("get_balance")
        if not data or isinstance(data, str):
            return 0.0
        # balance is in cents; moneyDigits tells us decimal precision
        money_digits = int(data.get("moneyDigits", 2))
        bal_cents = int(data.get("balance", 0))
        return bal_cents / (10 ** money_digits)

    async def get_spot_price(self):
        """Get current bid/ask for our symbol.

        cTrader MCP tool is 'get_spot_prices' (plural), symbolId must be a list.
        Returns dict with 'bid' and 'ask' in display price, or (0, 0) on failure.
        """
        data = await self.call("get_spot_prices", {"symbolId": [SYMBOL_ID]})
        if not data or isinstance(data, str):
            log.error(f"get_spot_prices failed: {data}")
            return 0, 0
        prices = data.get("prices", []) or data.get("spotPrices", [])
        if not prices:
            log.error(f"get_spot_prices: no prices in response: {data}")
            return 0, 0
        p = prices[0]
        # Bid/ask are in pipettes (price * 10^pip_digits)
        bid = float(p.get("bid", 0)) / (10 ** PIP_DIGITS)
        ask = float(p.get("ask", 0)) / (10 ** PIP_DIGITS)
        return bid, ask

    async def close_position(self, position_id, volume_cents):
        """Close a position."""
        return await self.call("close_position",
                               {"positionId": position_id, "volume": volume_cents})

    async def create_market_order(self, side, volume_lots, sl_price, tp_price):
        """Place MARKET order with SL/TP."""
        volume_cents = int(volume_lots * LOT_SIZE * 100)
        # SL/TP as relative pipettes
        step = 1000  # XAUUSD step
        # Get current price for relative calc
        bid, ask = await self.get_spot_price()
        if side == "BUY":
            cur_price = ask
            sl_pipettes = int(round(abs(cur_price - sl_price) * (10 ** PIP_DIGITS) / step) * step)
            tp_pipettes = int(round(abs(tp_price - cur_price) * (10 ** PIP_DIGITS) / step) * step)
        else:
            cur_price = bid
            sl_pipettes = int(round(abs(sl_price - cur_price) * (10 ** PIP_DIGITS) / step) * step)
            tp_pipettes = int(round(abs(cur_price - tp_price) * (10 ** PIP_DIGITS) / step) * step)

        args = {
            "symbolId": SYMBOL_ID,
            "orderType": "MARKET",
            "tradeSide": side,
            "volume": volume_cents,
            "relativeStopLoss": max(sl_pipettes, step),
            "relativeTakeProfit": max(tp_pipettes, step),
        }
        return await self.call("create_order", args)

    async def close_all(self):
        """Close all positions for symbol."""
        positions = await self.get_positions()
        results = []
        for p in positions:
            pid = p.get("positionId")
            vol = int(p.get("volume", 0))
            r = await self.close_position(pid, vol)
            results.append({"pid": pid, "result": r})
        return results, positions


# === Whisper STT ===
_whisper_model = None

def get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        log.info(f"Loading whisper model '{WHISPER_MODEL}' on {WHISPER_DEVICE} ({WHISPER_COMPUTE})...")
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE)
        log.info("Whisper model loaded.")
    return _whisper_model


def transcribe(audio_path):
    """Transcribe audio file. Returns text (lowercased).

    Force language=ru (prevents 'kupi' being misdetected as Hindi 'कुपी').
    Use initial_prompt with command words to bias recognition.
    """
    model = get_whisper_model()
    # Force Russian + initial prompt with command words for better accuracy
    segments, info = model.transcribe(
        audio_path,
        language="ru",          # force Russian (we accept English via fuzzy match later)
        beam_size=5,
        initial_prompt="Команды: купи, продай, закрой, статус. купи продай закрой статус."
    )
    text = " ".join([s.text for s in segments]).strip().lower()
    return text


# === Command parser ===
# Transliteration map: latin -> cyrillic (for Whisper misrecognitions like 'coupi' -> 'купи')
LATIN_TO_CYR = {
    "a": "а", "b": "б", "c": "к", "d": "д", "e": "е", "f": "ф", "g": "г",
    "h": "х", "i": "и", "j": "й", "k": "к", "l": "л", "m": "м", "n": "н",
    "o": "о", "p": "п", "q": "к", "r": "р", "s": "с", "t": "т", "u": "у",
    "v": "в", "w": "в", "x": "кс", "y": "и", "z": "з",
}


def _translit_to_cyr(word):
    """Transliterate latin chars to cyrillic. Keeps cyrillic as-is."""
    out = ""
    for ch in word.lower():
        if ch in LATIN_TO_CYR:
            out += LATIN_TO_CYR[ch]
        else:
            out += ch
    return out


# Levenshtein distance for fuzzy matching (handles 'купь' -> 'купи')
def _levenshtein(a, b):
    """Compute Levenshtein distance between two strings."""
    if len(a) < len(b):
        a, b = b, a
    if len(b) == 0:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            ins = prev[j + 1] + 1
            dele = curr[j] + 1
            sub = prev[j] + (ca != cb)
            curr.append(min(ins, dele, sub))
        prev = curr
    return prev[-1]


def _fuzzy_match(word, target, max_dist=2):
    """Check if word matches target within Levenshtein distance max_dist.
    Special handling: also matches if word starts with target (e.g. 'купить' starts with 'купи').
    Tries both original word AND transliterated version.
    """
    if word == target:
        return True
    if word.startswith(target) or target.startswith(word):
        return True
    # Try transliterated version (handles 'coupi' -> 'купи')
    word_cyr = _translit_to_cyr(word)
    if word_cyr == target or word_cyr.startswith(target) or target.startswith(word_cyr):
        return True
    # Levenshtein on both versions
    for w in [word, word_cyr]:
        dist = _levenshtein(w, target)
        max_allowed = 1 if len(target) <= 4 else 2
        if dist <= max_allowed:
            return True
    return False


def parse_command(text):
    """Parse transcribed text to trade command.

    Handles Whisper misrecognitions:
    - 'купи' may come as 'coupi', 'copi', 'kupi', 'kupy', 'куми', 'куби'
    - 'продай' may come as 'prodai', 'proday', 'прадай'
    - 'закрой' may come as 'zakroy', 'close'
    Uses fuzzy matching (Levenshtein distance <= 2) + transliteration.

    Returns: (command, note) where command is one of:
    'buy', 'sell', 'close', 'status', 'unknown'
    """
    text = text.lower().strip()
    # Remove punctuation and non-letter chars (keep cyrillic + latin)
    cleaned = ""
    for ch in text:
        if ch.isalpha() or ch.isspace():
            cleaned += ch
        else:
            cleaned += " "
    words = cleaned.split()

    # Target words (Russian + English + common misrecognitions)
    buy_targets = ["купи", "купить", "покупай", "лонг", "long", "buy", "bay", "bye", "куми", "куби", "куш"]
    sell_targets = ["продай", "продать", "продавай", "шорт", "short", "sell", "cel", "cell", "прадай", "прадать"]
    close_targets = ["закрой", "закрыть", "close", "flat", "клоз", "клоуз", "заркрой"]
    status_targets = ["статус", "status", "позиция", "position"]

    log.info(f"Parse: words={words} (translit: {[_translit_to_cyr(w) for w in words]})")

    for w in words:
        # Exact match first (also try transliterated)
        w_cyr = _translit_to_cyr(w)
        for variant in [w, w_cyr]:
            if variant in buy_targets:
                return "buy", f"распознано: '{w}' (exact{' translit' if variant != w else ''})"
            if variant in sell_targets:
                return "sell", f"распознано: '{w}' (exact{' translit' if variant != w else ''})"
            if variant in close_targets:
                return "close", f"распознано: '{w}' (exact{' translit' if variant != w else ''})"
            if variant in status_targets:
                return "status", f"распознано: '{w}' (exact{' translit' if variant != w else ''})"

    # Fuzzy match if no exact
    for w in words:
        for target in buy_targets:
            if _fuzzy_match(w, target):
                return "buy", f"распознано: '{w}' ~ '{target}' (fuzzy)"
        for target in sell_targets:
            if _fuzzy_match(w, target):
                return "sell", f"распознано: '{w}' ~ '{target}' (fuzzy)"
        for target in close_targets:
            if _fuzzy_match(w, target):
                return "close", f"распознано: '{w}' ~ '{target}' (fuzzy)"
        for target in status_targets:
            if _fuzzy_match(w, target):
                return "status", f"распознано: '{w}' ~ '{target}' (fuzzy)"

    return "unknown", f"не распознано: '{text}'"


# === Telegram bot ===
async def send_telegram(chat_id, text, bot_token=TELEGRAM_BOT_TOKEN):
    """Send message via Telegram bot."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    async with httpx.AsyncClient() as client:
        await client.post(url, json=data, timeout=30)


async def download_telegram_file(file_id, bot_token=TELEGRAM_BOT_TOKEN):
    """Download file from Telegram. Returns local path."""
    async with httpx.AsyncClient() as client:
        # Get file path
        resp = await client.get(f"https://api.telegram.org/bot{bot_token}/getFile?file_id={file_id}", timeout=30)
        data = resp.json()
        if not data.get("ok"):
            raise Exception(f"getFile failed: {data}")
        file_path = data["result"]["file_path"]
        # Download
        download_url = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
        resp = await client.get(download_url, timeout=60)
        if resp.status_code != 200:
            raise Exception(f"Download failed: HTTP {resp.status_code}")
        # Save to temp file with .ogg extension
        tmp = tempfile.NamedTemporaryFile(suffix=".ogg", delete=False)
        tmp.write(resp.content)
        tmp.close()
        return tmp.name


# Voice bot pause flag — auto-bot checks this and stays idle during manual trade
VOICE_PAUSE_FLAG = "/root/bots/voice_pause.flag"
VOICE_PAUSE_DURATION = 300  # 5 minutes — auto-bot stays paused this long after voice command

# Voice trades marker log — records position IDs opened/closed by voice bot
# ctrader_alerts.py reads this to show "voice" vs "auto" source in alerts
VOICE_TRADES_LOG = "/root/bots/voice_trades.jsonl"


def log_voice_trade(position_id, action, side, price, command):
    """Append a marker entry to voice_trades.jsonl.
    action: 'opened' | 'closed'
    side: 'LONG' | 'SHORT' | 'FLAT'
    command: original voice command ('купи', 'продай', 'закрой')
    """
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "position_id": position_id,
        "action": action,
        "side": side,
        "price": price,
        "command": command,
        "source": "voice"
    }
    try:
        with open(VOICE_TRADES_LOG, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        log.info(f"Voice trade logged: {action} {side} PID={position_id}")
    except Exception as e:
        log.warning(f"Failed to log voice trade: {e}")


def is_voice_position(position_id):
    """Check if a position was opened by voice bot (by position_id)."""
    if not position_id or not os.path.exists(VOICE_TRADES_LOG):
        return False
    try:
        with open(VOICE_TRADES_LOG) as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    if entry.get("position_id") == position_id and entry.get("action") == "opened":
                        return True
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return False


def set_voice_pause(duration_sec=VOICE_PAUSE_DURATION):
    """Create pause flag file. Auto-bot will see this and stay idle."""
    expiry = int(time.time()) + duration_sec
    with open(VOICE_PAUSE_FLAG, "w") as f:
        f.write(str(expiry))
    log.info(f"Voice pause set for {duration_sec}s (auto-bot idle)")


def clear_voice_pause():
    """Remove pause flag — auto-bot resumes normal operation."""
    try:
        if os.path.exists(VOICE_PAUSE_FLAG):
            os.remove(VOICE_PAUSE_FLAG)
            log.info("Voice pause cleared (auto-bot resumed)")
    except Exception as e:
        log.warning(f"Failed to clear pause flag: {e}")


# === Trade executor ===
async def execute_command(command, ctrader):
    """Execute trade command. Returns (success, message)."""
    positions = await ctrader.get_positions()
    pos_count = len(positions)
    has_long = any(p.get("tradeSide", "").upper() == "BUY" for p in positions)
    has_short = any(p.get("tradeSide", "").upper() == "SELL" for p in positions)

    if command == "status":
        bal = await ctrader.get_balance()
        if pos_count == 0:
            return True, f"📊 Статус GOLD:\nБаланс: ${bal:.2f}\nПозиция: нет (flat)"
        msg = f"📊 Статус GOLD:\nБаланс: ${bal:.2f}\nПозиций: {pos_count}\n"
        for p in positions:
            side = "LONG" if p.get("tradeSide", "").upper() == "BUY" else "SHORT"
            entry = float(p.get("entryPrice", 0))
            vol = int(p.get("volume", 0)) / (LOT_SIZE * 100)
            sl = float(p.get("stopLoss", 0)) if p.get("stopLoss") else 0
            tp = float(p.get("takeProfit", 0)) if p.get("takeProfit") else 0
            msg += f"  {side} {vol} lot @ ${entry:.2f} | SL=${sl:.2f} TP=${tp:.2f}\n"
        return True, msg.strip()

    if command == "close":
        if pos_count == 0:
            return True, "ℹ️ Нет открытых позиций для закрытия."
        # Pause auto-bot before closing (give us 5 min to manage position)
        set_voice_pause(300)
        # Log voice trade markers for each position being closed
        for p in positions:
            pid = p.get("positionId")
            side = "LONG" if p.get("tradeSide", "").upper() == "BUY" else "SHORT"
            entry = float(p.get("entryPrice", 0))
            log_voice_trade(pid, "closed", side, entry, "закрой")
        results, _ = await ctrader.close_all()
        return True, f"✅ Закрыто позиций: {len(results)}\n⏸️ Auto-bot на паузе 5 мин"

    if command == "buy":
        # Always pause auto-bot for 5 min when user gives manual command
        set_voice_pause(300)
        if has_short:
            # Close SHORT first
            for p in positions:
                if p.get("tradeSide", "").upper() == "SELL":
                    pid = p.get("positionId")
                    entry = float(p.get("entryPrice", 0))
                    log_voice_trade(pid, "closed", "SHORT", entry, "купи")
            results, _ = await ctrader.close_all()
            return True, f"✅ SHORT закрыт по команде 'купи' (закрыто: {len(results)})\n⏸️ Auto-bot на паузе 5 мин"
        if has_long:
            return True, "ℹ️ Уже в LONG позиции. Голосовая команда проигнорирована.\n⏸️ Auto-bot на паузе 5 мин"
        # Open LONG
        bid, ask = await ctrader.get_spot_price()
        if ask == 0:
            return False, "❌ Не удалось получить цену"
        entry = ask
        sl_price = entry - SL_USD
        tp_price = entry + TP_USD
        order = await ctrader.create_market_order("BUY", DEFAULT_VOLUME, sl_price, tp_price)
        if order and "positionId" in order:
            pid = order["positionId"]
            log_voice_trade(pid, "opened", "LONG", entry, "купи")
            return True, (f"✅ Открыт LONG {DEFAULT_VOLUME} lot @ ${entry:.2f}\n"
                          f"SL=${sl_price:.2f} (-${SL_USD})\n"
                          f"TP=${tp_price:.2f} (+${TP_USD})\n"
                          f"PID: {pid}")
        return False, f"❌ Ошибка открытия LONG: {order}"

    if command == "sell":
        # Always pause auto-bot for 5 min when user gives manual command
        set_voice_pause(300)
        if has_long:
            # Close LONG first
            for p in positions:
                if p.get("tradeSide", "").upper() == "BUY":
                    pid = p.get("positionId")
                    entry = float(p.get("entryPrice", 0))
                    log_voice_trade(pid, "closed", "LONG", entry, "продай")
            results, _ = await ctrader.close_all()
            return True, f"✅ LONG закрыт по команде 'продай' (закрыто: {len(results)})\n⏸️ Auto-bot на паузе 5 мин"
        if has_short:
            return True, "ℹ️ Уже в SHORT позиции. Голосовая команда проигнорирована.\n⏸️ Auto-bot на паузе 5 мин"
        # Open SHORT
        bid, ask = await ctrader.get_spot_price()
        if bid == 0:
            return False, "❌ Не удалось получить цену"
        entry = bid
        sl_price = entry + SL_USD
        tp_price = entry - TP_USD
        order = await ctrader.create_market_order("SELL", DEFAULT_VOLUME, sl_price, tp_price)
        if order and "positionId" in order:
            pid = order["positionId"]
            log_voice_trade(pid, "opened", "SHORT", entry, "продай")
            return True, (f"✅ Открыт SHORT {DEFAULT_VOLUME} lot @ ${entry:.2f}\n"
                          f"SL=${sl_price:.2f} (+${SL_USD})\n"
                          f"TP=${tp_price:.2f} (-${TP_USD})\n"
                          f"PID: {pid}")
        return False, f"❌ Ошибка открытия SHORT: {order}"

    return False, f"❌ Неизвестная команда: {command}"


# === Main bot loop ===
async def main():
    log.info("=" * 60)
    log.info("Voice Trade Bot for GOLD cTrader")
    log.info(f"Authorized chats: {AUTHORIZED_CHAT_IDS}")
    log.info(f"Default volume: {DEFAULT_VOLUME} lot, SL=${SL_USD}, TP=${TP_USD}")
    log.info("=" * 60)

    # Connect to cTrader
    ctrader = CTraderMCP()
    if not await ctrader.connect():
        log.error("Failed to connect to cTrader MCP. Exiting.")
        sys.exit(1)

    # Preload whisper model
    log.info("Preloading whisper model...")
    get_whisper_model()

    # Telegram getUpdates loop
    offset = 0
    log.info("Listening for Telegram updates...")
    while True:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                    params={"offset": offset, "timeout": 30},
                    timeout=35,
                )
                data = resp.json()
                if not data.get("ok"):
                    log.error(f"getUpdates failed: {data}")
                    await asyncio.sleep(5)
                    continue

                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    msg = update.get("message", {})
                    chat_id = msg.get("chat", {}).get("id")

                    # Auth check
                    if chat_id not in AUTHORIZED_CHAT_IDS:
                        log.warning(f"Unauthorized chat_id: {chat_id}")
                        await send_telegram(chat_id, "⛔️ Не авторизован.")
                        continue

                    # Handle voice message
                    voice = msg.get("voice")
                    if voice:
                        file_id = voice["file_id"]
                        await send_telegram(chat_id, "🎧 Обрабатываю голосовое сообщение...")
                        try:
                            # Download
                            audio_path = await download_telegram_file(file_id)
                            log.info(f"Downloaded voice: {audio_path}")

                            # Transcribe
                            t0 = time.time()
                            text = transcribe(audio_path)
                            t1 = time.time()
                            log.info(f"Transcribed in {t1-t0:.1f}s: '{text}'")
                            os.unlink(audio_path)

                            await send_telegram(chat_id, f"🎤 Распознано: <i>\"{text}\"</i>")

                            # Parse command
                            command, note = parse_command(text)
                            log.info(f"Command: {command} ({note})")

                            if command == "unknown":
                                await send_telegram(chat_id,
                                    f"❓ Не распознал команду.\n"
                                    f"Доступные: 'купи' (buy), 'продай' (sell), 'закрой' (close), 'статус' (status)")
                                continue

                            # Execute
                            success, result_msg = await execute_command(command, ctrader)
                            await send_telegram(chat_id, result_msg)
                            log.info(f"Execute {command}: success={success}, msg={result_msg[:100]}")

                        except Exception as e:
                            log.exception(f"Voice processing failed: {e}")
                            await send_telegram(chat_id, f"❌ Ошибка: {e}")
                        continue

                    # Handle text message
                    text = msg.get("text", "").lower().strip()
                    if text:
                        if text in ("/start", "/help", "помощь"):
                            await send_telegram(chat_id,
                                "🎙️ <b>Voice Trade Bot для GOLD</b>\n\n"
                                "Отправь голосовое сообщение с командой:\n"
                                "• <b>купи</b> / <b>buy</b> / <b>long</b> — закрыть SHORT или открыть LONG\n"
                                "• <b>продай</b> / <b>sell</b> / <b>short</b> — закрыть LONG или открыть SHORT\n"
                                "• <b>закрой</b> / <b>close</b> — закрыть любую позицию\n"
                                "• <b>статус</b> / <b>status</b> — показать текущую позицию\n\n"
                                f"Параметры: {DEFAULT_VOLUME} lot, SL=${SL_USD}, TP=${TP_USD}")
                            continue

                        # Parse text as command too
                        command, note = parse_command(text)
                        if command != "unknown":
                            success, result_msg = await execute_command(command, ctrader)
                            await send_telegram(chat_id, result_msg)
                            continue

                        await send_telegram(chat_id,
                            f"❓ Не понял команду. Отправь голосовое или текст: 'купи', 'продай', 'закрой', 'статус'")

        except httpx.ReadTimeout:
            # Normal — long polling
            continue
        except Exception as e:
            log.exception(f"Main loop error: {e}")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
