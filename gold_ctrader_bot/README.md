# GoldBot cTrader MCP

EMA20 + ADX + ATR strategy with scale-in (3×0.01 lots) for XAUUSD via cTrader MCP.

## How it works

- Connects to cTrader Desktop MCP server (`http://127.0.0.1:9876/mcp/`)
- Uses M5 candles → EMA20, ADX(14), ATR(14) for signals
- Scale-in: up to 3 entries of 0.01 lots each (=0.03 max)
- TP1 closes 50% at 0.8×ATR, TP2 at 1.5×ATR
- Trailing SL at 4×ATR, break-even after 0.3% profit
- Time exit after 2 hours, daily loss stop at 3%

## Requirements

- Windows with cTrader Desktop (MCP Server enabled, Allow trading ON)
- Python 3.12+
- `pip install -r requirements.txt`

## Setup

1. Copy `config.py` to `.env` and adjust settings
2. Launch cTrader Desktop → Settings → MCP Server → Enable + Allow trading
3. Run: `launch_mcp_bot.bat` or `python gold_mcp_bot.py`

## Files

| File | Purpose |
|---|---|
| `gold_mcp_bot.py` | Main bot (MCP client + scale-in logic) |
| `strategy.py` | EMA+ADX+ATR signal generation |
| `config.py` | Configuration template → copy to `.env` |
| `archive/` | Deprecated files (Open API path, C# cBot stub) |
