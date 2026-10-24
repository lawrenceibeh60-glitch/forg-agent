"""Live preview sessions on Vercel Sandbox with exposed HTTPS routes.

Each preview runs in its own isolated Linux VM. Vercel's port routing replaces
Render sandboxes and Cloudflare Quick Tunnels; preview links are public to anyone
who has the URL and expire when the sandbox is destroyed or times out.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shlex
import time
from pathlib import Path
from typing import Any

import vercel_sandbox

STATE_PATH = Path(os.getenv("FORGE_PREVIEW_STATE", "/tmp/forge-data/previews.json"))
MAX_PREVIEW_SECONDS = min(3600, max(300, int(os.getenv("VERCEL_PREVIEW_TIMEOUT", "3600"))))
SKIP_DIRS = {".git", ".forge", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
             ".ssh", ".aws", ".azure", ".kube", ".gnupg", ".config", "dist", "build"}
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".pdf", ".zip", ".gz",
                   ".tar", ".mp4", ".mp3", ".wav", ".woff", ".woff2", ".ttf", ".otf", ".so", ".dll"}


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
                "command": f"python3 -m http.server {chosen_port} --bind 0.0.0.0"}
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
                    "install": "python3 -m pip install -r requirements.txt" if requirements.is_file() else "",
                    "command": f"python3 -m uvicorn {module}:app --host 0.0.0.0 --port {chosen_port}"}
        if "flask(" in source or "from flask" in source or "import flask" in source:
            return {"kind": "flask", "port": chosen_port,
                    "install": "python3 -m pip install -r requirements.txt" if requirements.is_file() else "",
                    "command": f"python3 -m flask --app {module} run --host 0.0.0.0 --port {chosen_port}"}
    if requirements.is_file() and (root / "main.py").is_file():
        return {"kind": "python", "port": chosen_port,
                "install": "python3 -m pip install -r requirements.txt",
                "command": "python3 main.py"}
    raise ValueError("I couldn't identify a web entry point. Supply a start command and port, for example 'python3 -m uvicorn main:app --host 0.0.0.0 --port 8000'.")


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


def _sandbox_name(session: str) -> str:
    return "forge-preview-" + hashlib.sha256(session.encode("utf-8")).hexdigest()[:24]


def _safe_stage_path(path: Path) -> bool:
    rel = path
    if vercel_sandbox._secret_path(rel) or any(part in SKIP_DIRS for part in rel.parts):
        return False
    if path.suffix.lower() in vercel_sandbox.SECRET_SUFFIXES:
        return False
    return True


async def _write_workspace(box, project: Path) -> tuple[int, int]:
    count, total = 0, 0
    await box.run_process("mkdir", ["-p", "/workspace"], capture_output=True, check=True)
    # Start clean while preserving the sandbox home and system tools.
    await box.run_process("bash", ["-lc", "find /workspace -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +"],
                          capture_output=True, check=True)
    for path in project.rglob("*"):
        rel = path.relative_to(project)
        if path.is_symlink() or not path.is_file() or not _safe_stage_path(rel):
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        total += len(data)
        if total > int(os.getenv("VERCEL_PREVIEW_MAX_BYTES", str(100 * 1024 * 1024))):
            raise RuntimeError("Project exceeds the 100 MB preview staging limit.")
        remote = "/workspace/" + rel.as_posix()
        await box.run_process("mkdir", ["-p", str(Path(remote).parent)], capture_output=True, check=True)
        await box.fs.write_bytes(remote, data)
        count += 1
        if count > int(os.getenv("VERCEL_PREVIEW_MAX_FILES", "5000")):
            raise RuntimeError("Project contains too many files for preview staging.")
    return count, total


async def _start(session: str, root: Path, project: Path, start_command: str,
                 deps_command: str, port: int) -> tuple[str, str, int, int]:
    from vercel import sandbox
    from vercel.sandbox import NetworkPolicy

    name = _sandbox_name(session)
    region = os.getenv("VERCEL_SANDBOX_REGION", "bom1").strip() or "bom1"
    image = os.getenv("VERCEL_SANDBOX_IMAGE", "").strip() or None
    box = None
    try:
        try:
            box = await sandbox.get_sandbox(name=name)
            # A prior preview for this session may have left old processes behind.
            await box.run_process("bash", ["-lc", "if [ -f /tmp/forge-preview-app.pid ]; then kill $(cat /tmp/forge-preview-app.pid) 2>/dev/null || true; rm -f /tmp/forge-preview-app.pid; fi"],
                                  capture_output=True, check=False, timeout=10)
        except Exception:
            box = await sandbox.create_sandbox(
                name=name, image=image, ports=[port],
                execution_time_limit=MAX_PREVIEW_SECONDS, persistent=True,
                network_policy=NetworkPolicy.allow_all(), region=region,
                tags={"app": "forge-agent", "purpose": "live-preview"},
            )
        staged_count, staged_bytes = await _write_workspace(box, project)
        cwd = "/workspace"
        if deps_command:
            result = await box.run_process("bash", ["-lc", "cd /workspace && " + deps_command],
                                           capture_output=True, check=False, timeout=min(600, MAX_PREVIEW_SECONDS))
            if getattr(result, "exit_code", 1) != 0:
                detail = ((getattr(result, "stdout", "") or "") + (getattr(result, "stderr", "") or ""))[-2500:]
                raise RuntimeError("Dependency install failed: " + detail)
        launch = "cd " + shlex.quote(cwd) + " && exec " + start_command
        result = await box.run_process(
            "bash", ["-lc", "nohup bash -lc " + shlex.quote(launch) +
                     " >/tmp/forge-preview-app.log 2>&1 </dev/null & echo $! >/tmp/forge-preview-app.pid"],
            capture_output=True, check=False, timeout=30,
        )
        if getattr(result, "exit_code", 1) != 0:
            raise RuntimeError("Could not launch preview server: " + str(getattr(result, "stderr", ""))[-1500:])
        ready = False
        for _ in range(30):
            probe = await box.run_process("bash", ["-lc", f"curl -fsS --max-time 2 http://127.0.0.1:{port}/ >/dev/null"],
                                          capture_output=True, check=False, timeout=5)
            if getattr(probe, "exit_code", 1) == 0:
                ready = True
                break
            await asyncio.sleep(1)
        if not ready:
            logs = await box.run_process("bash", ["-lc", "tail -n 45 /tmp/forge-preview-app.log 2>/dev/null || true"],
                                         capture_output=True, check=False, timeout=10)
            raise RuntimeError("App did not become ready on port " + str(port) + ". Logs: " +
                               ((getattr(logs, "stdout", "") or "") + (getattr(logs, "stderr", "") or ""))[-2500:])
        route_url = ""
        for route in getattr(box, "routes", []) or []:
            route_port = getattr(route, "port", None)
            if str(route_port) == str(port):
                route_url = str(getattr(route, "url", "") or "")
                break
        if not route_url:
            try:
                route_url = str(box.domain(port))
            except Exception:
                route_url = ""
        if not route_url.startswith("https://"):
            raise RuntimeError("Vercel did not return an HTTPS route for port " + str(port))
        return name, route_url, staged_count, staged_bytes
    except Exception:
        if box is not None:
            try:
                await box.destroy()
            except Exception:
                pass
        raise


def start_preview(session: str, root: Path, path: str = ".", command: str = "",
                  port: int = 5173, install_command: str = "") -> str:
    """Create an isolated Vercel Sandbox and return its exposed HTTPS preview URL."""
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
    try:
        name, url, staged_count, staged_bytes = asyncio.run(
            _start(session, root, project, start_command, deps_command, selected_port))
        records = _state()
        records[session] = {"sandbox_name": name, "url": url, "port": selected_port,
                            "kind": selected.get("kind", "custom"), "created_at": time.time(),
                            "expires_in_seconds": MAX_PREVIEW_SECONDS}
        _save(records)
        return (f"Live preview is ready: [Open preview]({url})\n\n"
                f"Runtime: {selected.get('kind', 'custom')} · Linux sandbox · port {selected_port} · "
                f"{staged_count} files staged ({staged_bytes // 1024} KB) · expires in "
                f"{MAX_PREVIEW_SECONDS // 60} minutes.\n"
                "Vercel's exposed-port HTTPS routing is used directly; no Render service or tunnel is needed. "
                "The link is public to anyone who has it; do not put secrets or private data in the preview.")
    except Exception as exc:
        return "Preview failed: " + type(exc).__name__ + ": " + str(exc)[:2500]


async def _stop_named(name: str) -> None:
    from vercel import sandbox
    box = await sandbox.get_sandbox(name=name)
    await box.destroy()


def stop_preview(session: str) -> str:
    """Destroy the active Vercel preview sandbox and release its resources."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", session or ""):
        return "Preview stop failed: invalid session id."
    records = _state()
    record = records.get(session) or {}
    name = record.get("sandbox_name") or _sandbox_name(session)
    try:
        asyncio.run(_stop_named(name))
    except Exception as exc:
        return "Could not stop preview sandbox: " + type(exc).__name__ + ": " + str(exc)[:600]
    records.pop(session, None)
    _save(records)
    return "Preview stopped and the Vercel Sandbox was destroyed."
