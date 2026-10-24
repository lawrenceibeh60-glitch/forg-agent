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


def workspace_snapshot(root: str | Path, max_files: int = 90, max_manifest_chars: int = 1800) -> str:
    """Create a bounded, secret-safe workspace brief for engineering requests."""
    base = Path(root).resolve()
    files: list[str] = []
    found_manifests: list[str] = []
    languages: dict[str, int] = {}
    total_bytes = 0
    try:
        for path in base.rglob("*"):
            try:
                rel = path.relative_to(base)
                if path.is_symlink() or any(part in SKIP_DIRS for part in rel.parts):
                    continue
                if not path.is_file():
                    continue
                if path.name in SECRET_NAMES or path.name.startswith(".env.") or path.suffix.lower() in SECRET_SUFFIXES:
                    continue
                if path.stat().st_size > 1_000_000:
                    continue
                name = rel.as_posix()
                files.append(name)
                total_bytes += path.stat().st_size
                ext = path.suffix.lower() or "[no extension]"
                languages[ext] = languages.get(ext, 0) + 1
                if name in MANIFESTS or path.name in MANIFESTS:
                    if len(found_manifests) < 8:
                        try:
                            raw = path.read_text(encoding="utf-8", errors="replace")
                            found_manifests.append(f"--- {name} ---\n{raw[:max_manifest_chars]}")
                        except OSError:
                            pass
                if len(files) >= max_files:
                    break
            except OSError:
                continue
    except OSError:
        return "Workspace snapshot unavailable; inspect it with tools before editing."
    files.sort()
    if not files:
        return "The active workspace is empty. Create the requested project here; do not assume files exist."
    return (
        f"Workspace root: {base}\nFiles indexed: {len(files)}"
        + (" (file list bounded)" if len(files) >= max_files else "")
        + f"\nApproximate file bytes: {total_bytes}\nFile types: {json.dumps(dict(sorted(languages.items(), key=lambda item: -item[1])[:14]))}"
        + "\nRelevant file tree:\n" + "\n".join(files[:max_files])
        + ("\nProject manifests and entry docs:\n" + "\n\n".join(found_manifests) if found_manifests else "")
        + "\nThis snapshot is only an index. Read relevant source files, follow references, and verify assumptions before changing code."
    )[:16000]
