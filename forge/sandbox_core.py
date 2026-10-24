"""Single, fail-closed execution router for Forge's isolated workspace runners.

This module owns provider selection and diagnostics. Provider adapters own remote
provisioning and workspace sync. There is deliberately no host-shell fallback.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

PROVIDER_FLAGS = {
    "vercel": "SANDBOX_VERCEL",
    "render": "SANDBOX_RENDER",
    "docker": "SANDBOX_DOCKER",
}


def selected_provider() -> str | None:
    """Return the single configured provider; reject ambiguous configurations."""
    enabled = [name for name, flag in PROVIDER_FLAGS.items()
               if os.getenv(flag, "0").strip() == "1"]
    if len(enabled) > 1:
        raise ValueError(
            "Multiple sandbox providers are enabled (" + ", ".join(enabled)
            + "); enable exactly one of SANDBOX_VERCEL, SANDBOX_RENDER, or SANDBOX_DOCKER."
        )
    return enabled[0] if enabled else None


def status() -> dict[str, Any]:
    """Return safe, actionable runtime diagnostics without exposing credentials."""
    allowed = os.getenv("ALLOW_SHELL", "0").strip() == "1"
    try:
        provider = selected_provider()
    except ValueError as exc:
        return {"ready": False, "provider": None, "reason": str(exc), "shell_allowed": allowed}

    if not allowed:
        reason = "Set ALLOW_SHELL=1 to allow commands inside the isolated sandbox."
        return {"ready": False, "provider": provider, "reason": reason, "shell_allowed": False}
    if provider is None:
        return {
            "ready": False, "provider": None,
            "reason": "No isolated runner is selected. Configure exactly one sandbox provider; host execution is never used.",
            "shell_allowed": True,
        }

    if provider == "docker":
        ready = shutil.which("docker") is not None
        reason = "Docker executable found." if ready else "Docker sandbox requested but Docker is unavailable; no host fallback is allowed."
    elif provider == "vercel":
        try:
            from vercel_sandbox import status as provider_status
            detail = provider_status()
            ready = bool(detail.get("ready"))
            reason = str(detail.get("reason") or ("Vercel Sandbox is ready." if ready else "Vercel Sandbox is not ready."))
        except Exception as exc:
            ready, reason = False, f"Vercel Sandbox adapter could not load: {type(exc).__name__}: {exc}"
    else:
        try:
            from render_sandbox import status as provider_status
            detail = provider_status()
            ready = bool(detail.get("ready"))
            reason = str(detail.get("reason") or ("Render Sandbox is ready." if ready else "Render Sandbox is not ready."))
        except Exception as exc:
            ready, reason = False, f"Render Sandbox adapter could not load: {type(exc).__name__}: {exc}"
    return {"ready": ready, "provider": provider, "reason": reason, "shell_allowed": True}


def execute(command: str, workspace_root: str | Path, cwd: str | Path, timeout: int = 120) -> str:
    """Execute in the selected isolated provider, failing closed on every error."""
    root = Path(workspace_root).resolve()
    workdir = Path(cwd).resolve()
    if root != workdir and root not in workdir.parents:
        raise ValueError("command working directory escapes the active workspace")
    if os.getenv("ALLOW_SHELL", "0").strip() != "1":
        return "command execution is disabled by policy (ALLOW_SHELL=0)"
    try:
        provider = selected_provider()
    except ValueError as exc:
        return f"sandbox configuration error: {exc}"
    if provider is None:
        return (
            "no isolated command runner is configured. Enable one sandbox provider "
            "(Vercel, Render, or Docker); host execution is disabled."
        )

    try:
        if provider == "vercel":
            from vercel_sandbox import run
            return str(run(command, workdir, timeout))
        if provider == "render":
            from render_sandbox import run
            return str(run(command, workdir, timeout))

        relative = os.path.relpath(workdir, root)
        image = os.getenv("SANDBOX_IMAGE", "nikolaik/python-nodejs:python3.12-nodejs20")
        network = "bridge" if os.getenv("SANDBOX_NETWORK", "0").strip() == "1" else "none"
        uid = os.getuid() if hasattr(os, "getuid") else 1000
        gid = os.getgid() if hasattr(os, "getgid") else 1000
        argv = [
            "docker", "run", "--rm", "--network", network,
            "--memory=1g", "--cpus=1", "--pids-limit=128",
            "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--user", f"{uid}:{gid}", "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m",
            "--mount", f"type=bind,src={root},dst=/workspace",
            "--workdir", "/workspace" if relative == "." else "/workspace/" + relative,
            image, "sh", "-lc", command,
        ]
        result = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=max(1, int(timeout)))
        output = (result.stdout + result.stderr).strip() or "(no output)"
        return f"{output}\n[exit {result.returncode}]"
    except FileNotFoundError:
        return "Docker sandbox requested but Docker is unavailable; refusing to execute on the host."
    except subprocess.TimeoutExpired:
        return f"sandbox command timed out after {max(1, int(timeout))} seconds; the provider terminated the command."
    except Exception as exc:
        return f"sandbox provider failure ({provider}): {type(exc).__name__}: {exc}. No host fallback was attempted."
