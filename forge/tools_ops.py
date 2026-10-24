"""Sandbox, repo workflow, background processes and Render operations."""
import os, re, json, time, shlex, signal, subprocess, base64
import httpx
from tools import tool, safe, ROOT, sh, gh, rd
from tools_extra import detect_stack, tree, T

def tok(): return os.environ["GITHUB_TOKEN"]
def setup_git(d):
    u = gh("GET", "/user")
    n = os.getenv("GIT_NAME") or u.get("name") or u.get("login", "forge")
    e = os.getenv("GIT_EMAIL") or f"{u.get('id', 0)}+{u.get('login', 'forge')}@users.noreply.github.com"
    sh(f"git config user.name {shlex.quote(n)} && git config user.email {shlex.quote(e)}", d)

# ===================== repo workflow =====================
@tool("repo_open", "Clone (or update) a GitHub repo into the sandbox, set git identity, optionally switch/create a branch, and return its stack + file tree. Start every repo task with this.", {"repo": ("string", "owner/name"), "branch?": ("string", "branch to check out or create")})
def repo_open(repo, branch=""):
    name = repo.split("/")[1]; d = safe(name)
    out = sh("git fetch --all -q; git pull --ff-only", d) if (d / ".git").exists() else sh(f"git clone https://{tok()}@github.com/{repo}.git {name}")
    setup_git(d)
    if branch: out += "\n" + sh(f"git checkout {shlex.quote(branch)} 2>/dev/null || git checkout -b {shlex.quote(branch)}", d)
    return f"{out}\nrepo_dir: {name}\n{detect_stack(name)}\n{tree(name, 2)}"

@tool("repo_commit_push", "Stage everything, commit and push to a feature branch. Refuses main/master.", {"repo_dir": ("string", "folder"), "message": ("string", "commit message"), "branch?": ("string", "branch to use/create")})
def repo_commit_push(repo_dir, message, branch=""):
    d = safe(repo_dir); cur = sh("git rev-parse --abbrev-ref HEAD", d).split("\n")[0]
    if branch and branch != cur:
        sh(f"git checkout {shlex.quote(branch)} 2>/dev/null || git checkout -b {shlex.quote(branch)}", d); cur = branch
    if cur in ("main", "master", "HEAD"): return "refused: pass branch=<feature-branch>, never commit to main"
    return sh(f"git add -A && git commit -m {shlex.quote(message)}", d) + "\n" + sh(f"git push -u origin {shlex.quote(cur)}", d)

@tool("github_create_repo", "Create a new GitHub repository.", {"name": ("string", ""), "private?": ("boolean", "default true"), "description?": ("string", "")})
def github_create_repo(name, private=True, description=""):
    r = gh("POST", "/user/repos", json={"name": name, "private": private, "description": description})
    return r.get("html_url") or json.dumps(r)[:300]

@tool("publish_to_github", "Turn a sandbox folder into a new GitHub repo and push it (init, commit, create repo, push main).", {"dir": ("string", "sandbox folder"), "repo_name": ("string", ""), "private?": ("boolean", "default true")})
def publish_to_github(dir, repo_name, private=True):
    d = safe(dir); r = gh("POST", "/user/repos", json={"name": repo_name, "private": private})
    if "error" in r: return json.dumps(r)[:300]
    sh("git init -q -b main", d); setup_git(d)
    o = sh(f"git add -A && git commit -qm 'Initial commit'; git remote remove origin 2>/dev/null; git remote add origin https://{tok()}@github.com/{r['owner']['login']}/{repo_name}.git && git push -u origin main", d)
    return f"{r['html_url']}\n{o}"

@tool("github_put_file", "Create/update a single file in a GitHub repo directly via API (no clone).", {"repo": ("string", ""), "path": ("string", ""), "content": ("string", ""), "message": ("string", ""), "branch?": ("string", "")})
def github_put_file(repo, path, content, message, branch=""):
    cur = gh("GET", f"/repos/{repo}/contents/{path}" + (f"?ref={branch}" if branch else ""))
    body = {"message": message, "content": base64.b64encode(content.encode()).decode()}
    if branch: body["branch"] = branch
    if isinstance(cur, dict) and "sha" in cur: body["sha"] = cur["sha"]
    r = gh("PUT", f"/repos/{repo}/contents/{path}", json=body)
    return "committed" if "commit" in r else json.dumps(r)[:300]

@tool("github_pr_diff", "Get the diff of a pull request for review.", {"repo": ("string", ""), "number": ("integer", "")})
def github_pr_diff(repo, number):
    r = httpx.get(f"https://api.github.com/repos/{repo}/pulls/{number}", timeout=30, headers={"Authorization": f"Bearer {tok()}", "Accept": "application/vnd.github.diff"})
    return T(r.text, 12000)

@tool("github_merge_pr", "Merge a pull request.", {"repo": ("string", ""), "number": ("integer", ""), "method?": ("string", "squash|merge|rebase")})
def github_merge_pr(repo, number, method="squash"): return json.dumps(gh("PUT", f"/repos/{repo}/pulls/{number}/merge", json={"merge_method": method}))[:300]

# ===================== sandbox =====================
TPL = {
 "fastapi": {"main.py": "from fastapi import FastAPI\n\napp = FastAPI()\n\n@app.get('/')\ndef root():\n    return {'app': '{n}', 'ok': True}\n", "requirements.txt": "fastapi\nuvicorn\npytest\nhttpx\n", "README.md": "# {n}\n\nRun: `uvicorn main:app --port 8000`\n"},
 "node": {"index.js": "const http = require('http');\nhttp.createServer((q, s) => s.end('{n} ok')).listen(process.env.PORT || 3000);\n", "package.json": '{"name": "{n}", "version": "1.0.0", "scripts": {"start": "node index.js"}}\n'},
 "static": {"index.html": "<!doctype html>\n<meta charset=utf-8>\n<title>{n}</title>\n<h1>{n}</h1>\n"},
 "python-cli": {"main.py": "def main():\n    print('hello from {n}')\n\nif __name__ == '__main__':\n    main()\n", "test_main.py": "from main import main\n\ndef test_main(capsys):\n    main()\n    assert 'hello' in capsys.readouterr().out\n"},
}
@tool("sandbox_new_project", "Scaffold a starter project in the sandbox. Templates: fastapi, node, static, python-cli (or 'empty').", {"name": ("string", "folder name"), "template": ("string", "fastapi|node|static|python-cli|empty")})
def sandbox_new_project(name, template):
    d = safe(name); d.mkdir(parents=True, exist_ok=True)
    for f, c in TPL.get(template, {}).items(): (d / f).write_text(c.replace("{n}", name))
    return f"created {name}/ with: {', '.join(TPL.get(template, {})) or 'nothing (empty)'}"

@tool("sandbox_info", "Show the sandbox location, size, file count and running background processes.", {})
def sandbox_info():
    r = ROOT.get(); fs = [p for p in r.rglob("*") if p.is_file() and ".git" not in p.parts and ".forge" not in p.parts]
    return f"sandbox: {r.name}\nfiles: {len(fs)}, {sum(p.stat().st_size for p in fs) // 1024} KB\nprocesses:\n{bg_list()}"

PROCS = {}
def _tail(f, n=60): return "\n".join(f.read_text(errors="replace").splitlines()[-n:]) if f.exists() else "(no log)"
def _key(name): return (str(ROOT.get()), name)

@tool("bg_start", "Start a long-running process (dev server, watcher) in the background; returns first log lines.", {"name": ("string", "label"), "command": ("string", ""), "cwd?": ("string", "")})
def bg_start(name, command, cwd="."):
    bg_stop(name); log = ROOT.get() / ".forge" / f"{name}.log"; log.parent.mkdir(exist_ok=True)
    PROCS[_key(name)] = subprocess.Popen(command, shell=True, cwd=safe(cwd), stdout=open(log, "w"), stderr=subprocess.STDOUT, start_new_session=True)
    time.sleep(2.5); return f"started '{name}' pid {PROCS[_key(name)].pid}\n{_tail(log, 20)}"

@tool("bg_logs", "Tail the log of a background process.", {"name": ("string", ""), "lines?": ("integer", "")})
def bg_logs(name, lines=60):
    p = PROCS.get(_key(name)); st = "not started" if not p else ("running" if p.poll() is None else f"exited {p.poll()}")
    return f"[{st}]\n{_tail(ROOT.get() / '.forge' / f'{name}.log', int(lines))}"

@tool("bg_stop", "Stop a background process.", {"name": ("string", "")})
def bg_stop(name):
    p = PROCS.pop(_key(name), None)
    if p and p.poll() is None:
        try: os.killpg(p.pid, signal.SIGTERM)
        except Exception: p.terminate()
        return "stopped"
    return "not running"

@tool("bg_list", "List background processes in this sandbox.", {})
def bg_list():
    out = [f"{k[1]}: {'running' if p.poll() is None else 'exited'} pid {p.pid}" for k, p in PROCS.items() if k[0] == str(ROOT.get())]
    return "\n".join(out) or "none"

# ===================== Render =====================
@tool("render_owners", "List Render workspaces (owner ids).", {})
def render_owners(): return "\n".join(f"{o['owner']['id']}  {o['owner']['name']}" for o in rd("GET", "/owners"))

@tool("render_service_info", "Details of a Render service: repo, branch, URL, auto-deploy, status.", {"service_id": ("string", "srv-...")})
def render_service_info(service_id):
    s = rd("GET", f"/services/{service_id}"); d = s.get("serviceDetails", {})
    return "\n".join(f"{k}: {v}" for k, v in {"name": s.get("name"), "type": s.get("type"), "repo": s.get("repo"), "branch": s.get("branch"), "url": d.get("url"), "autoDeploy": s.get("autoDeploy"), "suspended": s.get("suspended"), "plan": d.get("plan")}.items())

@tool("render_env_vars", "List a service's environment variable NAMES (values hidden).", {"service_id": ("string", "")})
def render_env_vars(service_id): return "\n".join((e.get("envVar", e)).get("key", "?") for e in rd("GET", f"/services/{service_id}/env-vars"))

@tool("render_set_env", "Set an environment variable on a Render service.", {"service_id": ("string", ""), "key": ("string", ""), "value": ("string", "")})
def render_set_env(service_id, key, value): return json.dumps(rd("PUT", f"/services/{service_id}/env-vars/{key}", json={"value": value}))[:200].replace(value, "***")

def _act(service_id, verb): return json.dumps(rd("POST", f"/services/{service_id}/{verb}"))[:200]
@tool("render_restart", "Restart a Render service.", {"service_id": ("string", "")})
def render_restart(service_id): return _act(service_id, "restart")
@tool("render_suspend", "Suspend a Render service.", {"service_id": ("string", "")})
def render_suspend(service_id): return _act(service_id, "suspend")
@tool("render_resume", "Resume a suspended Render service.", {"service_id": ("string", "")})
def render_resume(service_id): return _act(service_id, "resume")

@tool("render_wait_deploy", "Wait until the latest deploy finishes (live or failed). If it fails, read render_logs.", {"service_id": ("string", ""), "timeout?": ("integer", "seconds, default 300")})
def render_wait_deploy(service_id, timeout=300):
    end, st = time.time() + int(timeout), "?"
    while time.time() < end:
        r = rd("GET", f"/services/{service_id}/deploys?limit=1")
        st = r[0]["deploy"]["status"] if r else "none"
        if st in ("live", "build_failed", "update_failed", "canceled", "deactivated"): break
        time.sleep(10)
    return f"deploy status: {st}" + (" — check render_logs" if "failed" in st else "")

@tool("render_create_service", "Create a new Render web service from a GitHub repo URL (auto-deploys on push).", {"name": ("string", ""), "repo_url": ("string", "https://github.com/owner/repo"), "runtime": ("string", "python|node|docker|go|ruby|rust|static"), "build_command": ("string", ""), "start_command": ("string", ""), "branch?": ("string", ""), "plan?": ("string", "free|starter..."), "region?": ("string", "oregon|frankfurt|...")})
def render_create_service(name, repo_url, runtime, build_command, start_command, branch="main", plan="free", region="oregon"):
    owner = rd("GET", "/owners")[0]["owner"]["id"]
    body = {"type": "web_service", "name": name, "ownerId": owner, "repo": repo_url, "branch": branch, "autoDeploy": "yes",
            "serviceDetails": {"runtime": runtime, "plan": plan, "region": region, "envSpecificDetails": {"buildCommand": build_command, "startCommand": start_command}}}
    return json.dumps(rd("POST", "/services", json=body))[:500]
