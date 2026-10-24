"""Deterministic execution planning and checkpoint helpers for Forge.

These helpers do not replace model reasoning. They make the model's work observable,
resumable, and evidence-driven across process restarts.
"""
from __future__ import annotations
import re
from typing import Any

PHASES = ("inspect", "plan", "implement", "verify", "review")


def seed_plan(goal: str) -> list[dict[str, Any]]:
    """Create a small, honest default plan that the model can refine."""
    text = str(goal or "").strip()
    lower = text.lower()
    engineering = bool(re.search(r"\b(code|repo|repository|implement|build|fix|debug|deploy|test|file|app|website|project|sandbox)\b", lower))
    if engineering:
        labels = [
            ("inspect", "Inspect the relevant files, project instructions, and current state"),
            ("plan", "Choose the smallest complete implementation and identify risks"),
            ("implement", "Make the changes and inspect the resulting diff"),
            ("verify", "Run focused tests or safe checks; repair failures and rerun"),
            ("review", "Review scope, regressions, security, and report evidence"),
        ]
    else:
        labels = [
            ("understand", "Resolve the user's intent, constraints, and required outcome"),
            ("work", "Reason through the request and use tools only where they add value"),
            ("review", "Check accuracy, assumptions, and completeness before answering"),
        ]
    return [{"id": key, "label": label, "status": "pending", "evidence": ""} for key, label in labels]


def normalize_plan(raw: Any, previous: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Normalize markdown or structured plans while preserving completed evidence."""
    old = {str(x.get("label", "")).casefold(): x for x in (previous or []) if isinstance(x, dict)}
    lines = raw.splitlines() if isinstance(raw, str) else raw if isinstance(raw, list) else []
    result = []
    for item in lines[:80]:
        if isinstance(item, dict):
            label = str(item.get("label", item.get("step", ""))).strip()
            status = str(item.get("status", "pending")).lower()
            evidence = str(item.get("evidence", ""))[:1200]
        else:
            line = str(item).strip()
            done = bool(re.search(r"\[[xX]\]|^(?:done|complete|completed|verified)\b", line, re.I))
            label = re.sub(r"^\s*(?:[-*+]\s*)?(?:\[[ xX]\]\s*)?(?:\d+[.)]\s*)?", "", line).strip()
            status = "completed" if done else "pending"
            evidence = ""
        if not label:
            continue
        label = label[:300]
        prior = old.get(label.casefold(), {})
        if prior.get("status") == "completed":
            status, evidence = "completed", str(prior.get("evidence", evidence))[:1200]
        if status not in {"pending", "in_progress", "completed", "blocked"}:
            status = "pending"
        result.append({"id": str(prior.get("id") or f"step-{len(result)+1}"),
                       "label": label, "status": status, "evidence": evidence})
    return result


def record_step(plan: list[dict[str, Any]], label: str, status: str, evidence: str = "") -> list[dict[str, Any]]:
    """Mark the best matching step and keep a single active step."""
    normalized = normalize_plan(plan)
    if status not in {"pending", "in_progress", "completed", "blocked"}:
        raise ValueError("invalid step status")
    needle = str(label or "").strip().casefold()
    match = next((i for i, s in enumerate(normalized) if s["label"].casefold() == needle), None)
    if match is None:
        match = next((i for i, s in enumerate(normalized) if needle and (needle in s["label"].casefold() or s["label"].casefold() in needle)), None)
    if match is None:
        return normalized
    if status == "in_progress":
        for step in normalized:
            if step["status"] == "in_progress":
                step["status"] = "pending"
    normalized[match]["status"] = status
    if evidence:
        normalized[match]["evidence"] = str(evidence)[:1200]
    return normalized


def tool_risk(name: str) -> str:
    """Advisory routing label; authorization must still be enforced separately."""
    name = str(name or "")
    if name.startswith("mcp__") or name in {"render_deploy", "render_set_env", "render_suspend",
            "render_resume", "render_create_service", "github_merge_pr", "github_create_repo",
            "repo_commit_push", "publish_to_github"}:
        return "external_mutation"
    if name in {"delete_file", "move_file", "bulk_replace", "apply_patch", "write_file",
                "edit_file", "append_file", "run_command", "pip_install", "npm_install",
                "python_eval", "node_eval", "http_request"}:
        return "workspace_mutation_or_execution"
    if name in {"run_tests", "lint", "check_url", "project_map", "find_symbol",
                "find_references", "dependency_graph", "read_file", "search_code", "list_files"}:
        return "inspection_or_verification"
    return "read_only_or_general"


def checkpoint(phase: str, **details: Any) -> dict[str, Any]:
    """Bound and sanitize checkpoint metadata; never store environment variables."""
    safe = {"phase": str(phase)[:80]}
    for key, value in details.items():
        if key.lower() in {"env", "environment", "token", "secret", "password", "api_key"}:
            continue
        if isinstance(value, str):
            safe[key[:80]] = value[:1500]
        elif isinstance(value, (int, float, bool)) or value is None:
            safe[key[:80]] = value
        elif isinstance(value, list):
            safe[key[:80]] = value[:80]
        elif isinstance(value, dict):
            safe[key[:80]] = {str(k)[:80]: str(v)[:500] for k, v in list(value.items())[:40]
                              if not re.search(r"token|secret|password|api.?key|credential", str(k), re.I)}
    return safe
