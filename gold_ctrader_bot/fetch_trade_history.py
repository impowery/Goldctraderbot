"""Fetch real trade history from cTrader via Remote MCP get_deals.
Shows ALL deals (filled orders) for last 7 days.
"""
import asyncio
import os
import sys
import json
import httpx
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gold_mcp_bot

MCP_URL = gold_mcp_bot.MCP_URL
MCP_BEARER_TOKEN = gold_mcp_bot.MCP_BEARER_TOKEN


async def mcp_call(client, session_id, tool_name, args=None):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {MCP_BEARER_TOKEN}",
        "Mcp-Session-Id": session_id,
    }
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool_name, "arguments": args or {}}}
    resp = await client.post(MCP_URL, json=body, headers=headers)
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
            except:
                pass
    return None


async def test():
    async with httpx.AsyncClient(timeout=30) as client:
        # Connect
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "history", "version": "1.0"}}}
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {MCP_BEARER_TOKEN}"
        }
        resp = await client.post(MCP_URL, json=body, headers=headers)
        session_id = resp.headers.get("Mcp-Session-Id")
        headers["Mcp-Session-Id"] = session_id
        await client.post(MCP_URL, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers)

        # Get deals for last 7 days (max 720h = 30 days, but we use 7)
        now = datetime.now(timezone.utc)
        week_ago = now - timedelta(days=7)

        print(f"=== DEALS (last 7 days: {week_ago.strftime('%Y-%m-%d')} to {now.strftime('%Y-%m-%d')}) ===")
        deals = await mcp_call(client, session_id, "get_deals", {
            "fromTimestamp": week_ago.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "toTimestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "maxRows": 100,
        })

        if not deals:
            print("No deals found")
            return

        deal_list = deals.get("deals", [])
        print(f"Total deals: {len(deal_list)}")
        print(f"Has more: {deals.get('hasMore', False)}")
        print()

        # Sort by timestamp
        deal_list.sort(key=lambda d: d.get("executionTimestamp", 0))

        # Group by positionId to show trades
        positions = {}
        for d in deal_list:
            pid = d.get("positionId", "unknown")
            if pid not in positions:
                positions[pid] = []
            positions[pid].append(d)

        print(f"=== GROUPED BY POSITION ({len(positions)} positions) ===")
        print()

        total_pnl = 0
        total_commission = 0

        for pid, deals in positions.items():
            print(f"--- Position {pid} ({len(deals)} deals) ---")
            entry_price = None
            exit_price = None
            direction = None
            volume = 0
            position_pnl = 0
            position_comm = 0

            for d in deals:
                ts = d.get("executionTimestamp", 0)
                dt = datetime.fromtimestamp(ts/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                side = d.get("tradeSide", "?")
                price = d.get("executionPrice", 0)
                vol = d.get("volume", 0)
                comm = d.get("commission", 0)
                # Volume is in cents: 200 = 0.02 lots
                vol_lots = vol / 10000 if vol else 0

                print(f"  {dt} | {side:4s} | {vol_lots:.4f} lots | ${price:.2f} | comm=${comm/100:.2f}")

                if entry_price is None:
                    entry_price = price
                    direction = side
                    volume = vol_lots
                else:
                    exit_price = price
                position_comm += comm

            # Calculate PnL
            if entry_price and exit_price and direction:
                # BUY: PnL = (exit - entry) * volume_in_oz
                # SELL: PnL = (entry - exit) * volume_in_oz
                vol_oz = volume * 100  # lots to oz
                if direction == "BUY":
                    pnl = (exit_price - entry_price) * vol_oz
                else:
                    pnl = (entry_price - exit_price) * vol_oz
                position_pnl = pnl
                total_pnl += position_pnl

            total_commission += position_comm
            print(f"  Result: {direction} {volume:.4f} lots @ ${entry_price:.2f} → ${exit_price:.2f}")
            print(f"  PnL: ${position_pnl:+.2f} | Commission: ${position_comm/100:.2f}")
            print()

        print("=" * 60)
        print(f"TOTAL PnL (7 days): ${total_pnl:+.2f}")
        print(f"TOTAL Commission: ${total_commission/100:.2f}")
        print(f"NET PnL: ${total_pnl - total_commission/100:+.2f}")
        print(f"Positions: {len(positions)}")


asyncio.run(test())
