import os, json, uuid, asyncio, hmac, hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()
from fastapi import FastAPI
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import re, shutil, llm, tools, tools_extra, tools_ops
from mcp_client import mcp

from prompts import SYSTEM
RISKY = {"run_command", "git", "github_create_pr", "render_deploy", "delete_file", "move_file", "bulk_replace", "python_eval", "node_eval", "pip_install", "npm_install", "http_request", "github_create_issue", "github_pr_comment", "apply_patch", "repo_commit_push", "publish_to_github", "github_create_repo", "github_put_file", "github_merge_pr", "bg_start", "render_set_env", "render_restart", "render_suspend", "render_resume", "render_create_service"}
EDIT = {"write_file", "edit_file", "apply_patch", "append_file", "bulk_replace"}
VERIFY = {"run_command", "run_tests", "lint", "python_eval", "node_eval", "bg_logs", "bg_start", "check_url", "http_request"}
DIS = Path("data/disabled.json")
def disabled(): return set(json.loads(DIS.read_text())) if DIS.exists() else set()
def schemas(): d = disabled(); return [s for s in tools.SCHEMAS + mcp.schemas() if s["function"]["name"] not in d]
CATS = [(r"^mcp__","plug"),(r"^(repo_|publish)","branch"),(r"^(bg_|sandbox)","term"),(r"^(think|update_plan|remember)","bulb"),(r"^render","cloud"),(r"^git","branch"),(r"search_code|^find_|^glob|outline","search"),(r"^(web|http|check_url|extract_links|pypi|npm_info|stack|dns|port|wiki)","globe"),(r"sql|csv|json|^env|stats|dependency","db"),(r"^(run|python|node|pip|npm_install|docker|process|lint|detect|system|apply)","term"),(r"file|dir|tree|move|delete|archive|extract|read|write|append|edit|bulk","file")]
def cat(n): return next((c for p, c in CATS if re.search(p, n)), "wrench")
SD = Path("data/sessions"); SD.mkdir(parents=True, exist_ok=True)
PENDING = {}

@asynccontextmanager
async def life(_):
    for n, c in mcp.saved().items():
        try: await mcp.connect(n, c, persist=False)
        except Exception as e: print("MCP", n, "failed:", e)
    yield
    for n in list(mcp.stops): await mcp.disconnect(n, persist=False)

app = FastAPI(lifespan=life)

def system():
    m, n = tools.memory(), tools.project_notes()
    return SYSTEM + (f"\n\nKnown about the user/project:\n{m}" if m else "") + (f"\n\nProject instructions found in the workspace:\n{n}" if n else "")

async def compact(msgs):
    """Keep long sessions sharp: prune stale tool output, then summarize old turns."""
    for m in msgs[1:-12]:
        if m["role"] == "tool" and len(m["content"]) > 600: m["content"] = m["content"][:600] + "\n…[truncated]"
    if sum(len(str(m.get("content") or "")) for m in msgs) < 80000: return
    last = max(i for i, m in enumerate(msgs) if m["role"] == "user")
    old = msgs[1:last]
    txt = "\n".join(f"{m['role']}: {str(m.get('content') or m.get('tool_calls'))[:800]}" for m in old)
    if last <= 2 or len(txt) < 8000: return
    s = ""
    async for k, v in llm.stream([{"role": "system", "content": "Summarize this coding session as notes for your future self: goals, decisions, files changed, open problems, user preferences. Dense, no fluff."},
                                  {"role": "user", "content": txt[:60000]}], []):
        if k == "message": s = v.get("content") or ""
    msgs[1:last] = [{"role": "user", "content": "[Summary of earlier conversation]\n" + s}, {"role": "assistant", "content": "Understood, continuing."}]
def load(sid):
    f = SD / f"{sid}.json"
    return json.loads(f.read_text()) if f.exists() else [{"role": "system", "content": system()}]
def save(sid, msgs): (SD / f"{sid}.json").write_text(json.dumps(msgs))
def sse(ev, data): return f"data: {json.dumps({'type': ev, **data})}\n\n"

class Req(BaseModel):
    session: str | None = None
    message: str
    auto: bool = False

class Appr(BaseModel):
    id: str
    ok: bool

@app.post("/api/chat")
async def chat(req: Req):
    sid = req.session or uuid.uuid4().hex
    msgs = load(sid); msgs[0]["content"] = system()
    msgs.append({"role": "user", "content": req.message})

    async def run():
        yield sse("session", {"id": sid})
        try:
            edited = verified = nudged = False
            for _ in range(40):
                tools.use(sid)
                await compact(msgs)
                m = None
                async for kind, val in llm.stream(msgs, schemas()):
                    if kind == "text": yield sse("delta", {"text": val})
                    else: m = val
                msgs.append({k: v for k, v in m.items() if v is not None})
                calls = m.get("tool_calls") or []
                if not calls:
                    if edited and not verified and not nudged:
                        nudged = True
                        msgs.append({"role": "user", "content": "[Automatic check] You changed files but ran nothing to verify them. Run the relevant tests/linter/build (or start the app and probe it) now and fix any failures. If verification is impossible, say exactly why."})
                        continue
                    break
                for c in calls:
                    name, raw = c["function"]["name"], c["function"]["arguments"]
                    try: args = json.loads(raw or "{}")
                    except Exception: args = {}
                    yield sse("tool_call", {"id": c["id"], "name": name, "args": args})
                    if name in EDIT: edited, verified = True, False
                    if name in VERIFY: verified = True
                    ok = True
                    if (name in RISKY or name.startswith("mcp__")) and not req.auto:
                        fut = asyncio.get_running_loop().create_future(); PENDING[c["id"]] = fut
                        yield sse("approval", {"id": c["id"], "name": name})
                        try: ok = await asyncio.wait_for(fut, 300)
                        except asyncio.TimeoutError: ok = False
                        PENDING.pop(c["id"], None)
                    try:
                        if not ok: out = "denied by user"
                        elif name.startswith("mcp__"): out = await mcp.call(name, args)
                        else: out = await asyncio.to_thread(tools.REG[name], **args)
                    except Exception as e: out = f"error: {e}"
                    yield sse("tool_result", {"id": c["id"], "output": str(out)[:4000]})
                    msgs.append({"role": "tool", "tool_call_id": c["id"], "content": str(out)[:12000]})
        except Exception as e:
            yield sse("error", {"error": str(e)})
        save(sid, msgs)
        yield sse("done", {})
    return StreamingResponse(run(), media_type="text/event-stream")

PW = os.getenv("APP_PASSWORD")
def tok(): return hmac.new(PW.encode(), b"forge", hashlib.sha256).hexdigest()

@app.middleware("http")
async def gate(request, call_next):
    p = request.url.path
    if PW and p.startswith("/api/") and p != "/api/login" and request.cookies.get("forge") != tok():
        return JSONResponse({"error": "auth"}, status_code=401)
    return await call_next(request)

@app.post("/api/login")
async def login(b: dict):
    if PW and not hmac.compare_digest(str(b.get("password", "")), PW): return JSONResponse({"error": "bad password"}, status_code=401)
    r = JSONResponse({"ok": True})
    if PW: r.set_cookie("forge", tok(), httponly=True, samesite="lax", max_age=2592000)
    return r

@app.get("/api/providers")
def prov(): return llm.public()

@app.post("/api/providers")
def prov_add(b: dict): return {"id": llm.add(b.get("preset", "custom"), b.get("key", ""), b["model"], b.get("base_url", ""), b.get("name", ""))}

@app.post("/api/providers/{pid}/activate")
def prov_act(pid: str): llm.activate(pid); return {"ok": True}

@app.delete("/api/providers/{pid}")
def prov_del(pid: str): llm.remove(pid); return {"ok": True}

@app.get("/api/tools")
def tools_list():
    d = disabled()
    return [{"name": s["function"]["name"], "desc": s["function"]["description"][:140], "cat": cat(s["function"]["name"]), "enabled": s["function"]["name"] not in d} for s in tools.SCHEMAS + mcp.schemas()]

@app.post("/api/tools/toggle")
def tools_toggle(b: dict):
    d = disabled(); (d.discard if b["enabled"] else d.add)(b["name"]); DIS.write_text(json.dumps(sorted(d))); return {"ok": True}

@app.post("/api/approve")
async def approve(a: Appr):
    f = PENDING.get(a.id)
    if f and not f.done(): f.set_result(a.ok)
    return {"ok": True}

@app.get("/api/sessions")
def sessions():
    out = []
    for f in sorted(SD.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:40]:
        m = json.loads(f.read_text()); u = next((x["content"] for x in m if x["role"] == "user"), "New chat")
        out.append({"id": f.stem, "title": u[:48]})
    return out

@app.get("/api/sessions/{sid}")
def session(sid: str):
    return [m for m in load(sid) if m["role"] in ("user", "assistant") and m.get("content") and not str(m["content"]).startswith(("[Automatic check]", "[Summary of"))]

@app.get("/api/mcp")
def mcp_list(): return {"servers": mcp.status()}

@app.post("/api/mcp")
async def mcp_add(body: dict):
    """Accepts {config: {"mcpServers": {...}}} (Claude Desktop format) or {config: {name: {...}}}."""
    cfg = body.get("config", {}); cfg = cfg.get("mcpServers", cfg); res = {}
    for n, c in cfg.items():
        try: res[n] = f"connected, {await mcp.connect(n, c)} tools"
        except Exception as e: res[n] = f"failed: {e}"
    return {"results": res}

@app.delete("/api/mcp/{name}")
async def mcp_del(name: str): await mcp.disconnect(name); return {"ok": True}

@app.get("/api/files")
def files(sid: str = ""):
    if not sid: return {"files": ["(empty)"]}
    tools.use(sid); return {"files": tools.list_files().split("\n")}

@app.get("/api/sandbox.zip")
def sandbox_zip(sid: str):
    r = tools.use(sid); return FileResponse(shutil.make_archive(f"/tmp/forge-{r.name}", "zip", r), filename="sandbox.zip")

@app.get("/healthz")
def healthz():
    return {"status": "ok"}

@app.get("/api/status")
def status():
    return {k: bool(os.getenv(v)) for k, v in
            {"xkiro": "XKIRO_API_KEY", "github": "GITHUB_TOKEN", "render": "RENDER_API_KEY"}.items()}

@app.get("/")
def index(): return FileResponse("static/index.html")
app.mount("/static", StaticFiles(directory="static"), name="static")
