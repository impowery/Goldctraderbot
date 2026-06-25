@echo off
REM Install GoldBot watchdog as Windows Scheduled Task
REM Runs every 5 minutes, restarts bot if crashed

echo Installing GoldBot watchdog...

REM Delete existing task if any
schtasks /Delete /TN "GoldBot Watchdog" /F >nul 2>&1

REM Create task - runs every 5 minutes, also at logon
schtasks /Create /TN "GoldBot Watchdog" /TR "powershell.exe -ExecutionPolicy Bypass -File \"%~dp0watchdog.ps1\"" /SC MINUTE /MO 5 /RL HIGHEST /F

if %errorlevel%==0 (
    echo.
    echo SUCCESS: Watchdog installed!
    echo Task name: GoldBot Watchdog
    echo Schedule: every 5 minutes
    echo.
    echo Also running NOW...
    powershell.exe -ExecutionPolicy Bypass -File "%~dp0watchdog.ps1"
) else (
    echo.
    echo FAILED - try running as Administrator
)

pause
