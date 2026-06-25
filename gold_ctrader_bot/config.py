# GOLD cTrader Bot — config
# Copy to .env and fill in your real values

# --- Open API (не используется для MCP) ---
# CTRADER_CLIENT_ID=
# CTRADER_CLIENT_SECRET=
# CTRADER_ACCESS_TOKEN=
# CTRADER_REFRESH_TOKEN=
# CTRADER_ACCOUNT_ID=

# --- MCP Server ---
MCP_URL=http://127.0.0.1:9876/mcp/

# --- Strategy ---
SYMBOL_NAME=XAUUSD
MIN_INTERVAL_MINUTES=30
MAX_LOSS_PERCENT=3.0

# Scale-in (lots per entry)
ENTRY_VOLUMES=0.02,0.02,0.02
MAX_ENTRIES=3

# Trend-following settings (better RR for PipFarm challenge)
# RR = TP2/SL = 4.0/2.0 = 2.0 — risk $24 to make $48 (at ATR=$12)
SL_ATR_MULT=2.0          # was 4.0 — tighter SL, cut losses faster
TP1_ATR_MULT=1.5         # was 0.8 — partial close later, give trend room
TP2_ATR_MULT=4.0         # was 1.5 — let profits run, 4×ATR = $48
TRAIL_ACTIVATE_PCT=0.5   # was 0.3 — later trailing (avoid noise)
TIME_EXIT_HOURS=4        # was 2 — more time for trend to develop

# Demo sizing: 0.02 lots/entry, 0.06 max
# Challenge sizing: 0.06 lots/entry, 0.18 max (1:20 leverage on PipFarm)

TIMEFRAME=m5
CANDLE_COUNT=100
CHECK_INTERVAL=60

RECONNECT_DELAY=5
MAX_RECONNECT_DELAY=300

# --- VPS sync (push trades + state to VPS dashboard) ---
VPS_SYNC_ENABLED=true
VPS_SYNC_URL=http://193.233.19.171:8089
VPS_AUTH_TOKEN=gold2026secret
