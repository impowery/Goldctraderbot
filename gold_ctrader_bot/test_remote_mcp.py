"""Quick test: connect to cTrader Remote MCP, list tools, get balance.
Does NOT place any orders. Safe to run.
"""
import asyncio
import os
import sys
import json
from dotenv import load_dotenv

load_dotenv()

# Add bot dir to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Force fresh import
import importlib
if 'gold_mcp_bot' in sys.modules:
    del sys.modules['gold_mcp_bot']
import gold_mcp_bot

MCP_URL = gold_mcp_bot.MCP_URL
MCP_BEARER_TOKEN = gold_mcp_bot.MCP_BEARER_TOKEN

print(f"MCP_URL: {MCP_URL}")
print(f"MCP_BEARER_TOKEN: {'set (' + MCP_BEARER_TOKEN[:30] + '...)' if MCP_BEARER_TOKEN else 'NOT SET'}")
print()

if not MCP_BEARER_TOKEN:
    print("ERROR: MCP_BEARER_TOKEN not set in .env")
    sys.exit(1)


async def test():
    bot = gold_mcp_bot.GoldMCPBot()

    print("=== STEP 1: Connect ===")
    try:
        ok = await bot.connect()
        if not ok:
            print("FAIL: connect() returned False")
            return
        print(f"OK: connected, session={bot.session_id[:20]}...")
    except Exception as e:
        print(f"FAIL: connect() exception: {e}")
        import traceback
        traceback.print_exc()
        return

    print()
    print("=== STEP 2: List tools ===")
    print(f"Available tools: {len(bot.tool_names)}")
    # Print first 30 tools
    for i, (name, tool) in enumerate(list(bot.tool_names.items())[:30]):
        desc = tool.get("description", "")[:80] if isinstance(tool, dict) else ""
        print(f"  {i+1:2d}. {name} — {desc}")
    if len(bot.tool_names) > 30:
        print(f"  ... and {len(bot.tool_names) - 30} more")

    # Check if critical tools exist
    print()
    print("=== STEP 3: Check critical tools ===")
    critical = [
        "place_market_order", "amend_position", "close_position",
        "close_position_partial", "close_all_positions", "get_positions",
        "get_balance", "get_symbol_details", "get_trendbars"
    ]
    for t in critical:
        present = t in bot.tool_names
        marker = "OK" if present else "MISSING"
        print(f"  [{marker}] {t}")

    print()
    print("=== STEP 4: Get balance ===")
    try:
        bal = await bot.get_balance_raw()
        if bal:
            print(f"OK: balance={bal.get('balance')} equity={bal.get('equity')}")
        else:
            print("WARN: get_balance returned empty")
    except Exception as e:
        print(f"FAIL: get_balance exception: {e}")

    print()
    print("=== STEP 5: Get positions ===")
    try:
        pos = await bot.get_positions_raw()
        if pos:
            positions = pos.get("positions", [])
            print(f"OK: {len(positions)} position(s)")
            for p in positions[:5]:
                print(f"  {p.get('symbolName')} {p.get('tradeSide')} {p.get('volumeInLots')} lots @ {p.get('entryPrice')}")
        else:
            print("OK: no positions (expected — you closed them)")
    except Exception as e:
        print(f"FAIL: get_positions exception: {e}")

    print()
    print("=== STEP 6: Get symbol details (XAUUSD) ===")
    try:
        sym = bot._parse_text(await bot.call("get_symbol_details", {"symbolName": "XAUUSD"}))
        if sym:
            print(f"OK: lotSize={sym.get('lotSize')} pipSize={sym.get('pipSize')} digits={sym.get('digits')}")
        else:
            print("WARN: get_symbol_details returned empty")
    except Exception as e:
        print(f"FAIL: get_symbol_details exception: {e}")

    print()
    print("=== TEST COMPLETE ===")
    print("If all steps passed — Remote MCP works. Bot can run on Linux VPS.")


asyncio.run(test())
