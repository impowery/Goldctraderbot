@echo off
cd /d "C:\Users\Андрей\Desktop\BOT live BTC\Ctrader\gold_ctrader_bot"
echo Installing/updating dependencies...
"C:\Users\Андрей\AppData\Local\Programs\Python\Python312\python.exe" -m pip install -r requirements.txt
echo Starting GoldBot MCP...
"C:\Users\Андрей\AppData\Local\Programs\Python\Python312\python.exe" -u gold_mcp_bot.py
pause
