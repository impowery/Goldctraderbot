"""Debug: print raw trendbar to see pipettes format."""
import asyncio
import os
import sys
import json
import httpx
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gold_mcp_bot

MCP_URL = gold_mcp_bot.MCP_URL
MCP_BEARER_TOKEN = gold_mcp_bot.MCP_BEARER_TOKEN


async def test():
    async with httpx.AsyncClient(timeout=30) as client:
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "test", "version": "1.0"}}}
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {MCP_BEARER_TOKEN}"
        }
        resp = await client.post(MCP_URL, json=body, headers=headers)
        session_id = resp.headers.get("Mcp-Session-Id")
        headers["Mcp-Session-Id"] = session_id
        await client.post(MCP_URL, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers)

        # Get trendbars raw
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        from_dt = now - timedelta(hours=2)
        body3 = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "get_trendbars",
                            "arguments": {"symbolId": 41, "period": "M_5",
                                          "fromTimestamp": from_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                          "toTimestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ")}}}
        resp3 = await client.post(MCP_URL, json=body3, headers=headers)
        # Parse SSE
        for line in resp3.text.split("\n"):
            if line.startswith("data: "):
                data = line[6:].strip()
                try:
                    parsed = json.loads(data)
                    if "result" in parsed:
                        content = parsed["result"].get("content", [])
                        for item in content:
                            if item.get("type") == "text":
                                text = item.get("text", "")
                                trendbars = json.loads(text)
                                bars = trendbars.get("trendbars", [])
                                print(f"Got {len(bars)} bars")
                                if bars:
                                    print("First bar:")
                                    print(json.dumps(bars[0], indent=2))
                                    print("Last bar:")
                                    print(json.dumps(bars[-1], indent=2))
                                    # Check fields
                                    print()
                                    print("Keys in first bar:", list(bars[0].keys()))
                        break
                except:
                    pass


asyncio.run(test())
