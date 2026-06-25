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
SL_ATR_MULT=4.0
TP1_ATR_MULT=0.8
TP2_ATR_MULT=1.5
TRAIL_ACTIVATE_PCT=0.3
TIME_EXIT_HOURS=2

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
