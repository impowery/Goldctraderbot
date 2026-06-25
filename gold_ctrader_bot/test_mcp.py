"""Quick MCP connection test."""
import httpx, json, asyncio

async def test():
    async with httpx.AsyncClient() as c:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "test", "version": "1.0"}}}
        r = await c.post("http://127.0.0.1:9876/mcp/", json=body, headers=headers)
        sid = r.headers.get("Mcp-Session-Id", "")
        d = r.json()
        print(f"Session: {sid}")
        print(f"Server: {d['result']['serverInfo']}")

        headers["Mcp-Session-Id"] = sid
        body2 = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        r2 = await c.post("http://127.0.0.1:9876/mcp/", json=body2, headers=headers)
        tools = r2.json()["result"]["tools"]
        print(f"Tools available: {len(tools)}")

        body3 = {"jsonrpc": "2.0", "id": 3, "method": "get_balance"}
        r3 = await c.post("http://127.0.0.1:9876/mcp/", json=body3, headers=headers)
        print(f"Balance: {json.dumps(r3.json(), indent=2)}")

asyncio.run(test())
