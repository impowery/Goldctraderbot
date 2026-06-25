"""Read-only test: get real data from cTrader demo via Remote MCP.
NO orders placed. Safe to run.
"""
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


async def mcp_call(client, session_id, tool_name, args=None):
    """Call MCP tool, parse SSE response."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {MCP_BEARER_TOKEN}",
        "Mcp-Session-Id": session_id,
    }
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool_name, "arguments": args or {}}}
    resp = await client.post(MCP_URL, json=body, headers=headers)
    # Parse SSE
    for line in resp.text.split("\n"):
        if line.startswith("data: "):
            data = line[6:].strip()
            try:
                parsed = json.loads(data)
                if "result" in parsed:
                    content = parsed["result"].get("content", [])
                    for item in content:
                        if item.get("type") == "text":
                            return json.loads(item.get("text", "{}"))
            except json.JSONDecodeError:
                pass
    return None


async def test():
    async with httpx.AsyncClient(timeout=30) as client:
        # Connect
        print("=== Connecting to Remote MCP ===")
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
        print(f"Session: {session_id}")

        # notifications/initialized
        headers["Mcp-Session-Id"] = session_id
        body2 = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        await client.post(MCP_URL, json=body2, headers=headers)

        # === STEP 1: get_balance ===
        print()
        print("=== STEP 1: get_balance ===")
        bal = await mcp_call(client, session_id, "get_balance")
        if bal:
            print(json.dumps(bal, indent=2))
        else:
            print("FAILED")

        # === STEP 2: get_positions ===
        print()
        print("=== STEP 2: get_positions ===")
        pos = await mcp_call(client, session_id, "get_positions")
        if pos:
            positions = pos.get("positions", [])
            print(f"Total positions: {len(positions)}")
            for p in positions[:10]:
                print(f"  {p.get('symbolName', '?'):10s} {p.get('tradeSide', '?'):5s} "
                      f"vol={p.get('volumeInLots', '?')} entry={p.get('entryPrice', '?')} "
                      f"SL={p.get('stopLoss', '?')} TP={p.get('takeProfit', '?')}")
            if not positions:
                print("(no open positions — expected, you closed them)")
        else:
            print("FAILED")

        # === STEP 3: get_symbols (find XAUUSD) ===
        print()
        print("=== STEP 3: get_symbols (search XAUUSD) ===")
        syms = await mcp_call(client, session_id, "get_symbols")
        if syms:
            sym_list = syms.get("symbols", [])
            print(f"Total symbols: {len(sym_list)}")
            # Find XAUUSD
            for s in sym_list:
                name = s.get("name", "") or s.get("symbolName", "")
                if "XAU" in name.upper():
                    print(f"  FOUND: {json.dumps(s)}")
                    break
        else:
            print("FAILED")

        # === STEP 4: get_spot_prices for XAUUSD ===
        print()
        print("=== STEP 4: get_spot_prices (XAUUSD) ===")
        # First need symbolId — try from get_symbols
        if syms:
            for s in syms.get("symbols", []):
                name = s.get("name", "") or s.get("symbolName", "")
                if name.upper() == "XAUUSD":
                    sid = s.get("id") or s.get("symbolId")
                    print(f"Using symbolId={sid} for XAUUSD")
                    prices = await mcp_call(client, session_id, "get_spot_prices", {"symbolId": sid})
                    if prices:
                        print(json.dumps(prices, indent=2))
                    else:
                        print("FAILED get_spot_prices")
                    break

        # === STEP 5: get_trendbars (last 5 M5 candles for XAUUSD) ===
        print()
        print("=== STEP 5: get_trendbars (XAUUSD M5, last 5) ===")
        if syms:
            for s in syms.get("symbols", []):
                name = s.get("name", "") or s.get("symbolName", "")
                if name.upper() == "XAUUSD":
                    sid = s.get("id") or s.get("symbolId")
                    bars = await mcp_call(client, session_id, "get_trendbars", {
                        "symbolId": sid,
                        "period": "M5",
                        "count": 5
                    })
                    if bars:
                        bar_list = bars.get("trendbars", []) or bars.get("bars", []) or bars
                        if isinstance(bar_list, dict):
                            bar_list = bar_list.get("trendbars", [])
                        print(f"Got {len(bar_list) if isinstance(bar_list, list) else '?'} bars")
                        if isinstance(bar_list, list):
                            for b in bar_list[-5:]:
                                print(f"  {b}")
                    else:
                        print("FAILED get_trendbars")
                    break

        # === STEP 6: get_deals (last 10) ===
        print()
        print("=== STEP 6: get_deals (last 24h) ===")
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        day_ago = now - timedelta(hours=24)
        deals = await mcp_call(client, session_id, "get_deals", {
            "fromTimestamp": day_ago.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "toTimestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "maxRows": 10,
        })
        if deals:
            deal_list = deals.get("deals", [])
            print(f"Got {len(deal_list)} deals in last 24h")
            for d in deal_list[:10]:
                print(f"  {d.get('timestamp', '?')} {d.get('symbolName', '?'):8s} "
                      f"{d.get('tradeSide', '?'):5s} vol={d.get('volumeInLots', '?')} "
                      f"price={d.get('price', '?')} pnl={d.get('pnl', '?')}")
        else:
            print("FAILED get_deals (or no deals)")


asyncio.run(test())
