"""Render-managed remote command execution for Forge."""
from __future__ import annotations
import os, shutil, tempfile, time
from pathlib import Path

SKIP_DIRS = {".git", ".forge", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache"}
SKIP_FILES = {".env", ".env.local", ".env.production", ".netrc", "id_rsa", "id_ed25519"}
MAX_FILES = int(os.getenv("SANDBOX_MAX_SYNC_FILES", "1500"))
MAX_BYTES = int(os.getenv("SANDBOX_MAX_SYNC_BYTES", str(30 * 1024 * 1024)))


def configured():
    return (os.getenv("SANDBOX_RENDER", "0") == "1"
            and os.getenv("ALLOW_SHELL", "0") == "1"
            and bool(os.getenv("RENDER_API_KEY", "").strip())
            and bool(os.getenv("RENDER_WORKSPACE_ID", "").strip()))


def status():
    enabled = os.getenv("SANDBOX_RENDER", "0") == "1"
    try:
        from importlib.util import find_spec
        sdk = find_spec("render") is not None
    except (ImportError, ValueError):
        sdk = False
    ready = configured() and sdk
    if not enabled: reason = "Set SANDBOX_RENDER=1 to select Render-managed execution."
    elif os.getenv("ALLOW_SHELL", "0") != "1": reason = "Set ALLOW_SHELL=1 to enable isolated remote execution."
    elif not os.getenv("RENDER_API_KEY", "").strip(): reason = "RENDER_API_KEY is missing."
    elif not os.getenv("RENDER_WORKSPACE_ID", "").strip(): reason = "RENDER_WORKSPACE_ID is missing."
    elif not sdk: reason = "The Render Python SDK is not installed."
    else: reason = "Configured; workspace access is verified only when a sandbox operation succeeds."
    return {"enabled": enabled, "configured": configured(), "sdk_installed": sdk,
            "ready": ready, "reason": reason,
            "network": "enabled" if os.getenv("SANDBOX_NETWORK", "0") == "1" else "disabled"}


def _stage(source: Path, target: Path):
    count = total = 0
    source = source.resolve()
    for p in source.rglob("*"):
        rel = p.relative_to(source)
        if p.is_symlink() or not p.is_file() or any(x in SKIP_DIRS for x in rel.parts):
            continue
        if p.name in SKIP_FILES or p.name.startswith(".env.") or p.suffix in {".pem", ".key", ".p12", ".pfx"}:
            continue
        count += 1
        total += p.stat().st_size
        if count > MAX_FILES or total > MAX_BYTES:
            raise RuntimeError("workspace exceeds configured sandbox sync limits")
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dest)
    return count, total


def _sync_back(source: Path, target: Path):
    count = total = 0
    source, target = source.resolve(), target.resolve()
    for p in source.rglob("*"):
        if p.is_symlink() or not p.is_file():
            continue
        rel = p.relative_to(source)
        if any(x in SKIP_DIRS for x in rel.parts) or p.name in SKIP_FILES or p.name.startswith(".env.") or p.suffix in {".pem", ".key", ".p12", ".pfx"}:
            continue
        count += 1
        total += p.stat().st_size
        if count > MAX_FILES or total > MAX_BYTES:
            raise RuntimeError("sandbox output exceeded sync limits; refusing to sync further files")
        dest = (target / rel).resolve()
        if dest != target and target not in dest.parents:
            raise RuntimeError("sandbox output attempted a path traversal")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dest)
    return count


def run(command: str, cwd: Path, timeout: int = 120) -> str:
    if not configured():
        return "Render sandbox is not configured. Required: SANDBOX_RENDER=1, ALLOW_SHELL=1, RENDER_API_KEY, and RENDER_WORKSPACE_ID."
    try:
        from render import Render
    except ImportError:
        return "Render sandbox SDK missing; install render>=1.2 and redeploy."
    root = cwd.resolve()
    workspace_root = Path(os.getenv("WORKSPACE", "workspace")).resolve()
    active_root = root
    while active_root.parent != workspace_root and active_root != workspace_root and workspace_root in active_root.parents:
        active_root = active_root.parent
    if active_root != workspace_root and active_root.parent != workspace_root:
        raise ValueError("sandbox workspace is outside WORKSPACE")
    relative_cwd = root.relative_to(active_root).as_posix()
    timeout = max(1, min(int(timeout), int(os.getenv("SANDBOX_COMMAND_TIMEOUT", "120"))))
    client = Render(token=os.getenv("RENDER_API_KEY"), owner_id=os.getenv("RENDER_WORKSPACE_ID"))
    sandbox = None
    with tempfile.TemporaryDirectory(prefix="forge-in-") as tmp_in, tempfile.TemporaryDirectory(prefix="forge-out-") as tmp_out:
        stage_in = Path(tmp_in) / "workspace"
        stage_in.mkdir()
        count, size = _stage(active_root, stage_in)
        stage_out = Path(tmp_out) / "workspace"
        stage_out.mkdir()
        try:
            network_policy = os.getenv("SANDBOX_NETWORK_POLICY", "deny-all")
            if os.getenv("SANDBOX_NETWORK", "0") == "1":
                network_policy = "allow-all"
            api = client.experimental.sandboxes
            sandbox = api.create(timeout_seconds=max(60, timeout + 30), network_policy=network_policy)
            deadline = time.monotonic() + min(90, int(os.getenv("SANDBOX_START_TIMEOUT", "60")))
            while getattr(sandbox, "status", "") != "running":
                if getattr(sandbox, "status", "") in {"failed", "terminated", "suspended"}:
                    raise RuntimeError("Render sandbox entered status " + str(sandbox.status))
                if time.monotonic() >= deadline:
                    raise TimeoutError("Render sandbox startup timed out")
                time.sleep(2)
                sandbox = api.from_id(sandbox.id)
            api.copy_to(sandbox.id, str(stage_in), "/workspace")
            import shlex
            remote_cwd = "/workspace" if relative_cwd == "." else "/workspace/" + relative_cwd
            events = api.exec(sandbox.id, "cd " + shlex.quote(remote_cwd) + " && " + command)
            output, exit_code = [], None
            for event in events:
                if hasattr(event, "exit_code"):
                    exit_code = event.exit_code
                elif getattr(event, "data", ""):
                    output.append(event.data)
            api.copy_from(sandbox.id, "/workspace", str(stage_out))
            synced = _sync_back(stage_out, active_root)
            result = "".join(output).strip()[-6000:] or "(no output)"
            return f"{result}\n[exit {exit_code if exit_code is not None else 'unknown'}; synced {synced} file(s); staged {count} file(s), {size // 1024} KB]"
        except Exception as exc:
            return f"Render sandbox execution failed closed: {type(exc).__name__}: {str(exc)[:500]}"
        finally:
            if sandbox is not None:
                try: api.terminate(sandbox.id)
                except Exception: pass
