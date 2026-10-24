"""Vercel Sandbox-backed command execution for Forge.

Commands execute in a short-lived, isolated Vercel Sandbox VM. Workspace files are
staged with strict limits; secrets and credential files are never copied into it.
Sandbox network access is deny-all by default and there is no host execution fallback.
"""
from __future__ import annotations

import asyncio
import os
import re
import hashlib
import time
from pathlib import Path

SKIP_DIRS = {".git", ".forge", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
             ".ssh", ".aws", ".azure", ".kube", ".gnupg", ".config"}
SKIP_NAMES = {".env", ".netrc", ".npmrc", ".pypirc", ".dockerconfigjson", "credentials",
              "credentials.json", "secrets.json", "id_rsa", "id_ed25519", "known_hosts"}
SKIP_FILES = {".env", ".env.local", ".env.production", ".netrc", "id_rsa", "id_ed25519"}
SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".keystore", ".jks", ".kdb", ".crt"}
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".pdf", ".zip", ".gz",
                   ".tar", ".mp4", ".mp3", ".wav", ".woff", ".woff2", ".ttf", ".otf", ".so", ".dll"}


def _secret_path(path: Path) -> bool:
    return (path.name in SKIP_FILES or path.name in SKIP_NAMES
            or path.name.startswith(".env.") or path.suffix.lower() in SECRET_SUFFIXES
            or any(part.lower() in {"secrets", "credentials"} for part in path.parts))


def status() -> dict:
    enabled = os.getenv("SANDBOX_VERCEL", "0") == "1"
    sdk = False
    try:
        from vercel import sandbox as _sandbox  # noqa: F401
        from vercel.sandbox import NetworkPolicy as _NetworkPolicy  # noqa: F401
        sdk = True
    except Exception:
        pass
    return {
        "enabled": enabled,
        "configured": enabled and sdk,
        "sdk_installed": sdk,
        "ready": enabled and sdk,
        "reason": ("Configured for Vercel Sandbox; live access is verified on the first command."
                   if enabled and sdk else
                   "Set SANDBOX_VERCEL=1 and install the official vercel Python SDK."
                   if not enabled else "The official Vercel Python SDK is unavailable."),
        "network": "deny-all",
    }


def _stage_files(source: Path):
    files = []
    total = 0
    for path in source.rglob("*"):
        rel = path.relative_to(source)
        if path.is_symlink() or not path.is_file() or any(part in SKIP_DIRS for part in rel.parts):
            continue
        if _secret_path(rel) or path.suffix.lower() in BINARY_SUFFIXES:
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw:
            continue
        total += len(raw)
        files.append((rel, raw.decode("utf-8", errors="replace")))
        if len(files) > int(os.getenv("SANDBOX_MAX_SYNC_FILES", "1500")) or total > int(os.getenv("SANDBOX_MAX_SYNC_BYTES", str(30 * 1024 * 1024))):
            raise RuntimeError("workspace exceeds configured sandbox sync limits")
    return files, total


async def _run(command: str, cwd: Path, timeout: int) -> str:
    from vercel import sandbox
    from vercel.sandbox import NetworkPolicy

    root = cwd.resolve()
    workspace_root = Path(os.getenv("WORKSPACE", "workspace")).resolve()
    active_root = root
    while active_root.parent != workspace_root and active_root != workspace_root and workspace_root in active_root.parents:
        active_root = active_root.parent
    if active_root != workspace_root and active_root.parent != workspace_root:
        raise ValueError("sandbox workspace is outside WORKSPACE")
    relative_cwd = root.relative_to(active_root)
    staged, total = _stage_files(active_root)
    limit = max(1, min(int(timeout), int(os.getenv("SANDBOX_COMMAND_TIMEOUT", "120"))))
    region = os.getenv("VERCEL_SANDBOX_REGION", "bom1").strip() or "bom1"
    # One persistent, named Linux sandbox per workspace: package installs and
    # user-created files survive subsequent tool calls and snapshots.
    identity = hashlib.sha256(str(active_root).encode("utf-8")).hexdigest()[:24]
    box_name = "forge-workspace-" + identity
    network_enabled = os.getenv("SANDBOX_NETWORK", "0") == "1"
    network_policy = NetworkPolicy.allow_all() if network_enabled else NetworkPolicy.deny_all()
    image = os.getenv("VERCEL_SANDBOX_IMAGE", "").strip() or None
    box = None
    try:
        try:
            box = await sandbox.get_sandbox(name=box_name)
        except Exception:
            box = await sandbox.create_sandbox(
                name=box_name,
                image=image,
                execution_time_limit=limit,
                persistent=True,
                network_policy=network_policy,
                region=region,
                tags={"app": "forge-agent", "purpose": "workspace"},
            )
        mkdir = await box.run_process("mkdir", ["-p", "/workspace"], capture_output=True, check=False)
        if getattr(mkdir, "exit_code", 1) != 0:
            return "Vercel Sandbox failed to prepare /workspace: " + str(getattr(mkdir, "stderr", ""))[:1000]
        for rel, text in staged:
            remote = Path("/workspace") / rel
            await box.run_process("mkdir", ["-p", str(remote.parent)], capture_output=True, check=True)
            await box.fs.write_text(str(remote), text)

        remote_cwd = Path("/workspace") if str(relative_cwd) == "." else Path("/workspace") / relative_cwd
        started = time.monotonic()
        result = await box.run_process(
            "sh", ["-lc", command], cwd=str(remote_cwd),
            capture_output=True, check=False, timeout=limit,
        )
        output = ((getattr(result, "stdout", "") or "") + (getattr(result, "stderr", "") or "")).strip()
        output = output or "(no output)"
        exit_code = getattr(result, "exit_code", None)

        # Sync text outputs only. Validate paths and skip secrets before writing locally.
        listing = await box.run_process("find", ["/workspace", "-type", "f", "-print"], capture_output=True, check=False)
        changed = 0
        for raw_path in (getattr(listing, "stdout", "") or "").splitlines():
            try:
                remote = Path(raw_path).resolve()
                rel = remote.relative_to(Path("/workspace"))
            except (ValueError, OSError):
                continue
            if _secret_path(rel) or any(part in SKIP_DIRS for part in rel.parts) or remote.suffix.lower() in BINARY_SUFFIXES:
                continue
            dest = (active_root / rel).resolve()
            if dest != active_root and active_root not in dest.parents:
                continue
            try:
                text = await box.fs.read_text(str(remote))
                if "\x00" in text:
                    continue
                if dest.is_file() and dest.read_text(errors="replace") == text:
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.is_symlink():
                    continue
                dest.write_text(text)
                changed += 1
            except Exception:
                continue
        elapsed = round(time.monotonic() - started, 2)
        evidence = "exit status unavailable" if exit_code is None else f"exit {exit_code}"
        network_label = "allow-all" if network_enabled else "deny-all"
        return f"{output[-6000:]}\n[{evidence}; synced {changed} changed text file(s); staged {len(staged)} file(s), {total // 1024} KB; {elapsed}s; persistent Linux sandbox {box_name}; region {region}; network {network_label}]"
    finally:
        if box is not None:
            try:
                # Persistent sandboxes snapshot on stop and can be resumed by name.
                await box.stop()
            except Exception:
                pass


def run(command: str, cwd: Path, timeout: int = 120) -> str:
    if os.getenv("SANDBOX_VERCEL", "0") != "1":
        return "Vercel Sandbox is not selected (SANDBOX_VERCEL=1 required)."
    try:
        return asyncio.run(_run(command, cwd, timeout))
    except Exception as exc:
        # Fail closed: never fall back to the Vercel Function host.
        return f"Vercel Sandbox execution failed closed: {type(exc).__name__}: {str(exc)[:1200]}"
