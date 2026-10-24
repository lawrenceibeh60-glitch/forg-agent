"""Render-managed remote command execution for Forge."""
from __future__ import annotations
import os, shutil, tempfile, time, hashlib
from pathlib import Path

SKIP_DIRS = {".git", ".forge", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
             ".ssh", ".aws", ".azure", ".kube", ".gnupg", ".config"}
SKIP_NAMES = {".env", ".netrc", ".npmrc", ".pypirc", ".dockerconfigjson", "credentials",
              "credentials.json", "secrets.json", "id_rsa", "id_ed25519", "known_hosts"}
SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".keystore", ".jks", ".kdb", ".crt"}
def _secret_path(path: Path) -> bool:
    return (path.name in SKIP_FILES or path.name in SKIP_NAMES
            or path.name.startswith(".env.") or path.suffix.lower() in SECRET_SUFFIXES
            or any(part.lower() in {"secrets", "credentials"} for part in path.parts))
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
            "network": "enabled" if os.getenv("SANDBOX_NETWORK", "0") == "1" else "disabled",
            "snapshot_configured": bool(os.getenv("SANDBOX_SNAPSHOT_NAME", "").strip())}


def _stage(source: Path, target: Path):
    count = total = 0
    source = source.resolve()
    for p in source.rglob("*"):
        rel = p.relative_to(source)
        if p.is_symlink() or not p.is_file() or any(x in SKIP_DIRS for x in rel.parts):
            continue
        if _secret_path(rel):
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
    """Validate the entire sandbox result before applying any output files."""
    count = total = 0
    source, target = source.resolve(), target.resolve()
    pending = []
    for p in source.rglob("*"):
        if p.is_symlink() or not p.is_file():
            continue
        rel = p.relative_to(source)
        if any(x in SKIP_DIRS for x in rel.parts) or _secret_path(rel):
            continue
        count += 1
        total += p.stat().st_size
        if count > MAX_FILES or total > MAX_BYTES:
            raise RuntimeError("sandbox output exceeded sync limits; refusing to sync further files")
        raw_dest = target / rel
        if raw_dest.is_symlink():
            raise RuntimeError("sandbox output attempted to overwrite a symlink")
        resolved_parent = raw_dest.parent.resolve()
        if resolved_parent != target and target not in resolved_parent.parents:
            raise RuntimeError("sandbox output attempted a path traversal")
        dest = resolved_parent / raw_dest.name
        if dest.is_symlink():
            raise RuntimeError("sandbox output attempted to overwrite a symlink")
        pending.append((p, dest))
    # Validate every path first so a malicious late file cannot leave a partial sync.
    changed = []
    for source_file, dest in pending:
        if dest.is_file():
            try:
                if dest.stat().st_size == source_file.stat().st_size and hashlib.sha256(dest.read_bytes()).digest() == hashlib.sha256(source_file.read_bytes()).digest():
                    continue
            except OSError:
                pass
        changed.append((source_file, dest))
    for source_file, dest in changed:
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Re-check after parent creation in case the target tree changed.
        if dest.is_symlink():
            raise RuntimeError("sandbox output attempted to overwrite a symlink")
        shutil.copy2(source_file, dest)
    return len(changed)


def creation_failure_diagnosis(exc, api=None) -> str:
    """Explain Render sandbox 'not found' failures without retrying creation."""
    message = f"{type(exc).__name__}: {str(exc)[:700]}"
    lowered = message.lower()
    if "not found" not in lowered and "404" not in lowered:
        return message
    group_note = ""
    if api is not None:
        try:
            groups_result = api.list_groups()
            groups = getattr(groups_result, "groups", None)
            if groups is not None and not groups:
                group_note = " The Render API reports no sandbox group for this workspace."
            elif groups:
                group_note = " A sandbox group is visible, so check whether this API key can create sandboxes in that group."
        except Exception as group_exc:
            group_note = " A separate sandbox-group check also failed (" + type(group_exc).__name__ + ")."
    return (
        "Render returned 'not found' while creating the sandbox. Forge stopped here and did not repeat the create request."
        + group_note
        + " Verify in Render that Sandboxes are enabled for this workspace and that RENDER_WORKSPACE_ID matches that workspace; "
          "the RENDER_API_KEY must have access to it. This is a Render provisioning/access problem, not a project-code test failure."
    )


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
    try:
        configured_timeout = int(os.getenv("SANDBOX_COMMAND_TIMEOUT", "120"))
    except (TypeError, ValueError):
        configured_timeout = 120
    timeout = max(1, min(int(timeout), max(1, min(configured_timeout, 900))))
    client = Render(token=os.getenv("RENDER_API_KEY"), owner_id=os.getenv("RENDER_WORKSPACE_ID"))
    sandbox = None
    with tempfile.TemporaryDirectory(prefix="forge-in-") as tmp_in, tempfile.TemporaryDirectory(prefix="forge-out-") as tmp_out:
        stage_in = Path(tmp_in) / "workspace"
        stage_in.mkdir()
        count, size = _stage(active_root, stage_in)
        stage_out = Path(tmp_out) / "workspace"
        stage_out.mkdir()
        try:
            network_policy = os.getenv("SANDBOX_NETWORK_POLICY", "deny-all").strip().lower()
            if os.getenv("SANDBOX_NETWORK", "0") == "1":
                network_policy = "allow-all"
            if network_policy not in {"deny-all", "allow-all"}:
                raise ValueError("SANDBOX_NETWORK_POLICY must be deny-all or allow-all")
            api = client.experimental.sandboxes
            create_args = {"timeout_seconds": max(60, timeout + 30), "network_policy": network_policy}
            # A named snapshot can preinstall tools such as IPython while the
            # live sandbox still starts with a deny-all network policy.
            snapshot_name = os.getenv("SANDBOX_SNAPSHOT_NAME", "").strip()
            if snapshot_name:
                create_args["snapshot_name"] = snapshot_name
            sandbox = api.create(**create_args)
            deadline = time.monotonic() + min(90, int(os.getenv("SANDBOX_START_TIMEOUT", "60")))
            while getattr(sandbox, "status", "") != "running":
                if getattr(sandbox, "status", "") in {"failed", "errored", "terminated", "suspended"}:
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
            evidence = "exit status unavailable; verification cannot be confirmed" if exit_code is None else f"exit {exit_code}"
            return f"{result}\n[{evidence}; synced {synced} changed file(s); staged {count} file(s), {size // 1024} KB]"
        except Exception as exc:
            detail = creation_failure_diagnosis(exc, api) if sandbox is None else f"{type(exc).__name__}: {str(exc)[:500]}"
            return "Render sandbox creation failed closed: " + detail
        finally:
            if sandbox is not None:
                try: api.terminate(sandbox.id)
                except Exception: pass
