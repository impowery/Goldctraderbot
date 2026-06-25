# GoldBot Watchdog — restarts bot if Python process dies
# Run via Task Scheduler every 5 minutes
# Or: run in background via startup

$botName = "gold_mcp_bot"
$python = "C:\Users\Андрей\AppData\Local\Programs\Python\Python312\python.exe"
$botPath = "C:\Users\Андрей\Desktop\BOT live BTC\Ctrader\gold_ctrader_bot\gold_mcp_bot.py"
$workDir = "C:\Users\Андрей\Desktop\BOT live BTC\Ctrader\gold_ctrader_bot"
$logFile = "$env:TEMP\gold_mcp_bot_watchdog.log"

function Write-Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$ts $msg" | Out-File -Append -FilePath $logFile -Encoding UTF8
    Write-Host "$ts $msg"
}

# Check if bot process is running
$proc = Get-Process -Name "python" -ErrorAction SilentlyContinue | Where-Object {
    try {
        $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)").CommandLine
        $cmd -like "*gold_mcp_bot*"
    } catch { $false }
}

if ($proc) {
    # Bot is running, check if it's actually alive (state push within last 5 min)
    Write-Log "Bot process running (PID $($proc.Id)) - OK"
    exit 0
}

# Bot not running - restart it
Write-Log "Bot NOT running - restarting..."

# Set working directory
Set-Location $workDir

# Start bot in hidden window
$startInfo = New-Object System.Diagnostics.ProcessStartInfo
$startInfo.FileName = $python
$startInfo.Arguments = "-u `"$botPath`""
$startInfo.WorkingDirectory = $workDir
$startInfo.UseShellExecute = $false
$startInfo.RedirectStandardOutput = "$env:TEMP\gold_mcp_bot_output.txt"
$startInfo.RedirectStandardError = "$env:TEMP\gold_mcp_bot_output.err"
$startInfo.CreateNoWindow = $true

try {
    $p = [System.Diagnostics.Process]::Start($startInfo)
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
