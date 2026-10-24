import os, re, json, subprocess, httpx, connections
from pathlib import Path

from contextvars import ContextVar
BASE = Path(os.getenv("WORKSPACE", "workspace")).resolve(); BASE.mkdir(exist_ok=True)
ROOT = ContextVar("root", default=BASE)
class _WS(os.PathLike):  # always points at the current chat's sandbox
    def __fspath__(self): return str(ROOT.get())
    def __truediv__(self, o): return ROOT.get() / o
    def __getattr__(self, n): return getattr(ROOT.get(), n)
    __str__ = __fspath__
WS = _WS()
def use(sid):
    r = BASE / (re.sub(r"\W", "", sid or "") or "default")[:16]; r.mkdir(exist_ok=True); ROOT.set(r); return r
REG, SCHEMAS = {}, []

def tool(name, desc, params):
    """params: {arg: (type, description)}; suffix arg with ? for optional."""
    props, req = {}, []
    for k, (t, d) in params.items():
        opt = k.endswith("?"); k = k.rstrip("?")
        props[k] = {"type": t, "description": d}
        if not opt: req.append(k)
    SCHEMAS.append({"type": "function", "function": {"name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": req}}})
    def deco(fn): REG[name] = fn; return fn
    return deco

def safe(p):
    r = ROOT.get(); full = (r / p).resolve()
    if r not in full.parents and full != r: raise ValueError("path escapes the sandbox")
    return full

def sh(cmd, cwd=None, timeout=120):
    import shlex
    root, cwd = ROOT.get(), cwd or ROOT.get()
    if os.getenv("SANDBOX_DOCKER") == "1" and not cmd.startswith("git "):  # optional hard isolation
        rel = os.path.relpath(cwd, root)
        cmd = f"docker run --rm -v {shlex.quote(str(root))}:/work -w {shlex.quote('/work/' + rel)} {os.getenv('SANDBOX_IMAGE', 'nikolaik/python-nodejs:python3.12-nodejs20')} sh -c {shlex.quote(cmd)}"; cwd = root
    r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    out = r.stdout + r.stderr
    token = connections.get_access_token("github") or os.getenv("GITHUB_TOKEN", "")
    if token: out = out.replace(token, "***")
    return (out.strip() or "(no output)")[-6000:] + f"\n[exit {r.returncode}]"

# ---------- filesystem & code ----------
@tool("list_files", "List files in the workspace (recursive, skips .git/node_modules).", {"path?": ("string", "subdirectory")})
def list_files(path="."):
    out = []
    for p in sorted(safe(path).rglob("*")):
        if any(x in p.parts for x in (".git", "node_modules", "__pycache__", ".venv")): continue
        if p.is_file(): out.append(str(p.relative_to(WS)))
    return "\n".join(out[:500]) or "(empty)"

@tool("read_file", "Read a text file, optionally a line range.", {"path": ("string", "file path"), "start?": ("integer", "1-based start line"), "end?": ("integer", "end line")})
def read_file(path, start=1, end=None):
    lines = safe(path).read_text(errors="replace").splitlines()
    s = max(start - 1, 0)
    return "\n".join(f"{i+s+1}: {l}" for i, l in enumerate(lines[s:end]))[:20000]

@tool("write_file", "Create or overwrite a file.", {"path": ("string", "file path"), "content": ("string", "full content")})
def write_file(path, content):
    p = safe(path); p.parent.mkdir(parents=True, exist_ok=True); p.write_text(content)
    return f"wrote {len(content)} bytes to {path}"

@tool("edit_file", "Replace an exact unique string in a file. Prefer over write_file for changes.", {"path": ("string", "file"), "old": ("string", "exact text to replace"), "new": ("string", "replacement")})
def edit_file(path, old, new):
    p = safe(path); t = p.read_text()
    if t.count(old) != 1: return f"error: old text found {t.count(old)} times; must be exactly 1"
    p.write_text(t.replace(old, new)); return "edited"

@tool("search_code", "Regex search across workspace files.", {"pattern": ("string", "regex"), "glob?": ("string", "e.g. *.py")})
def search_code(pattern, glob="*"):
    rx, hits = re.compile(pattern), []
    for p in WS.rglob(glob):
        if not p.is_file() or ".git" in p.parts or "node_modules" in p.parts: continue
        try:
            for i, l in enumerate(p.read_text().splitlines(), 1):
                if rx.search(l): hits.append(f"{p.relative_to(WS)}:{i}: {l.strip()[:160]}")
        except Exception: pass
    return "\n".join(hits[:100]) or "no matches"

@tool("run_command", "Run a shell command in the workspace (tests, installs, builds, linters).", {"command": ("string", "shell command"), "cwd?": ("string", "subdirectory")})
def run_command(command, cwd="."):
    if os.getenv("ALLOW_SHELL", "1") != "1": return "shell disabled (ALLOW_SHELL=0)"
    try: return sh(command, safe(cwd))
    except subprocess.TimeoutExpired: return "timed out after 120s"

@tool("update_plan", "Record a step-by-step plan / progress checklist for the user to see.", {"steps": ("string", "markdown checklist")})
def update_plan(steps): return "plan updated"

@tool("web_fetch", "Fetch a URL (docs, issues) and return text.", {"url": ("string", "https url")})
def web_fetch(url):
    r = httpx.get(url, timeout=30, follow_redirects=True)
    return re.sub(r"<[^>]+>", " ", r.text)[:8000]

# ---------- git / GitHub ----------
def gh(method, path, **kw):
    token = connections.get_access_token("github") or os.getenv("GITHUB_TOKEN", "")
    if not token:
        return {"error": "GitHub not connected. Open Settings → Connections → Connect GitHub."}
    r = httpx.request(method, f"https://api.github.com{path}", timeout=30, **kw,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    return r.json() if r.status_code < 400 else {"error": r.status_code, "body": r.text[:300]}

@tool("github_list_repos", "List the user's GitHub repos.", {})
def github_list_repos():
    return "\n".join(f"{r['full_name']} - {r.get('description') or ''}" for r in gh("GET", "/user/repos?per_page=50&sort=updated"))

@tool("git_clone", "Clone a GitHub repo (owner/name) into the workspace.", {"repo": ("string", "owner/name")})
def git_clone(repo):
    token = connections.get_access_token("github") or os.getenv("GITHUB_TOKEN", "")
    if not token:
        return "GitHub not connected. Open Settings → Connections → Connect GitHub."
    url = f"https://x-access-token:{token}@github.com/{repo}.git"
    return sh(f"git clone {url} {repo.split('/')[1]}").replace(token, "***")

@tool("git", "Run a git command inside a cloned repo (status, diff, checkout -b, add, commit, push).", {"repo_dir": ("string", "folder name"), "args": ("string", "git arguments")})
def git(repo_dir, args):
    if args.split()[0] in ("reset", "clean") and "--hard" in args + "-fd": return "refused: destructive"
    return sh(f"git {args}", safe(repo_dir))

@tool("github_create_pr", "Open a pull request.", {"repo": ("string", "owner/name"), "head": ("string", "branch"), "base": ("string", "target branch"), "title": ("string", "title"), "body?": ("string", "description")})
def github_create_pr(repo, head, base, title, body=""):
    return json.dumps(gh("POST", f"/repos/{repo}/pulls", json={"head": head, "base": base, "title": title, "body": body}))[:600]

@tool("github_issues", "List open issues of a repo.", {"repo": ("string", "owner/name")})
def github_issues(repo):
    return "\n".join(f"#{i['number']} {i['title']}" for i in gh("GET", f"/repos/{repo}/issues?state=open&per_page=30"))

# ---------- Render ----------
def rd(method, path, **kw):
    r = httpx.request(method, f"https://api.render.com/v1{path}", timeout=30, **kw,
        headers={"Authorization": f"Bearer {os.environ['RENDER_API_KEY']}", "Accept": "application/json"})
    return r.json() if r.text else {"status": r.status_code}

@tool("render_list_services", "List Render services.", {})
def render_list_services():
    return "\n".join(f"{s['service']['id']}  {s['service']['name']}  ({s['service']['type']})" for s in rd("GET", "/services?limit=50"))

@tool("render_deploy", "Trigger a deploy of a Render service.", {"service_id": ("string", "srv-...")})
def render_deploy(service_id): return json.dumps(rd("POST", f"/services/{service_id}/deploys", json={}))[:400]

@tool("render_deploy_status", "Show recent deploys and their status.", {"service_id": ("string", "srv-...")})
def render_deploy_status(service_id):
    return "\n".join(f"{d['deploy']['id']} {d['deploy']['status']} {d['deploy']['createdAt']}" for d in rd("GET", f"/services/{service_id}/deploys?limit=5"))

# ---------- long-term memory ----------
MEM = Path("data/memory.md")
@tool("remember", "Save a durable fact/preference about the user or project (conventions, stack, decisions). Persists across chats.", {"note": ("string", "one concise fact")})
def remember(note):
    MEM.parent.mkdir(exist_ok=True)
    with MEM.open("a") as f: f.write(f"- {note}\n")
    return "saved"
def memory(): return MEM.read_text() if MEM.exists() else ""

@tool("render_logs", "Fetch recent runtime logs of a Render service (debug failing deploys/crashes).", {"service_id": ("string", "srv-...")})
def render_logs(service_id):
    owner = rd("GET", f"/services/{service_id}").get("ownerId")
    logs = rd("GET", f"/logs?ownerId={owner}&resource={service_id}&limit=60&direction=backward").get("logs", [])
    return "\n".join(l["message"] for l in reversed(logs))[-5000:] or "no logs"


# ---------- reasoning scratchpad & project instructions ----------
@tool("think", "Private scratchpad: reason step by step about a hard problem, tradeoffs, or a bug before acting. No side effects.", {"thought": ("string", "your reasoning")})
def think(thought): return "ok, continue"

def project_notes():
    out = []
    for d in [WS] + [p for p in sorted(WS.iterdir()) if p.is_dir() and p.name != ".git"][:8]:
        for n in ("FORGE.md", "AGENTS.md", "CLAUDE.md"):
            f = d / n
            if f.exists(): out.append(f"## {f.relative_to(WS)}\n{f.read_text(errors='replace')[:3000]}")
    return "\n\n".join(out[:3])
