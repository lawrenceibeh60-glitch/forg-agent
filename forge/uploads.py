"""Safe browser uploads for Forge workspaces and GitHub repositories."""
import base64
import json
import os
import re
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import httpx
from fastapi import APIRouter, File, Form, HTTPException, UploadFile

import connections
import tools

router = APIRouter()
MAX_FILES = 150
MAX_TOTAL_BYTES = 50 * 1024 * 1024
MAX_FILE_BYTES = 15 * 1024 * 1024
BLOCKED_NAMES = {".env", ".env.local", ".env.production", ".netrc", ".npmrc", ".pypirc",
                 "credentials", "credentials.json", "secrets.json", "id_rsa", "id_ed25519",
                 "known_hosts"}
BLOCKED_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".keystore", ".jks"}


def _safe_relative(raw: str) -> str:
    raw = (raw or "").replace("\\", "/").strip("/")
    p = PurePosixPath(raw)
    if not raw or p.is_absolute() or any(part in ("", ".", "..") for part in p.parts):
        raise HTTPException(400, f"Unsafe file path: {raw!r}")
    if any(part in {".git", ".ssh", ".aws", ".azure", ".kube", "node_modules", ".venv", "venv"} for part in p.parts):
        raise HTTPException(400, f"Uploading into protected folder is not allowed: {raw}")
    name = p.name.lower()
    if name in BLOCKED_NAMES or name.startswith(".env.") or p.suffix.lower() in BLOCKED_SUFFIXES:
        raise HTTPException(400, f"Refusing to upload a credential or private-key file: {raw}")
    return p.as_posix()


async def _read_files(files: list[UploadFile], paths_json: str):
    if not files:
        raise HTTPException(400, "Choose at least one file.")
    if len(files) > MAX_FILES:
        raise HTTPException(413, f"Maximum {MAX_FILES} files per upload.")
    try:
        paths = json.loads(paths_json) if paths_json else []
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid upload path metadata.")
    if paths and (not isinstance(paths, list) or len(paths) != len(files)):
        raise HTTPException(400, "Upload path metadata does not match the selected files.")
    result, total = [], 0
    for i, upload in enumerate(files):
        rel = _safe_relative(str(paths[i] if paths else upload.filename or ""))
        data = await upload.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise HTTPException(413, f"{rel} exceeds the 15 MiB per-file limit.")
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise HTTPException(413, "Total upload exceeds the 50 MiB limit.")
        result.append((rel, data))
    return result


def _github_headers(token: str):
    return {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


async def _github_upload(repo: str, branch: str, destination: str, items):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo or ""):
        raise HTTPException(400, "Repository must use owner/name format.")
    token = connections.get_access_token("github") or os.getenv("GITHUB_TOKEN", "")
    if not token:
        raise HTTPException(401, "Connect GitHub in Forge Settings before uploading to a repository.")
    dest = (destination or "").strip("/")
    if dest:
        dest = _safe_relative(dest)
    api = "https://api.github.com"
    async with httpx.AsyncClient(timeout=45, headers=_github_headers(token)) as client:
        async def call(method, url, **kwargs):
            response = await client.request(method, api + url, **kwargs)
            try:
                body = response.json()
            except ValueError:
                body = {}
            if response.status_code >= 400:
                message = body.get("message", f"GitHub API returned HTTP {response.status_code}") if isinstance(body, dict) else "GitHub request failed"
                raise HTTPException(response.status_code, message)
            return body

        repo_info = await call("GET", f"/repos/{repo}")
        branch = branch.strip() or repo_info.get("default_branch") or "main"
        ref = await call("GET", f"/repos/{repo}/git/ref/heads/{quote(branch, safe='')}")
        parent_sha = ref.get("object", {}).get("sha")
        if not parent_sha:
            raise HTTPException(502, "GitHub did not return the target branch head.")
        parent = await call("GET", f"/repos/{repo}/git/commits/{parent_sha}")
        base_tree = parent.get("tree", {}).get("sha")
        if not base_tree:
            raise HTTPException(502, "GitHub did not return the target tree.")
        entries = []
        for rel, data in items:
            target = f"{dest}/{rel}" if dest else rel
            blob = await call("POST", f"/repos/{repo}/git/blobs",
                              json={"content": base64.b64encode(data).decode("ascii"), "encoding": "base64"})
            entries.append({"path": target, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        tree = await call("POST", f"/repos/{repo}/git/trees",
                          json={"base_tree": base_tree, "tree": entries})
        commit = await call("POST", f"/repos/{repo}/git/commits", json={
            "message": f"Upload {len(items)} file(s) with Forge",
            "tree": tree["sha"], "parents": [parent_sha]})
        updated = await call("PATCH", f"/repos/{repo}/git/refs/heads/{quote(branch, safe="")}",
                             json={"sha": commit["sha"], "force": False})
    return {"ok": True, "mode": "github", "repo": repo, "branch": branch,
            "files": len(items), "commit": commit["sha"],
            "url": f"https://github.com/{repo}/commit/{commit['sha']}"}


@router.post("/api/uploads")
async def upload_files(
    files: list[UploadFile] = File(...),
    paths: str = Form(""),
    sid: str = Form(""),
    mode: str = Form("workspace"),
    repo: str = Form(""),
    branch: str = Form(""),
    destination: str = Form(""),
):
    items = await _read_files(files, paths)
    if mode == "github":
        return await _github_upload(repo, branch, destination, items)
    if mode != "workspace":
        raise HTTPException(400, "Unknown upload destination.")
    root = tools.use(sid or "default").resolve()
    written = []
    for rel, data in items:
        target = (root / rel).resolve()
        if root not in target.parents:
            raise HTTPException(400, f"Unsafe destination: {rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        written.append(rel)
    return {"ok": True, "mode": "workspace", "files": written, "count": len(written)}
