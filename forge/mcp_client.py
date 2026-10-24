"""Connect to ANY MCP server (stdio, streamable-HTTP, SSE). Tools are exposed to the agent as mcp__<server>__<tool>."""
import json, re, asyncio
from contextlib import AsyncExitStack
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

CFG = Path("data/mcp_servers.json")

class Manager:
    def __init__(s): s.sess, s.tools, s.tasks, s.stops = {}, {}, {}, {}

    def saved(s): return json.loads(CFG.read_text()) if CFG.exists() else {}
    def _save(s, c): CFG.parent.mkdir(exist_ok=True); CFG.write_text(json.dumps(c, indent=1))

    async def _run(s, name, cfg, ready, stop):
        try:
            async with AsyncExitStack() as st:  # each server lives in its own task (anyio requirement)
                if "url" in cfg:
                    h = cfg.get("headers")
                    if cfg["url"].rstrip("/").endswith("sse"):
                        r, w = await st.enter_async_context(sse_client(cfg["url"], headers=h))
                    else:
                        r, w, _ = await st.enter_async_context(streamablehttp_client(cfg["url"], headers=h))
                else:
                    p = StdioServerParameters(command=cfg["command"], args=cfg.get("args", []), env=cfg.get("env"))
                    r, w = await st.enter_async_context(stdio_client(p))
                sess = await st.enter_async_context(ClientSession(r, w))
                await sess.initialize()
                for t in (await sess.list_tools()).tools:
                    full = f"mcp__{name}__{t.name}"[:64]
                    s.tools[full] = (name, t.name, {"type": "function", "function": {
                        "name": full, "description": (t.description or t.name)[:600],
                        "parameters": t.inputSchema or {"type": "object", "properties": {}}}})
                s.sess[name] = sess
                ready.set_result(sum(1 for v in s.tools.values() if v[0] == name))
                await stop.wait()
        except BaseException as e:
            if not ready.done(): ready.set_exception(e if isinstance(e, Exception) else RuntimeError(str(e)))
        finally:
            s.sess.pop(name, None)
            for k in [k for k, v in s.tools.items() if v[0] == name]: s.tools.pop(k)

    async def connect(s, name, cfg, persist=True):
        name = re.sub(r"\W", "_", name)
        await s.disconnect(name, persist=False)
        ready, stop = asyncio.get_running_loop().create_future(), asyncio.Event()
        s.stops[name], s.tasks[name] = stop, asyncio.create_task(s._run(name, cfg, ready, stop))
        n = await asyncio.wait_for(ready, 60)
        if persist: c = s.saved(); c[name] = cfg; s._save(c)
        return n

    async def disconnect(s, name, persist=True):
        if name in s.stops:
            s.stops.pop(name).set()
            try: await asyncio.wait_for(s.tasks.pop(name), 10)
            except Exception: pass
        if persist: c = s.saved(); c.pop(name, None); s._save(c)

    async def call(s, full, args):
        server, tool, _ = s.tools[full]
        res = await s.sess[server].call_tool(tool, args)
        return "\n".join(getattr(c, "text", None) or f"[{c.type} content]" for c in res.content) or "(empty)"

    def schemas(s): return [v[2] for v in s.tools.values()]
    def status(s):
        cfg = s.saved()
        return [{"name": n, "connected": n in s.sess, "tools": sum(1 for v in s.tools.values() if v[0] == n)} for n in cfg]

mcp = Manager()
