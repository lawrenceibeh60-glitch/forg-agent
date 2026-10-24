"""Live preview sessions backed by isolated, temporary Render sandboxes.

Preview URLs are public HTTPS links protected only by their random Cloudflare
Quick Tunnel hostname. Never stage credentials or .env files into a preview.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import time
import tempfile
from pathlib import Path
from typing import Any

import render_sandbox

STATE_PATH = Path(os.getenv("FORGE_PREVIEW_STATE", "data/previews.json"))
MAX_PREVIEW_SECONDS = 3600


def detect_preview(root: Path, port: int | None = None) -> dict[str, Any]:
    """Infer a conservative web start command from common project manifests."""
    root = root.resolve()
    chosen_port = max(1024, min(int(port or 5173), 65535))
    package = root / "package.json"
    if package.is_file():
        try:
            data = json.loads(package.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        scripts = data.get("scripts", {}) if isinstance(data, dict) else {}
        if "dev" in scripts:
            return {"kind": "node", "port": chosen_port, "install": "npm install",
                    "command": f"npm run dev -- --host 0.0.0.0 --port {chosen_port}"}
        if "start" in scripts:
            return {"kind": "node", "port": chosen_port, "install": "npm install",
                    "command": f"npm start -- --host 0.0.0.0 --port {chosen_port}"}
    if (root / "index.html").is_file():
        return {"kind": "static", "port": chosen_port, "install": "",
                "command": f"python -m http.server {chosen_port} --bind 0.0.0.0"}
    requirements = root / "requirements.txt"
    python_files = [name for name in ("main.py", "app.py", "server.py", "wsgi.py")
                    if (root / name).is_file()]
    for name in python_files:
        try:
            source = (root / name).read_text(encoding="utf-8", errors="replace")[:120000].lower()
        except OSError:
            source = ""
        module = Path(name).stem
        if "fastapi(" in source or "fastapi " in source or "from fastapi" in source:
            return {"kind": "asgi", "port": chosen_port,
                    "install": "python -m pip install -r requirements.txt" if requirements.is_file() else "",
                    "command": f"python -m uvicorn {module}:app --host 0.0.0.0 --port {chosen_port}"}
        if "flask(" in source or "from flask" in source or "import flask" in source:
            return {"kind": "flask", "port": chosen_port,
                    "install": "python -m pip install -r requirements.txt" if requirements.is_file() else "",
                    "command": f"python -m flask --app {module} run --host 0.0.0.0 --port {chosen_port}"}
    if requirements.is_file() and (root / "main.py").is_file():
        return {"kind": "python", "port": chosen_port,
                "install": "python -m pip install -r requirements.txt",
                "command": f"python main.py"}
    raise ValueError("I couldn't identify a web entry point. Supply a start command and port, for example 'python -m uvicorn main:app --host 0.0.0.0 --port 8000'.")


def _state() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict[str, dict[str, Any]]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _events(api, sandbox_id: str, command: str, timeout: int = 180) -> tuple[str, int | None]:
    output: list[str] = []
    exit_code = None
    for event in api.exec(sandbox_id, command):
        if hasattr(event, "exit_code"):
            exit_code = event.exit_code
        elif getattr(event, "data", ""):
            output.append(str(event.data))
    return "".join(output).strip()[-6000:], exit_code


def start_preview(session: str, root: Path, path: str = ".", command: str = "",
                  port: int = 5173, install_command: str = "") -> str:
    """Create a temporary isolated sandbox and return a public HTTPS preview link."""
    if not render_sandbox.configured():
        return "Preview unavailable: Render sandbox is not configured. Check the sandbox status endpoint; the preview feature cannot bypass missing sandbox credentials."
    try:
        from render import Render
    except ImportError:
        return "Preview unavailable: Render SDK is missing. Install render>=1.4 and redeploy."
    root = root.resolve()
    project = (root / path).resolve()
    if project != root and root not in project.parents:
        return "Preview refused: project path escapes this chat workspace."
    if not project.is_dir():
        return "Preview failed: the selected project directory does not exist."
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", session or ""):
        return "Preview failed: invalid session id."
    try:
        selected = detect_preview(project, port)
    except ValueError as exc:
        if not command.strip():
            return str(exc)
        selected = {"kind": "custom", "port": max(1024, min(int(port), 65535)),
                    "install": "", "command": command.strip()}
    selected_port = max(1024, min(int(port or selected.get("port", 5173)), 65535))
    start_command = command.strip() or str(selected["command"])
    deps_command = install_command.strip() or str(selected.get("install") or "")
    if len(start_command) > 1500 or len(deps_command) > 1500:
        return "Preview failed: start/install command is too long."
    client = Render(token=os.getenv("RENDER_API_KEY"), owner_id=os.getenv("RENDER_WORKSPACE_ID"))
    api = client.experimental.sandboxes
    sandbox = None
    try:
        # Previews need outbound network access for package installation and the
        # temporary HTTPS tunnel. They remain isolated from the Forge service.
        sandbox = api.create(timeout_seconds=MAX_PREVIEW_SECONDS, network_policy="allow-all")
        deadline = time.monotonic() + min(90, max(20, int(os.getenv("SANDBOX_START_TIMEOUT", "60"))))
        while getattr(sandbox, "status", "") != "running":
            if getattr(sandbox, "status", "") in {"failed", "errored", "terminated", "suspended"}:
                raise RuntimeError("sandbox entered status " + str(sandbox.status))
            if time.monotonic() >= deadline:
                raise TimeoutError("sandbox startup timed out")
            time.sleep(2)
            sandbox = api.from_id(sandbox.id)
        with tempfile.TemporaryDirectory(prefix="forge-preview-") as stage:
            stage_root = Path(stage) / "workspace"
            stage_root.mkdir()
            render_sandbox._stage(project, stage_root)
            api.copy_to(sandbox.id, str(stage_root), "/workspace")
        if deps_command:
            output, code = _events(api, sandbox.id, "cd /workspace && " + deps_command, 360)
            if code not in (0, None):
                raise RuntimeError("dependency install failed (exit " + str(code) + "): " + output[-1800:])
        # Install the official Cloudflare tunnel binary only inside the disposable
        # sandbox; never install or execute project code on the Forge web service.
        tunnel_setup = (
            "if command -v cloudflared >/dev/null 2>&1; then CF=$(command -v cloudflared); "
            "else CF=/tmp/cloudflared; "
            "curl -fsSL --max-time 90 https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o "$CF" "
            "&& chmod 700 "$CF"; fi"
        )
        output, code = _events(api, sandbox.id, tunnel_setup, 120)
        if code not in (0, None):
            raise RuntimeError("could not prepare HTTPS preview tunnel: " + output[-1200:])
        cwd = "/workspace" if path == "." else "/workspace/" + project.relative_to(root).as_posix()
        shell = "cd " + shlex.quote(cwd) + " && exec " + start_command
        app_cmd = "nohup bash -lc " + shlex.quote(shell) + " >/tmp/forge-app.log 2>&1 </dev/null & echo $! >/tmp/forge-app.pid"
        output, code = _events(api, sandbox.id, app_cmd, 30)
        if code not in (0, None):
            raise RuntimeError("could not start the project server: " + output[-1200:])
        tunnel_cmd = (
            "CF=$(command -v cloudflared || echo /tmp/cloudflared); "
            "nohup "$CF" tunnel --url http://127.0.0.1:" + str(selected_port) +
            " --no-autoupdate >/tmp/forge-tunnel.log 2>&1 </dev/null & echo $! >/tmp/forge-tunnel.pid"
        )
        output, code = _events(api, sandbox.id, tunnel_cmd, 30)
        if code not in (0, None):
            raise RuntimeError("could not start HTTPS preview tunnel: " + output[-1200:])
        url = ""
        for _ in range(24):
            time.sleep(2)
            output, _ = _events(api, sandbox.id,
                "grep -Eo 'https://[a-z0-9-]+\\.trycloudflare\\.com' /tmp/forge-tunnel.log | head -n 1", 10)
            match = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", output)
            if match:
                url = match.group(0)
                break
        if not url:
            logs, _ = _events(api, sandbox.id, "tail -n 35 /tmp/forge-tunnel.log; echo '--- app log ---'; tail -n 35 /tmp/forge-app.log", 15)
            raise RuntimeError("the app/tunnel did not become ready. Recent logs: " + logs[-2200:])
        records = _state()
        old = records.get(session)
        if old and old.get("sandbox_id") and old.get("sandbox_id") != sandbox.id:
            try:
                api.terminate(old["sandbox_id"])
            except Exception:
                pass
        records[session] = {"sandbox_id": sandbox.id, "url": url, "port": selected_port,
                            "kind": selected.get("kind", "custom"), "created_at": time.time(),
                            "expires_in_seconds": MAX_PREVIEW_SECONDS}
        _save(records)
        return (f"Live preview is ready: [Open preview]({url})\n\n"
                f"Runtime: {selected.get('kind', 'custom')} · port {selected_port} · "
                f"temporary sandbox (up to {MAX_PREVIEW_SECONDS // 60} minutes).\n"
                "The link is public to anyone who has it; do not put secrets or private data in the preview. "
                "The sandbox is isolated from Forge and will expire automatically.")
    except Exception as exc:
        if sandbox is not None:
            try:
                api.terminate(sandbox.id)
            except Exception:
                pass
        return "Preview failed: " + type(exc).__name__ + ": " + str(exc)[:1800]


def stop_preview(session: str) -> str:
    """Stop and release the current preview sandbox for a chat session."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", session or ""):
        return "Preview stop failed: invalid session id."
    records = _state()
    record = records.get(session)
    if not record:
        return "No active preview is recorded for this chat session."
    try:
        from render import Render
        client = Render(token=os.getenv("RENDER_API_KEY"), owner_id=os.getenv("RENDER_WORKSPACE_ID"))
        client.experimental.sandboxes.terminate(record["sandbox_id"])
    except Exception as exc:
        return "Could not stop preview sandbox: " + type(exc).__name__ + ": " + str(exc)[:500]
    records.pop(session, None)
    _save(records)
    return "Preview stopped and sandbox released."
