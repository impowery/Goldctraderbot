# GoldBot Watchdog — restarts bot (+ cTrader) if process dies
# Run via Task Scheduler every 5 minutes

$botName = "gold_mcp_bot"
$python = "C:\Users\Андрей\AppData\Local\Programs\Python\Python312\python.exe"
$botPath = "C:\Users\Андрей\Desktop\BOT live BTC\Ctrader\gold_ctrader_bot\gold_mcp_bot.py"
$workDir = "C:\Users\Андрей\Desktop\BOT live BTC\Ctrader\gold_ctrader_bot"
$ctraderDir = "$env:LOCALAPPDATA\Spotware\cTrader"
$ctraderPath = if (Test-Path $ctraderDir) {
    $folder = Get-ChildItem -Path $ctraderDir -Directory | Select-Object -First 1
    if ($folder) { Join-Path $folder.FullName "cTrader.exe" } else { $null }
} else { $null }
$logFile = "$env:TEMP\gold_mcp_bot_watchdog.log"

function Write-Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$ts $msg" | Out-File -Append -FilePath $logFile -Encoding UTF8
    Write-Host "$ts $msg"
}

# 1. Check cTrader Desktop — if not running, start it
$ct = Get-Process -Name "cTrader" -ErrorAction SilentlyContinue
if (-not $ct) {
    Write-Log "cTrader NOT running — starting..."
    try {
        Start-Process -FilePath $ctraderPath
        Write-Log "cTrader launched"
    } catch {
        Write-Log "ERROR starting cTrader: $_"
    }
} else {
    # Already running — silently OK
}

# 2. Check if bot process is running
$proc = Get-Process -Name "python" -ErrorAction SilentlyContinue | Where-Object {
    try {
        $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)").CommandLine
        $cmd -like "*gold_mcp_bot*"
    } catch { $false }
}

if ($proc) {
    Write-Log "Bot process running (PID $($proc.Id)) - OK"
    exit 0
}

# Bot not running - restart it
Write-Log "Bot NOT running - restarting..."

# Start bot in hidden window (redirect output to file)
$logOut = "$env:TEMP\gold_mcp_bot_output.txt"
$logErr = "$env:TEMP\gold_mcp_bot_output.err"
try {
    $p = Start-Process -PassThru -NoNewWindow -FilePath $python -WorkingDirectory $workDir `
        -ArgumentList "-u", "`"$botPath`"" `
        -RedirectStandardOutput $logOut -RedirectStandardError $logErr
    Write-Log "Bot started - PID $($p.Id)"
    Start-Sleep -Seconds 5
    if ($p.HasExited) {
        Write-Log "ERROR: Bot exited immediately with code $($p.ExitCode)"
    } else {
        Write-Log "Bot running OK"
    }
} catch {
    Write-Log "ERROR starting bot: $_"
}
