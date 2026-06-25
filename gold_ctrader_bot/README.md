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
| `gold_mcp_bot.py` | Main bot (MCP client + scale-in logic + VPS sync) |
| `strategy.py` | EMA+ADX+ATR signal generation (Wilder's smoothing) |
| `config.py` | Configuration template → copy to `.env` |
| `watchdog.ps1` | Auto-restart bot if process dies |
| `install_watchdog.bat` | Register watchdog in Windows Task Scheduler |
| `update_bot.bat` | One-command update: stop + git pull + start |
| `archive/` | Deprecated files (Open API path, C# cBot stub) |

## Automation (set up once)

### 1. Auto-start on Windows boot
- Copy `launch_mcp_bot.bat` shortcut to `shell:startup` (Win+R → `shell:startup`)
- Bot will start automatically after every reboot

### 2. Auto-restart if crashed (Watchdog)
```bash
# Run as Administrator:
install_watchdog.bat
```
Creates Windows Scheduled Task "GoldBot Watchdog" that:
- Runs every 5 minutes
- Checks if bot process is alive
- Restarts if crashed

### 3. One-command update
```bash
update_bot.bat
```
Stops bot, pulls latest from GitHub, restarts. No need for Ctrl+C + manual restart.

## Manual control

```bash
# Stop bot
taskkill /F /IM python.exe /FI "WINDOWTITLE eq gold_mcp*"

# Or more precise:
powershell "Get-Process python | Where-Object { (Get-CimInstance Win32_Process -Filter \"ProcessId=$($_.Id)\").CommandLine -like '*gold_mcp_bot*' } | Stop-Process -Force"

# Start bot
launch_mcp_bot.bat

# Check logs
type %TEMP%\gold_mcp_bot_output.txt
type %TEMP%\gold_mcp_bot_watchdog.log
```

## VPS sync (optional, recommended)

Bot can push closed trades + state to VPS HTTP endpoint for dashboard integration.

1. VPS must run `ctrader_trades_server.py` on port 8089
2. Add to `.env`:
   ```
   VPS_SYNC_ENABLED=true
   VPS_SYNC_URL=http://your-vps-ip:8089
   VPS_AUTH_TOKEN=gold2026secret
   ```
3. Bot will push:
   - Each closed trade → `POST /api/trade` (for dashboard + Telegram alerts)
   - State every 60s → `POST /api/state` (for live card in dashboard)

Dashboard: http://your-vps-ip:8080/report_latest.html (GOLD-CTRADER appears as 7th bot)
Telegram alerts: configure separately via `ctrader_alerts.py` on VPS
