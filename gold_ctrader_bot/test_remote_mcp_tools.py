"""List all 16 Remote MCP tools + server instructions."""
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
        # initialize
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "test", "version": "1.0"}}}
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {MCP_BEARER_TOKEN}"
        }
        resp = await client.post(MCP_URL, json=body, headers=headers)
        # Parse SSE for instructions
        for line in resp.text.split("\n"):
            if line.startswith("data: "):
                data = line[6:].strip()
                try:
                    parsed = json.loads(data)
                    if "result" in parsed:
                        server_info = parsed["result"].get("serverInfo", {})
                        instructions = parsed["result"].get("instructions", "")
                        print("=== SERVER INFO ===")
                        print(f"Name: {server_info.get('name')}")
                        print(f"Version: {server_info.get('version')}")
                        print()
                        print("=== INSTRUCTIONS ===")
                        print(instructions)
                        break
                except:
                    pass

        session_id = resp.headers.get("Mcp-Session-Id")
        headers["Mcp-Session-Id"] = session_id

        # tools/list
        body3 = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        resp3 = await client.post(MCP_URL, json=body3, headers=headers)
        for line in resp3.text.split("\n"):
            if line.startswith("data: "):
                data = line[6:].strip()
                try:
                    parsed = json.loads(data)
                    if "result" in parsed:
                        tools = parsed["result"].get("tools", [])
                        print()
                        print(f"=== ALL {len(tools)} TOOLS ===")
                        for i, t in enumerate(tools):
                            name = t.get("name")
                            desc = t.get("description", "")[:100]
                            schema = t.get("inputSchema", {}).get("properties", {})
                            props = list(schema.keys()) if schema else []
                            print(f"  {i+1:2d}. {name}")
                            print(f"      desc: {desc}")
                            if props:
                                print(f"      args: {props}")
                        break
                except:
                    pass


asyncio.run(test())
