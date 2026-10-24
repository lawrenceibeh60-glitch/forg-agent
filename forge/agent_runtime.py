"""Adaptive agent runtime: compact tool routing and bounded workspace reconnaissance.

This module does not generate reasoning or claim to be a neural network. It helps the
configured model choose relevant tools and starts coding tasks with grounded project
context instead of an empty, generic prompt.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", "dist", "build", ".next", ".cache", "coverage",
}
SECRET_NAMES = {
    ".env", ".env.local", ".env.production", ".netrc", "id_rsa", "id_ed25519",
    "credentials.json", "secrets.json",
}
SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
MANIFESTS = (
    "README.md", "pyproject.toml", "package.json", "requirements.txt",
    "go.mod", "Cargo.toml", "pom.xml", "Gemfile", "Makefile", "render.yaml",
    "Dockerfile", "compose.yaml", "docker-compose.yml",
)

# Curated semantic bridges help a model discover tools whose names differ from the
# user's language. These are routing hints, not hard restrictions on authorization.
ALIASES = {
    "project": "tree list_files glob_files detect_stack outline read_many search_code project_map dependency_graph find_symbol find_references",
    "architecture": "tree detect_stack outline project_map dependency_graph find_symbol find_references read_file read_many search_code",
    "understand": "tree detect_stack outline project_map dependency_graph find_symbol find_references read_file read_many search_code",
    "trace": "search_code find_references git_blame git_log read_file outline",
    "reference": "search_code find_references outline read_file",
    "symbol": "find_symbol find_references outline search_code",
    "bug": "search_code read_file read_many run_tests lint run_command git_changes git_show",
    "error": "search_code read_file read_many run_tests lint run_command web_search web_fetch",
    "fix": "list_files tree read_file read_many search_code find_references edit_file apply_patch write_file run_tests lint git_changes",
    "add": "list_files tree read_file read_many search_code find_references edit_file apply_patch write_file make_dir run_tests lint",
    "update": "list_files tree read_file read_many search_code find_references edit_file apply_patch write_file run_tests lint git_changes",
    "improve": "list_files tree read_file read_many search_code find_references edit_file apply_patch write_file run_tests lint git_changes",
    "refactor": "project_map find_symbol find_references dependency_graph read_file read_many edit_file apply_patch run_tests lint",
    "feature": "project_map find_symbol find_references dependency_graph read_file read_many write_file edit_file apply_patch run_tests lint",
    "frontend": "tree glob_files read_file read_many search_code find_references write_file edit_file apply_patch run_command run_tests",
    "ui": "tree glob_files read_file read_many search_code write_file edit_file apply_patch run_tests",
    "build": "list_files tree detect_stack read_file read_many search_code write_file edit_file apply_patch run_command run_tests lint sandbox_info",
    "implement": "list_files tree detect_stack read_file read_many search_code find_references write_file edit_file apply_patch run_command run_tests lint git_changes",
    "create": "list_files tree detect_stack read_file write_file edit_file make_dir run_command run_tests",
    "test": "run_tests lint run_command git_changes read_file search_code",
    "verify": "run_tests lint run_command check_url http_request git_changes",
    "deploy": "render_list_services render_deploy_status render_logs render_deploy check_url",
    "render": "render_list_services render_deploy_status render_logs render_deploy render_set_env render_restart",
    "github": "github_list_repos repo_open github_get_file github_search_code github_prs github_workflow_runs github_repo_info git_changes git_log",
    "repository": "repo_open list_files tree git_changes git_log git_branches github_get_file github_search_code",
    "repo": "repo_open list_files tree git_changes git_log git_branches github_get_file github_search_code",
    "commit": "git_changes git_log git_branches git repo_commit_push",
    "push": "git_changes git repo_commit_push github_create_pr",
    "research": "web_search web_fetch extract_links wikipedia stack_search pypi_info npm_info github_search_code",
    "search": "search_code glob_files web_search github_search_code stack_search",
    "docs": "web_search web_fetch extract_links wikipedia stack_search pypi_info npm_info",
    "web": "web_search web_fetch extract_links check_url",
    "api": "web_search web_fetch http_request check_url github_get_file",
    "database": "sql_query json_get csv_preview dependency_graph",
    "security": "risk_report env_keys search_code web_search",
    "memory": "remember project_memory_search project_memory_save project_memory_list",
    "tool": "tool_catalog",
    "capability": "tool_catalog",
    "available": "tool_catalog",
    "image": "image_generate image_edit web_search",
    "workflow": "workflow_save workflow_list workflow_run",
    "mcp": "mcp",
    "plugin": "mcp",
    "browser": "web_search web_fetch check_url extract_links",
}

# Tools always available to coding/research tasks so routing cannot strand the agent.
CORE_TOOLS = {
    "list_files", "tree", "glob_files", "read_file", "read_many", "search_code",
    "outline", "detect_stack", "update_plan", "run_tests", "lint", "run_command",
    "git_changes", "git_log", "web_search", "web_fetch", "check_url", "tool_catalog",
}
CODING_TERMS = re.compile(
    r"\b(code|coding|repo|repository|project|file|folder|bug|fix|implement|build|"
    r"create|edit|refactor|test|tests|deploy|website|app|application|api|frontend|"
    r"backend|script|function|class|compile|error|crash|feature|architecture|"
    r"debug|commit|github|render|workspace|sandbox|install|dependency)\b", re.I
)
RESEARCH_TERMS = re.compile(
    r"\b(research|look up|search the web|latest|current|compare|sources|documentation|"
    r"docs|citation|investigate|benchmark)\b", re.I
)


def classify_task(text: str) -> str:
    """Choose an operating mode from the request and recent user intent."""
    value = (text or "")[-12000:]
    if CODING_TERMS.search(value):
        return "engineering"
    if re.search(r"\b(add|implement|improve|upgrade|change|update|integrate|refactor|fix|build|create|remove|optimize|support)\b", value, re.I) and re.search(r"\b(feature|tool|workflow|interface|screen|button|component|endpoint|route|model|provider|agent|memory|sandbox|security|performance|test|deployment|database|schema|project|theme|layout|dark mode|responsive|animation|style|ui)\b", value, re.I):
        return "engineering"
    if RESEARCH_TERMS.search(value):
        return "research"
    return "general"


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z][a-z0-9_]{1,}", (text or "").lower()))


def select_schemas(all_schemas: list[dict], task_text: str, max_tools: int = 42) -> list[dict]:
    """Reduce tool overload while keeping essential tools and semantic matches.

    The full registry remains available to the agent through an explicit discovery
    request; this selector is a ranking layer, never an authorization layer.
    """
    if not all_schemas:
        return []
    text = (task_text or "")[-12000:]
    mode = classify_task(text)
    all_by_name = {}
    for schema in all_schemas:
        name = schema.get("function", {}).get("name", "")
        if name and name not in all_by_name:
            all_by_name[name] = schema

    selected: dict[str, dict] = {}
    if mode == "engineering":
        for name in CORE_TOOLS:
            if name in all_by_name:
                selected[name] = all_by_name[name]
    elif mode == "research":
        for name in {"web_search", "web_fetch", "extract_links", "check_url", "wikipedia", "stack_search", "pypi_info", "npm_info"}:
            if name in all_by_name:
                selected[name] = all_by_name[name]

    lower = text.lower()
    # Explicitly surface aliases, including synonyms such as "trace a function"
    # or "what calls this symbol?", that basic token overlap misses.
    wanted: set[str] = set()
    for key, names in ALIASES.items():
        if re.search(r"\b" + re.escape(key) + r"\b", lower):
            wanted.update(names.split())
    tokens = _tokens(text)
    scored = []
    for name, schema in all_by_name.items():
        if name in selected:
            continue
        fn = schema.get("function", {})
        description = str(fn.get("description", ""))
        name_tokens = _tokens(name.replace("__", " "))
        desc_tokens = _tokens(description)
        score = 0
        if name in wanted:
            score += 12
        score += 3 * len(tokens & name_tokens)
        score += min(5, len(tokens & desc_tokens))
        if name.startswith("mcp__") and re.search(r"\b(mcp|plugin|server|integration)\b", lower):
            score += 8
        if mode == "engineering" and name in {"write_file", "edit_file", "apply_patch", "find_todos", "code_stats", "dependency_graph", "project_map", "find_symbol", "find_references", "git_diff"}:
            score += 5
        if mode == "research" and name in {"web_search", "web_fetch", "extract_links", "stack_search", "wikipedia", "github_search_code"}:
            score += 5
        if score:
            scored.append((score, name, schema))
    scored.sort(key=lambda item: (-item[0], item[1]))
    for _, name, schema in scored:
        if len(selected) >= max(16, min(int(max_tools), 64)):
            break
        selected[name] = schema

    # If the prompt specifically asks for a capability that is only exposed through
    # MCP, include the connected server tools; otherwise avoid flooding the context.
    if re.search(r"\b(mcp|plugin|connected server|integration tool)\b", lower):
        for name, schema in all_by_name.items():
            if name.startswith("mcp__") and len(selected) < max_tools:
                selected[name] = schema

    # Preserve original registry order for deterministic provider requests.
    return [schema for schema in all_schemas if schema.get("function", {}).get("name") in selected]


def verification_succeeded(tool_name: str, output: str) -> bool:
    """Return true only when a meaningful check produced positive evidence."""
    name, value = str(tool_name or ""), str(output or "").strip()
    if not value:
        return False
    if name in {"run_tests", "lint", "run_command", "python_eval", "node_eval"}:
        # Render's runner uses "[exit 0; ...]" while Docker/local runners use
        # "[exit 0]". Do not infer success from reassuring prose alone.
        match = re.search(r"\[exit\s+(\d+)(?:[;\]])", value, re.I)
        return bool(match and int(match.group(1)) == 0)
    if name == "check_url":
        match = re.match(r"\s*(\d{3})\b", value)
        return bool(match and 200 <= int(match.group(1)) < 300)
    if name == "http_request":
        match = re.match(r"\s*(\d{3})\b", value)
        return bool(match and 200 <= int(match.group(1)) < 300)
    return False


def verification_unavailable(output: str) -> bool:
    """Detect a policy/configuration blocker so the agent doesn't retry blindly."""
    value = str(output or "").lower()
    markers = (
        "command execution is disabled by policy",
        "shell disabled by policy",
        "no isolated command runner is configured",
        "docker sandbox requested but docker is unavailable",
        "sandbox is not configured",
        "set app_password before using the sandbox test",
        "refusing to execute on the host",
    )
    return sandbox_blocker(value) or any(marker in value for marker in markers)


def workspace_snapshot(root: str | Path, max_files: int = 90, max_manifest_chars: int = 1800) -> str:
    """Create a bounded, secret-safe workspace brief for engineering requests."""
    base = Path(root).resolve()
    limit = max(20, min(int(max_files), 500))
    manifest_chars = max(300, min(int(max_manifest_chars), 5000))
    files: list[str] = []
    found_manifests: list[str] = []
    languages: dict[str, int] = {}
    total_bytes = 0
    try:
        candidates = []
        for path in base.rglob("*"):
            try:
                rel = path.relative_to(base)
                if path.is_symlink() or not path.is_file() or any(part in SKIP_DIRS for part in rel.parts):
                    continue
                if path.name in SECRET_NAMES or path.name.startswith(".env.") or path.suffix.lower() in SECRET_SUFFIXES:
                    continue
                if path.stat().st_size > 1_000_000:
                    continue
                candidates.append(path)
            except OSError:
                continue

        def priority(path: Path):
            rel = path.relative_to(base)
            name = rel.as_posix()
            if path.name in MANIFESTS or path.name in {"AGENTS.md", "CLAUDE.md", "FORGE.md"}:
                tier = 0
            elif len(rel.parts) <= 2:
                tier = 1
            elif any(part.lower() in {"test", "tests", "spec", "specs"} for part in rel.parts):
                tier = 2
            else:
                tier = 3
            return (tier, len(rel.parts), name)

        candidates.sort(key=priority)
        selected = candidates[:limit]
        for path in selected:
            name = path.relative_to(base).as_posix()
            files.append(name)
            try:
                total_bytes += path.stat().st_size
            except OSError:
                pass
            ext = path.suffix.lower() or "[no extension]"
            languages[ext] = languages.get(ext, 0) + 1
            if (path.name in MANIFESTS or path.name in {"AGENTS.md", "CLAUDE.md", "FORGE.md"}) and len(found_manifests) < 8:
                try:
                    raw = path.read_text(encoding="utf-8", errors="replace")
                    raw = re.sub(
                        r"""(?im)^([ \t]*["']?(?:[A-Z0-9_]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|PRIVATE[_-]?KEY|CLIENT[_-]?SECRET)[A-Z0-9_]*|password|secret|token)["']?\s*[:=]\s*["']?)[^,\n}]+""",
                        r"\1[REDACTED]",
                        raw,
                    )
                    found_manifests.append(f"--- {name} ---\n{raw[:manifest_chars]}")
                except OSError:
                    pass
    except OSError:
        return "Workspace snapshot unavailable; inspect it with tools before editing."
    if not files:
        return "The active workspace is empty. Create the requested project here; do not assume files exist."
    return (
        f"Workspace root: {base}\nFiles indexed: {len(files)}"
        + (" (file list bounded)" if len(candidates) > len(files) else "")
        + f"\nApproximate bytes of indexed files: {total_bytes}\nFile types: {json.dumps(dict(sorted(languages.items(), key=lambda item: -item[1])[:14]))}"
        + "\nRelevant file tree:\n" + "\n".join(files)
        + ("\nProject manifests and instructions:\n" + "\n\n".join(found_manifests) if found_manifests else "")
        + "\nThis snapshot is only an index. Read relevant source and tests, follow references, and verify assumptions before changing code."
    )[:16000]



def sandbox_blocker(output: str) -> bool:
    """Recognize sandbox infrastructure/configuration failures, not ordinary test failures."""
    value = str(output or "").lower()
    markers = (
        "no isolated command runner is configured",
        "render sandbox is not configured",
        "render sandbox not configured",
        "render sandbox sdk missing",
        "the render python sdk is not installed",
        "command execution is disabled by policy",
        "shell disabled by policy",
        "docker sandbox requested but docker is unavailable",
        "refusing to execute on the host",
        "sandbox workspace is outside workspace",
        "sandbox provisioning failed",
        "failed to provision sandbox",
        "failed to create sandbox",
        "unable to create sandbox",
        "failed to start sandbox",
        "sandbox failed to start",
        "sandbox creation failed",
        "sandbox execution unavailable",
        "sandbox failed",
        "sandbox error",
        "render sandbox error",
        "workspace not found",
        "sandbox api returned http 401",
        "sandbox api returned http 403",
        "sandbox api returned http 404",
        "render api returned http 401",
        "render api returned http 403",
        "render api returned http 404",
        "workspace access is not available",
        "workspace entitlement",
        "sandbox network policy must be",
        "required: sandbox_render=1",
    )
    return any(marker in value for marker in markers)


def route_heads(task_text: str) -> list[str]:
    """Return a deterministic set of specialist lenses for task intent."""
    value = str(task_text or "").lower()
    heads = ["router", "planner", "critic"]
    mode = classify_task(value)
    if mode == "engineering":
        heads += ["analyst", "executor", "verifier"]
    elif mode == "research":
        heads += ["analyst", "verifier"]
    else:
        heads += ["analyst"]
    if re.search(r"\b(sandbox|shell|execute|command|security|credential|permission)\b", value):
        heads = ["router", "analyst", "executor", "verifier", "critic"]
    return list(dict.fromkeys(heads))



def action_failed(output: str) -> bool:
    """Conservatively identify failed execution/action results for loop prevention."""
    value = str(output or "").lower()
    markers = (
        "traceback (most recent call last)",
        "tool failed:",
        "failed with exit code",
        "exit code 1",
        "exit code: 1",
        "syntaxerror:",
        "modulenotfounderror:",
        "permissionerror:",
        "connectionerror:",
        "timeouterror:",
        "assertionerror:",
        "command not found",
        "no such file or directory",
        "permission denied",
        "sandbox provisioning failed",
        "failed to provision sandbox",
        "http 500",
        "http 502",
        "http 503",
        "http 504",
    )
    return any(marker in value for marker in markers)
