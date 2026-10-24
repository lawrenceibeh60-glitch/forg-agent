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
    """Execute in an explicitly enabled, resource-limited Docker sandbox by default."""
    root = ROOT.get().resolve()
    cwd = (cwd or ROOT.get()).resolve()
    if root != cwd and root not in cwd.parents:
        raise ValueError("command working directory escapes the active workspace")
    if os.getenv("ALLOW_SHELL", "0") != "1":
        return "command execution is disabled by policy (ALLOW_SHELL=0)"
    if os.getenv("SANDBOX_DOCKER", "0") == "1":
        rel = os.path.relpath(cwd, root)
        image = os.getenv("SANDBOX_IMAGE", "nikolaik/python-nodejs:python3.12-nodejs20")
        network = "bridge" if os.getenv("SANDBOX_NETWORK", "0") == "1" else "none"
        uid = os.getuid() if hasattr(os, "getuid") else 1000
        gid = os.getgid() if hasattr(os, "getgid") else 1000
        argv = [
            "docker", "run", "--rm", "--network", network,
            "--memory=1g", "--cpus=1", "--pids-limit=128",
            "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--user", f"{uid}:{gid}", "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m",
            "--mount", f"type=bind,src={root},dst=/workspace",
            "--workdir", "/workspace" if rel == "." else "/workspace/" + rel,
            image, "sh", "-lc", cmd,
        ]
        try:
            r = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            return "Docker sandbox requested but Docker is unavailable; refusing to execute on the host."
    elif os.getenv("SANDBOX_ALLOW_HOST_EXECUTION", "0") == "1":
        # Explicit development-only escape hatch. Never enable on a public deployment.
        r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, env={"PATH": os.getenv("PATH", "/usr/bin:/bin"),
                                                "HOME": str(root), "LANG": "C.UTF-8",
                                                "WORKSPACE": str(root)})
    else:
        return ("no isolated command runner is configured. Enable SANDBOX_DOCKER=1 on a host "
                "with Docker available; host-shell fallback is disabled.")
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
    if os.getenv("ALLOW_SHELL", "0") != "1": return "shell disabled by policy (ALLOW_SHELL=0)"
    try: return sh(command, safe(cwd))
    except subprocess.TimeoutExpired: return "timed out after 120s"

@tool("update_plan", "Record a step-by-step plan / progress checklist for the user to see.", {"steps": ("string", "markdown checklist")})
def update_plan(steps): return "plan updated"

@tool("web_fetch", "Fetch a URL (docs, issues) and return text.", {"url": ("string", "https url")})
def web_fetch(url):
    r = httpx.get(url, timeout=30, follow_redirects=True)
    return re.sub(r"<[^>]+>", " ", r.text)[:8000]

# ---------- git / GitHub ----------
def _response_json(response):
    """Parse JSON APIs without assuming every response is JSON."""
    try:
        return response.json()
    except (ValueError, json.JSONDecodeError):
        return {"message": (response.text or "").strip()[:1000] or f"HTTP {response.status_code} returned an empty body"}

def _api_error(data, status=None):
    if isinstance(data, dict) and (data.get("error") or (data.get("message") and status and status >= 400)):
        body = data.get("body") or data.get("message") or data.get("error")
        details = data.get("errors")
        if details:
            body = f"{body}; details: {details}"
        return f"API error{f' ({status})' if status else ''}: {body}"
    return ""

def _as_api_list(data, label):
    """Never iterate an error object or a scalar as though it were an API array."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        err = _api_error(data, data.get("status") if isinstance(data.get("status"), int) else None)
        if err:
            return err
        return f"{label} returned an unexpected object instead of a list: {json.dumps(data)[:700]}"
    return f"{label} returned {type(data).__name__}, not a list: {str(data)[:500]}"

def gh(method, path, **kw):
    token = connections.get_access_token("github") or os.getenv("GITHUB_TOKEN", "")
    if not token:
        return {"error": "GitHub is not connected. Open Settings → Connections → Connect GitHub."}
    headers = dict(kw.pop("headers", {}) or {})
    headers.setdefault("Authorization", f"Bearer {token}")
    headers.setdefault("Accept", "application/vnd.github+json")
    headers.setdefault("X-GitHub-Api-Version", "2022-11-28")
    try:
        response = httpx.request(method, f"https://api.github.com{path}", timeout=30, headers=headers, **kw)
    except httpx.HTTPError as exc:
        return {"error": "network", "message": f"Could not reach GitHub: {type(exc).__name__}: {exc}"}
    data = _response_json(response)
    if response.status_code >= 400:
        message = data.get("message", "request failed") if isinstance(data, dict) else str(data)
        errors = data.get("errors") if isinstance(data, dict) else None
        return {"error": response.status_code, "message": message, "errors": errors, "body": (response.text or "")[:1000]}
    return data

@tool("github_list_repos", "List the user's GitHub repos.", {})
def github_list_repos():
    rows = _as_api_list(gh("GET", "/user/repos?per_page=50&sort=updated"), "GitHub repository list")
    if isinstance(rows, str):
        return rows
    output = []
    for item in rows:
        if isinstance(item, dict) and item.get("full_name"):
            output.append(f"{item['full_name']} - {item.get('description') or ''}")
    return "\\n".join(output) or "GitHub returned no repositories (or entries without full_name)."

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
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo or ""):
        return "Invalid repository. Use owner/name."
    rows = _as_api_list(gh("GET", f"/repos/{repo}/issues?state=open&per_page=30"), "GitHub issues endpoint")
    if isinstance(rows, str):
        return rows
    output = []
    for item in rows:
        if isinstance(item, dict) and item.get("number") is not None and item.get("title"):
            output.append(f"#{item['number']} {item['title']}" + (" [pull request]" if item.get("pull_request") else ""))
    return "\\n".join(output) or "No open issues were returned for this repository."

# ---------- Render ----------
def rd(method, path, **kw):
    token = os.getenv("RENDER_API_KEY", "")
    if not token:
        return {"error": "RENDER_API_KEY is not configured"}
    headers = dict(kw.pop("headers", {}) or {})
    headers.setdefault("Authorization", f"Bearer {token}")
    headers.setdefault("Accept", "application/json")
    try:
        response = httpx.request(method, f"https://api.render.com/v1{path}", timeout=30, headers=headers, **kw)
    except httpx.HTTPError as exc:
        return {"error": "network", "message": f"Could not reach Render: {type(exc).__name__}: {exc}"}
    data = _response_json(response) if response.text else {"status": response.status_code}
    if response.status_code >= 400:
        message = data.get("message", data.get("error", "request failed")) if isinstance(data, dict) else str(data)
        return {"error": response.status_code, "message": message, "body": (response.text or "")[:1000]}
    return data

@tool("render_list_services", "List Render services.", {})
def render_list_services():
    rows = _as_api_list(rd("GET", "/services?limit=50"), "Render services endpoint")
    if isinstance(rows, str):
        return rows
    output = []
    for item in rows:
        service = item.get("service", item) if isinstance(item, dict) else {}
        if isinstance(service, dict) and service.get("id"):
            output.append(f"{service['id']}  {service.get('name', '(unnamed)')}  ({service.get('type', 'unknown type')})")
    return "\\n".join(output) or "Render returned no services."

@tool("render_deploy", "Trigger a deploy of a Render service.", {"service_id": ("string", "srv-...")})
def render_deploy(service_id): return json.dumps(rd("POST", f"/services/{service_id}/deploys", json={}))[:400]

@tool("render_deploy_status", "Show recent deploys and their status.", {"service_id": ("string", "srv-...")})
def render_deploy_status(service_id):
    rows = _as_api_list(rd("GET", f"/services/{service_id}/deploys?limit=5"), "Render deploys endpoint")
    if isinstance(rows, str):
        return rows
    output = []
    for item in rows:
        deploy = item.get("deploy", item) if isinstance(item, dict) else {}
        if isinstance(deploy, dict):
            output.append(f"{deploy.get('id', '(unknown id)')} {deploy.get('status', 'unknown')} {deploy.get('createdAt', 'time unavailable')}")
    return "\\n".join(output) or "Render returned no recent deploys."

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
