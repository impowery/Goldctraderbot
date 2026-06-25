@echo off
REM One-command bot update: pull + restart
REM Usage: update_bot.bat

cd /d "C:\Users\Андрей\Desktop\BOT live BTC\Ctrader\gold_ctrader_bot"

echo === 1. Stopping bot ===
taskkill /F /IM python.exe /FI "WINDOWTITLE eq gold_mcp*" 2>nul
REM More robust: kill by command line
powershell "Get-Process python -ErrorAction SilentlyContinue | Where-Object { (Get-CimInstance Win32_Process -Filter \"ProcessId=$($_.Id)\").CommandLine -like '*gold_mcp_bot*' } | Stop-Process -Force"

echo === 2. Git pull ===
git pull origin main
if %errorlevel% neq 0 (
    echo Git pull FAILED - check conflicts
    pause
    exit /b 1
)

echo === 3. Starting bot ===
start "" /B powershell.exe -ExecutionPolicy Bypass -File "%~dp0watchdog.ps1"

echo === Done. Bot restarted. ===
echo Log: %TEMP%\gold_mcp_bot_output.txt
echo Watchdog log: %TEMP%\gold_mcp_bot_watchdog.log

timeout /t 3 >nul
