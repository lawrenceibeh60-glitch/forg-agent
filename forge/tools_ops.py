import os, re, json, time, shlex, signal, subprocess, base64
import httpx, connections
from pathlib import Path
from tools import tool, safe, ROOT, sh, gh, rd
from tools_extra import detect_stack, tree, T

def tok(): return connections.get_access_token("github") or os.getenv("GITHUB_TOKEN", "")

def git_run(d, *args, timeout=120):
    """Run fixed-argument git commands on the trusted host, not the arbitrary-code sandbox."""
    d = Path(d).resolve()
    root = Path(os.getenv("WORKSPACE", "workspace")).resolve()
    if d != root and root not in d.parents:
        raise ValueError("git working directory escapes the workspace")
    env = os.environ.copy()
    token = tok()
    if token:
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
        env["GIT_CONFIG_VALUE_0"] = "AUTHORIZATION: basic " + base64.b64encode(("x-access-token:" + token).encode()).decode()
    result = subprocess.run(["git", *map(str, args)], cwd=d, env=env, capture_output=True,
                            text=True, timeout=timeout)
    output = (result.stdout + result.stderr).strip()
    if result.returncode:
        return f"git {args[0]} failed (exit {result.returncode}): {output[-2500:]}"
    return output or f"git {args[0]} completed"

def setup_git(d):
    u = gh("GET", "/user")
    if not isinstance(u, dict) or u.get("error"):
        login, uid, display = "forge", 0, ""
    else:
        login, uid, display = u.get("login", "forge"), u.get("id", 0), u.get("name", "")
    git_run(d, "config", "user.name", os.getenv("GIT_NAME") or display or login)
    git_run(d, "config", "user.email", os.getenv("GIT_EMAIL") or f"{uid}+{login}@users.noreply.github.com")

@tool("repo_open", "Clone or update a GitHub repo into the workspace, configure git identity, optionally switch/create a branch, and return its stack and file tree. Use this to begin repository tasks.", {"repo": ("string", "owner/name"), "branch?": ("string", "optional branch to check out or create")})
def repo_open(repo, branch=""):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo or ""):
        return "Invalid repository. Use owner/name."
    if not tok():
        return "GitHub is not connected. Connect GitHub in Settings or configure GITHUB_TOKEN."
    name = repo.split("/")[1]
    d = safe(name)
    if (d / ".git").exists():
        out = git_run(d, "fetch", "--all", "-q")
        if "failed" not in out:
            out += "\n" + git_run(d, "pull", "--ff-only")
    else:
        out = git_run(ROOT.get(), "clone", f"https://github.com/{repo}.git", name)
    if "failed" in out:
        return out
    setup_git(d)
    if branch:
        exists = subprocess.run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=d).returncode == 0
        out += "\n" + (git_run(d, "checkout", branch) if exists else git_run(d, "checkout", "-b", branch))
    return f"{out}\nrepo_dir: {name}\n{detect_stack(name)}\n{tree(name, 2)}"

@tool("repo_commit_push", "Stage changes, commit, and push the current feature branch to GitHub. Never commits to main/master.", {"repo_dir": ("string", "folder"), "message": ("string", "commit message"), "branch?": ("string", "optional feature branch to use/create")})
def repo_commit_push(repo_dir, message, branch=""):
    d = safe(repo_dir)
    if not (d / ".git").is_dir():
        return f"{repo_dir} is not a cloned Git repository. Use repo_open first."
    if not tok():
        return "GitHub is not connected. Connect GitHub in Settings or configure GITHUB_TOKEN."
    cur = git_run(d, "branch", "--show-current")
    if "failed" in cur:
        return cur
    if branch and branch != cur:
        exists = subprocess.run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=d).returncode == 0
        out = git_run(d, "checkout", branch) if exists else git_run(d, "checkout", "-b", branch)
        if "failed" in out:
            return out
        cur = branch
    if cur in ("main", "master", ""):
        return "Refused to commit directly to main/master. Create or select a feature branch and retry."
    staged = git_run(d, "add", "-A")
    if "failed" in staged:
        return staged
    status = git_run(d, "status", "--porcelain")
    if not status:
        return "No changes to commit; working tree is clean."
    committed = git_run(d, "commit", "-m", message)
    if "failed" in committed:
        return committed
    pushed = git_run(d, "push", "-u", "origin", cur)
    return f"{committed}\n{pushed}"

@tool("github_create_repo", "Create a new GitHub repository.", {"name": ("string", ""), "private?": ("boolean", "default true"), "description?": ("string", "")})
def github_create_repo(name, private=True, description=""):
    r = gh("POST", "/user/repos", json={"name": name, "private": private, "description": description})
    return r.get("html_url") or json.dumps(r)[:300]

@tool("publish_to_github", "Turn a sandbox folder into a new GitHub repo and push it (init, commit, create repo, push main).", {"dir": ("string", "sandbox folder"), "repo_name": ("string", ""), "private?": ("boolean", "default true")})
def publish_to_github(dir, repo_name, private=True):
    d = safe(dir); r = gh("POST", "/user/repos", json={"name": repo_name, "private": private})
    if "error" in r: return json.dumps(r)[:300]
    init = git_run(d, "init", "-b", "main")
    if "failed" in init: return init
    setup_git(d)
    added = git_run(d, "add", "-A")
    if "failed" in added: return added
    committed = git_run(d, "commit", "-m", "Initial commit")
    if "failed" in committed: return committed
    git_run(d, "remote", "remove", "origin")
    remote = git_run(d, "remote", "add", "origin", f"https://github.com/{r['owner']['login']}/{repo_name}.git")
    if "failed" in remote: return remote
    pushed = git_run(d, "push", "-u", "origin", "main")
    return f"{r['html_url']}\n{committed}\n{pushed}"

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

def _bg_name(name):
    value = re.sub(r"[^A-Za-z0-9_-]", "-", str(name or "process"))[:60].strip("-")
    return value or "process"

@tool("bg_start", "Start a long-running process inside the persistent isolated Vercel Linux sandbox; returns first log lines.", {"name": ("string", "label"), "command": ("string", ""), "cwd?": ("string", "")})
def bg_start(name, command, cwd="."):
    label = _bg_name(name)
    bg_stop(label)
    folder = safe(cwd)
    if not folder.is_dir():
        return f"working directory does not exist: {cwd}"
    remote_dir = ".forge"
    shell = ("mkdir -p .forge; nohup bash -lc " + shlex.quote(command) +
             " >.forge/" + label + ".log 2>&1 </dev/null & echo $! >.forge/" + label + ".pid; "
             "sleep 2; echo PID=$(cat .forge/" + label + ".pid); tail -n 20 .forge/" + label + ".log 2>/dev/null || true")
    return sh(shell, folder, 20)

@tool("bg_logs", "Tail the log and report status of a background process running in the persistent Vercel Sandbox.", {"name": ("string", ""), "lines?": ("integer", "")})
def bg_logs(name, lines=60):
    label = _bg_name(name)
    count = max(1, min(int(lines), 250))
    command = ("if [ -f .forge/" + label + ".pid ] && kill -0 $(cat .forge/" + label + ".pid) 2>/dev/null; "
               "then echo '[running]'; else echo '[stopped or exited]'; fi; "
               "tail -n " + str(count) + " .forge/" + label + ".log 2>/dev/null || echo '(no log)'")
    return sh(command, ROOT.get(), 20)

@tool("bg_stop", "Stop a background process inside the persistent Vercel Sandbox.", {"name": ("string", "")})
def bg_stop(name):
    label = _bg_name(name)
    command = ("if [ -f .forge/" + label + ".pid ]; then P=$(cat .forge/" + label + ".pid); "
               "kill \"$P\" 2>/dev/null && echo stopped || echo 'not running'; "
               "rm -f .forge/" + label + ".pid; else echo 'not running'; fi")
    return sh(command, ROOT.get(), 15)

@tool("bg_list", "List background processes in the current persistent Vercel Sandbox.", {})
def bg_list():
    command = "mkdir -p .forge; for f in .forge/*.pid; do [ -f \"$f\" ] || continue; n=$(basename \"$f\" .pid); p=$(cat \"$f\"); if kill -0 \"$p\" 2>/dev/null; then echo \"$n: running pid $p\"; else echo \"$n: exited pid $p\"; fi; done"
    return sh(command, ROOT.get(), 15)

# ===================== Render =====================
@tool("render_owners", "List Render workspaces (owner ids).", {})
def render_owners():
    data = rd("GET", "/owners")
    rows = _as_api_list(data, "Render owners endpoint")
    if isinstance(rows, str):
        return rows
    output = []
    for item in rows:
        owner = item.get("owner", item) if isinstance(item, dict) else {}
        if isinstance(owner, dict) and owner.get("id"):
            output.append(f"{owner['id']}  {owner.get('name', '(unnamed)')}")
    return "\n".join(output) or "Render returned no owners."

@tool("render_service_info", "Details of a Render service: repo, branch, URL, auto-deploy, status.", {"service_id": ("string", "srv-...")})
def render_service_info(service_id):
    s = rd("GET", f"/services/{service_id}"); d = s.get("serviceDetails", {})
    return "\n".join(f"{k}: {v}" for k, v in {"name": s.get("name"), "type": s.get("type"), "repo": s.get("repo"), "branch": s.get("branch"), "url": d.get("url"), "autoDeploy": s.get("autoDeploy"), "suspended": s.get("suspended"), "plan": d.get("plan")}.items())

@tool("render_env_vars", "List a service's environment variable NAMES (values hidden).", {"service_id": ("string", "")})
def render_env_vars(service_id):
    data = rd("GET", f"/services/{service_id}/env-vars")
    rows = _as_api_list(data, "Render environment variables endpoint")
    if isinstance(rows, str):
        return rows
    output = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        env = item.get("envVar", item)
        if isinstance(env, dict) and env.get("key"):
            output.append(env["key"])
    return "\n".join(output) or "No environment variable names returned."

def _act(service_id, verb): return json.dumps(rd("POST", f"/services/{service_id}/{verb}"))[:200]
@tool("render_restart", "Restart a Render service.", {"service_id": ("string", "")})
def render_restart(service_id): return _act(service_id, "restart")
@tool("render_suspend", "Suspend a Render service.", {"service_id": ("string", "")})
def render_suspend(service_id): return _act(service_id, "suspend")
@tool("render_resume", "Resume a suspended Render service.", {"service_id": ("string", "")})
def render_resume(service_id): return _act(service_id, "resume")

@tool("render_wait_deploy", "Wait until the latest deploy finishes (live or failed). If it fails, read render_logs.", {"service_id": ("string", ""), "timeout?": ("integer", "seconds, default 300")})
def render_wait_deploy(service_id, timeout=300):
    end, st = time.time() + max(1, int(timeout)), "unknown"
    while time.time() < end:
        data = rd("GET", f"/services/{service_id}/deploys?limit=1")
        rows = _as_api_list(data, "Render deploys endpoint")
        if isinstance(rows, str):
            return rows
        if not rows:
            st = "no deploys returned"
            break
        first = rows[0]
        deploy = first.get("deploy", first) if isinstance(first, dict) else {}
        if not isinstance(deploy, dict):
            return "Render deploy response had an unexpected structure."
        st = str(deploy.get("status", "unknown"))
        if st in ("live", "build_failed", "update_failed", "canceled", "deactivated"):
            break
        time.sleep(min(10, max(1, end - time.time())))
    return f"deploy status: {st}" + (" — check render_logs" if "failed" in st else "")

@tool("render_create_service", "Create a new Render web service from a GitHub repo URL (auto-deploys on push).", {"name": ("string", ""), "repo_url": ("string", "https://github.com/owner/repo"), "runtime": ("string", "python|node|docker|go|ruby|rust|static"), "build_command": ("string", ""), "start_command": ("string", ""), "branch?": ("string", ""), "plan?": ("string", "free|starter..."), "region?": ("string", "oregon|frankfurt|...")})
def render_create_service(name, repo_url, runtime, build_command, start_command, branch="main", plan="free", region="oregon"):
    owner = rd("GET", "/owners")[0]["owner"]["id"]
    body = {"type": "web_service", "name": name, "ownerId": owner, "repo": repo_url, "branch": branch, "autoDeploy": "yes",
            "serviceDetails": {"runtime": runtime, "plan": plan, "region": region, "envSpecificDetails": {"buildCommand": build_command, "startCommand": start_command}}}
    return json.dumps(rd("POST", "/services", json=body))[:500]
