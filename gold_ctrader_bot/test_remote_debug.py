"""Debug: full JSON for get_deals + try different params for get_spot_prices/get_trendbars."""
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


async def mcp_call_raw(client, session_id, tool_name, args=None):
    """Return RAW response text for debugging."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {MCP_BEARER_TOKEN}",
        "Mcp-Session-Id": session_id,
    }
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool_name, "arguments": args or {}}}
    resp = await client.post(MCP_URL, json=body, headers=headers)
    return resp.text


async def mcp_call(client, session_id, tool_name, args=None):
    """Parse SSE response, return content text."""
    raw = await mcp_call_raw(client, session_id, tool_name, args)
    for line in raw.split("\n"):
        if line.startswith("data: "):
            data = line[6:].strip()
            try:
                parsed = json.loads(data)
                if "result" in parsed:
                    content = parsed["result"].get("content", [])
                    for item in content:
                        if item.get("type") == "text":
                            return item.get("text", "")
            except:
                pass
    return raw[:500]


async def test():
    async with httpx.AsyncClient(timeout=30) as client:
        # Connect
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
        body2 = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        await client.post(MCP_URL, json=body2, headers=headers)

        # === get_deals FULL JSON ===
        print("=== get_deals FULL JSON (first deal) ===")
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        day_ago = now - timedelta(hours=24)
        deals_text = await mcp_call(client, session_id, "get_deals", {
            "fromTimestamp": day_ago.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "toTimestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "maxRows": 3,
        })
        try:
            deals = json.loads(deals_text)
            print(json.dumps(deals, indent=2)[:3000])
        except:
            print(f"Raw: {deals_text[:500]}")

        # === get_spot_prices RAW ===
        print()
        print("=== get_spot_prices RAW (symbolId=41) ===")
        raw = await mcp_call_raw(client, session_id, "get_spot_prices", {"symbolId": 41})
        print(raw[:1500])

        # === get_trendbars RAW ===
        print()
        print("=== get_trendbars RAW (symbolId=41, period=M5, count=3) ===")
        raw = await mcp_call_raw(client, session_id, "get_trendbars", {
            "symbolId": 41, "period": "M5", "count": 3
        })
        print(raw[:1500])

        # === try period=m5 (lowercase) ===
        print()
        print("=== get_trendbars period='m5' (lowercase) ===")
        raw = await mcp_call_raw(client, session_id, "get_trendbars", {
            "symbolId": 41, "period": "m5", "count": 3
        })
        print(raw[:1500])

        # === try period=5m ===
        print()
        print("=== get_trendbars period='5m' ===")
        raw = await mcp_call_raw(client, session_id, "get_trendbars", {
            "symbolId": 41, "period": "5m", "count": 3
        })
        print(raw[:1500])

        # === try minute5 ===
        print()
        print("=== get_trendbars period='minute5' ===")
        raw = await mcp_call_raw(client, session_id, "get_trendbars", {
            "symbolId": 41, "period": "minute5", "count": 3
        })
        print(raw[:1500])


asyncio.run(test())
