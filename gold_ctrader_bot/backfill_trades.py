#!/usr/bin/env python3
"""Backfill trades from cTrader get_deals into trades_gold_ctrader.jsonl.

Fetches ALL deals from cTrader via Remote MCP, groups by positionId,
calculates PnL, writes to trade log in same format as bot writes.

Usage:
    python3 backfill_trades.py              # fetch last 7 days
    python3 backfill_trades.py --days 30    # fetch last 30 days

This REPLACES the trade log with clean data from cTrader.
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
TRADE_LOG_PATH = os.getenv("TRADE_LOG_PATH", "trades_gold_ctrader.jsonl")


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


async def fetch_all_deals(client, session_id, days=7):
    """Fetch all deals for last N days, handling pagination (720h limit)."""
    now = datetime.now(timezone.utc)
    all_deals = []
    
    # Split into 720h (30 day) chunks if needed
    chunk_days = min(days, 30)
    from_dt = now - timedelta(days=chunk_days)
    
    deals = await mcp_call(client, session_id, "get_deals", {
        "fromTimestamp": from_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "toTimestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "maxRows": 1000,
    })
    
    if not deals:
        return []
    
    all_deals.extend(deals.get("deals", []))
    
    # Handle pagination if hasMore
    while deals and deals.get("hasMore"):
        last_ts = max(d.get("executionTimestamp", 0) for d in all_deals)
        deals = await mcp_call(client, session_id, "get_deals", {
            "fromTimestamp": datetime.fromtimestamp(last_ts/1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "toTimestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "maxRows": 1000,
        })
        if not deals:
            break
        all_deals.extend(deals.get("deals", []))
    
    return all_deals


def group_by_position(deals):
    """Group deals by positionId, return list of trade dicts."""
    positions = {}
    for d in deals:
        pid = d.get("positionId", "unknown")
        if pid not in positions:
            positions[pid] = []
        positions[pid].append(d)
    
    trades = []
    for pid, deals in positions.items():
        deals.sort(key=lambda d: d.get("executionTimestamp", 0))
        
        entry_deal = deals[0]
        exit_deal = deals[-1] if len(deals) > 1 else None
        
        entry_price = entry_deal.get("executionPrice", 0)
        exit_price = exit_deal.get("executionPrice", entry_price) if exit_deal else entry_price
        direction = entry_deal.get("tradeSide", "BUY")  # BUY or SELL
        vol_cents = entry_deal.get("volume", 0)
        vol_lots = vol_cents / 10000 if vol_cents else 0  # 200 cents = 0.02 lots
        vol_oz = vol_lots * 100
        
        # Calculate PnL
        if direction == "BUY":
            pnl = (exit_price - entry_price) * vol_oz
        else:
            pnl = (entry_price - exit_price) * vol_oz
        
        # Subtract commission
        total_comm = sum(d.get("commission", 0) for d in deals) / 100  # cents to dollars
        pnl_net = pnl - total_comm
        
        # Determine reason from exit deal
        reason = "CLOSED"
        if len(deals) == 1:
            reason = "OPEN"
        elif exit_deal:
            # Try to infer reason from time difference
            entry_ts = entry_deal.get("executionTimestamp", 0)
            exit_ts = exit_deal.get("executionTimestamp", 0)
            time_diff_min = (exit_ts - entry_ts) / 60000
            if time_diff_min < 5:
                reason = "SCALP"
            elif time_diff_min > 120:
                reason = "TIME"
            else:
                reason = "TP_OR_SL"
        
        ts = exit_deal.get("executionTimestamp", entry_deal.get("executionTimestamp", 0)) if exit_deal else entry_deal.get("executionTimestamp", 0)
        ts_iso = datetime.fromtimestamp(ts/1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        
        trade = {
            "ts": ts_iso,
            "type": "SHORT" if direction == "SELL" else "LONG",
            "entry_price": round(entry_price, 2),
            "exit_price": round(exit_price, 2),
            "pnl": round(pnl_net, 2),
            "reason": reason,
            "entries": 1,
            "version": "ctrader-backfill",
            "position_id": pid,
            "volume_lots": round(vol_lots, 4),
        }
        trades.append(trade)
    
    trades.sort(key=lambda t: t["ts"])
    return trades


async def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7, help="Days to fetch (default 7, max 30)")
    args = p.parse_args()
    
    print(f"=== Fetching deals from cTrader (last {args.days} days) ===")
    
    async with httpx.AsyncClient(timeout=30) as client:
        # Connect
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "backfill", "version": "1.0"}}}
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {MCP_BEARER_TOKEN}"
        }
        resp = await client.post(MCP_URL, json=body, headers=headers)
        session_id = resp.headers.get("Mcp-Session-Id")
        headers["Mcp-Session-Id"] = session_id
        await client.post(MCP_URL, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers)
        
        # Fetch deals
        deals = await fetch_all_deals(client, session_id, args.days)
        print(f"Fetched {len(deals)} deals")
        
        if not deals:
            print("No deals found")
            return
        
        # Group into trades
        trades = group_by_position(deals)
        print(f"Grouped into {len(trades)} trades")
        
        # Write to trade log (replace)
        with open(TRADE_LOG_PATH, "w") as f:
            for t in trades:
                f.write(json.dumps(t, ensure_ascii=False) + "\n")
        
        print(f"Wrote {len(trades)} trades to {TRADE_LOG_PATH}")
        
        # Summary
        total_pnl = sum(t["pnl"] for t in trades)
        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] < 0]
        print()
        print("=== SUMMARY ===")
        print(f"Total PnL: ${total_pnl:+.2f}")
        print(f"Wins: {len(wins)} | Losses: {len(losses)}")
        print(f"Win rate: {len(wins)/len(trades)*100:.1f}%")
        if wins:
            print(f"Avg win: ${sum(t['pnl'] for t in wins)/len(wins):+.2f}")
        if losses:
            print(f"Avg loss: ${sum(t['pnl'] for t in losses)/len(losses):+.2f}")


asyncio.run(main())
