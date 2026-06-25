"""Test what Remote MCP returns for tools/list request."""
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
    print(f"URL: {MCP_URL}")
    print(f"Token: {MCP_BEARER_TOKEN[:40]}...")
    print()

    async with httpx.AsyncClient(timeout=30) as client:
        # Step 1: initialize
        print("=== STEP 1: initialize ===")
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "test", "version": "1.0"}}}
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {MCP_BEARER_TOKEN}"
        }
        resp = await client.post(MCP_URL, json=body, headers=headers)
        print(f"Status: {resp.status_code}")
        print(f"Headers: {dict(resp.headers)}")
        print(f"Content-Type: {resp.headers.get('content-type')}")
        body_text = resp.text
        print(f"Body (first 500 chars): {body_text[:500]}")
        print()

        session_id = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
        if not session_id:
            print("ERROR: No session ID")
            return
        print(f"Session ID: {session_id}")

        # Step 2: notifications/initialized
        print()
        print("=== STEP 2: notifications/initialized ===")
        headers["Mcp-Session-Id"] = session_id
        body2 = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        resp2 = await client.post(MCP_URL, json=body2, headers=headers)
        print(f"Status: {resp2.status_code}")
        print(f"Body: {resp2.text[:300]}")

        # Step 3: tools/list
        print()
        print("=== STEP 3: tools/list ===")
        body3 = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        resp3 = await client.post(MCP_URL, json=body3, headers=headers)
        print(f"Status: {resp3.status_code}")
        print(f"Content-Type: {resp3.headers.get('content-type')}")
        body3_text = resp3.text
        print(f"Body length: {len(body3_text)}")
        print(f"Body (first 1000 chars): {body3_text[:1000]}")
        print()

        # Try to parse SSE
        if "text/event-stream" in resp3.headers.get("content-type", ""):
            print("=== SSE format detected, parsing ===")
            # SSE format: data: {...}\n\n
            for line in body3_text.split("\n"):
                if line.startswith("data: "):
                    data = line[6:].strip()
                    try:
                        parsed = json.loads(data)
                        if "result" in parsed:
                            tools = parsed["result"].get("tools", [])
                            print(f"Found {len(tools)} tools")
                            for t in tools[:10]:
                                print(f"  - {t.get('name')}")
                            break
                    except json.JSONDecodeError:
                        print(f"  (not JSON: {data[:100]})")


asyncio.run(test())
