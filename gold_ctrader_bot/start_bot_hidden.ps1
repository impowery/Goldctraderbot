$py = "C:\Users\A4F7~1\AppData\Local\Programs\Python\Python312\python.exe"
$dir = "C:\Users\A4F7~1\Desktop\BOT live BTC\Ctrader\gold_ctrader_bot"
$log = "$env:TEMP\gold_mcp_bot_output.txt"

Start-Process -NoNewWindow -FilePath $py -ArgumentList "-u", "gold_mcp_bot.py" -WorkingDirectory "$dir" -RedirectStandardOutput $log -RedirectStandardError "${log}.err"
Write-Host "GoldBot MCP started."
Write-Host "Log: $log"
