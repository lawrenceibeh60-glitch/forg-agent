# Forge Agent

Forge is a streaming, tool-using AI workspace with configurable OpenAI-compatible / Anthropic providers, GitHub and Render integrations, reference-image input, per-session workspaces, task checkpoints, and an opt-in model fallback.

## Adaptive Agent Engine v2

Forge now includes a task-aware runtime layer intended to make the configured model more effective at real engineering work instead of merely exposing a large flat tool list.

- **Adaptive tool routing:** selects a focused, relevant set of tool schemas per cycle while retaining essential engineering tools. This reduces schema overload; it is a ranking layer, not a permission system.
- **Tool discovery:** `tool_catalog` searches the full registered built-in tool catalog when a needed capability is not present in the current selection. Connected MCP tools are surfaced when relevant.
- **Workspace reconnaissance:** engineering tasks receive a bounded, secret-safe file index and key project manifests before the model starts acting. This is not a substitute for reading relevant source files.
- **Architecture navigation:** `project_map` reports manifests, test files, directories, and Python symbols; `find_symbol` locates definitions; `find_references` finds usages; `dependency_graph` estimates local module dependencies.
- **Evidence loop:** system instructions explicitly require inspect → implement → test → inspect output → repair → retest → review, with honest reporting when execution is blocked.
- **Verification gate:** running a check no longer counts as success by itself. Forge checks exit codes / HTTP status, retries repair prompts in a bounded way after failed or missing verification, and stops blindly retrying when sandbox execution is blocked by policy or configuration.
- **Purpose routing:** engineering tasks use the coding profile, research tasks use the review profile, and ordinary conversation uses the general profile. Task-specific profiles activate only when fully configured.

Runtime tuning: `FORGE_AGENT_TOOL_BUDGET` controls the maximum selected tool schemas per cycle (default 42; clamped to 16–64). `FORGE_CONTEXT_BUDGET_CHARS` controls when older context is compacted (default 90,000 characters; clamped to 32,000–240,000). Keep the budget below the active model's real context capacity to leave room for system instructions and tool schemas. These are guardrails, not a guarantee that every provider accepts the same context size.

These changes do not enlarge the underlying language model or create a new neural network. They improve how Forge supplies context, discovers capabilities, and uses tool feedback. Results still depend on the configured model, its context/tool-call quality, sandbox availability, and connected services.

## Runtime design

- **Primary model:** configured in Settings, or from `XKIRO_API_KEY`, `XKIRO_BASE_URL`, and `XKIRO_MODEL`.
- **Agent loop:** streams progress, executes tool calls, preserves recent conversation turns during compaction, checks completion, and nudges verification after edits.
- **Task checkpoints:** SQLite records the goal, status, plan, completed steps, current step, recent tool result, and execution metadata. `GET /api/tasks` lists tasks; `GET /api/tasks/{id}` inspects one; `POST /api/tasks/{id}/resume` resumes a non-completed task using its saved conversation and checkpoint.
- **Conversation history:** `FORGE_SESSION_DIR` selects the directory for session JSON. Writes are atomic and session IDs are validated.
- **Reference images:** attach up to four PNG/JPEG/WEBP/GIF images. The configured model must support vision for image interpretation.
- **Provider failover:** configure all of `FORGE_FALLBACK_API_KEY`, `FORGE_FALLBACK_BASE_URL`, and `FORGE_FALLBACK_MODEL` to opt in. Optional `FORGE_FALLBACK_KIND` is `openai` (default) or `anthropic`. Failover only occurs before the primary emits content; Forge never mixes partial answers from two providers.
- **Sandbox:** command execution uses Render-managed, ephemeral sandboxes when `SANDBOX_RENDER=1` and Render credentials are configured. The web service itself never executes arbitrary project commands in this mode. Workspace sync excludes environment files, keys, git metadata, and dependency caches; output sync is size-limited and never deletes host files. Outbound network is disabled by default (`SANDBOX_NETWORK=0`). Render Sandboxes are early access and must be enabled for the workspace. Provisioning failures fail closed without falling back to the host. Keep `SANDBOX_ALLOW_HOST_EXECUTION=0` in public deployments.
- **Safety boundary:** the Docker runner drops Linux capabilities, uses no-new-privileges, caps memory/CPU/processes, and defaults to no network. Workspace file edits remain available when shell execution is disabled.

## Project intelligence and quality tools

Forge includes a session-scoped Project Brain panel in the sidebar:

- **Workspace map:** lists safe project files, size/language counts, and Python class/function symbols. It skips secrets, symlinks, VCS metadata, dependency caches, and common build folders.
- **Project search:** searches text files with file/line references, capped results, and size limits.
- **Project memory:** stores explicit project decisions, architecture notes, facts, preferences, TODOs, and lessons from failures in SQLite, scoped to the chat session. It rejects likely credential-shaped content. Do not store secrets or personal sensitive information.
- **Security review:** performs a lightweight heuristic scan for suspicious hard-coded credentials, disabled TLS verification, unsafe pickle deserialization, shell-enabled subprocesses, and debug servers. It is a review aid, not a security certification.
- **Operations overview:** reports model/fallback configuration, integration presence, task storage health, sandbox readiness, and key safety settings without returning secret values.
- **Evaluation catalog:** documents behavioral regression targets. This endpoint is a catalog, not a claim that the model has passed live evaluations.

New endpoints:

- `GET /api/brain/map?sid=<session>`
- `GET /api/brain/search?sid=<session>&q=<query>`
- `GET /api/brain/memory?sid=<session>`
- `POST /api/brain/memory` with `session`, `kind`, `title`, `body`
- `DELETE /api/brain/memory/<id>?sid=<session>`
- `GET /api/security/report?sid=<session>`
- `GET /api/ops/overview`
- `GET /api/evals/catalog`

The intelligence database defaults to the same SQLite file as task checkpoints. For durable memory across deploys, configure `TASK_DB_PATH` on persistent storage. Project memory is not automatically inferred from model output; save only confirmed, useful decisions.

## Environment variables

| Variable | Purpose |
| --- | --- |
| `XKIRO_API_KEY` | Primary xKiro API key |
| `XKIRO_BASE_URL` | Primary API base URL; defaults to `https://api.xkiro.com/v1` |
| `XKIRO_MODEL` | Primary model; defaults to `mistralai/devstral-medium` |
| `FORGE_MAX_AGENT_TURNS` | Agent execution budget, clamped to 40–80; default 60 |
| `FORGE_FALLBACK_API_KEY` | Optional fallback provider key |
| `FORGE_FALLBACK_BASE_URL` | Optional fallback API base URL |
| `FORGE_FALLBACK_MODEL` | Optional fallback model ID |
| `FORGE_FALLBACK_KIND` | `openai` or `anthropic` |
| `FORGE_CODING_API_KEY` / `FORGE_CODING_BASE_URL` / `FORGE_CODING_MODEL` | Optional dedicated coding model; configure all three to enable routing |
| `FORGE_REVIEW_API_KEY` / `FORGE_REVIEW_BASE_URL` / `FORGE_REVIEW_MODEL` | Optional dedicated specialist-review model |
| `FORGE_SUMMARY_API_KEY` / `FORGE_SUMMARY_BASE_URL` / `FORGE_SUMMARY_MODEL` | Optional lower-cost context-summary model |
| `FORGE_CODING_KIND`, `FORGE_REVIEW_KIND`, `FORGE_SUMMARY_KIND` | Optional provider kind per route: `openai` (default) or `anthropic` |
| `TASK_DB_PATH` | SQLite checkpoint database path; default `data/tasks.sqlite3` |
| `FORGE_SESSION_DIR` | Session history directory; default `data/sessions` |
| `ALLOW_SHELL` | Must be `1` to enable any command runner |
| `SANDBOX_DOCKER` | Must be `1` to use Docker isolation when Render-managed mode is off |
| `SANDBOX_RENDER` | Use Render-managed ephemeral sandboxes when `1` |
| `RENDER_WORKSPACE_ID` | Render workspace ID required by the managed sandbox SDK |
| `SANDBOX_NETWORK` | Opt-in outbound network access from sandbox jobs; default off |
| `SANDBOX_NETWORK_POLICY` | Render sandbox outbound policy; default `deny-all` |
| `SANDBOX_COMMAND_TIMEOUT` | Maximum per-command runtime in seconds; default 120 |
| `SANDBOX_ALLOW_HOST_EXECUTION` | Development-only host shell escape hatch; keep `0) on public deployments |
| `APP_PASSWORD` | Optional app access password / encryption fallback |
| `OAUTH_ENCRYPTION_SECRET` | Stable encryption secret for connected OAuth tokens |

Do not put API keys in source control. Configure secrets through the Render dashboard.

## Persistence on Render

SQLite and session files survive process restarts when the filesystem remains intact. **They do not survive every redeploy on an ephemeral filesystem.** For cross-deploy task and conversation persistence, attach a persistent disk to the service and set both `TASK_DB_PATH` and `FORGE_SESSION_DIR` to paths under its mount directory. The current free Render service plan may not support persistent disks; if not, use a supported persistent storage service before relying on checkpoints across deploys.

## GitHub Mission Control

The Project Brain panel includes an on-demand GitHub snapshot for the connected account: recently updated repositories, open pull requests, and recent GitHub Actions runs for a selected `owner/name` repository. The endpoint never returns the access token. Repository and workflow data require a valid GitHub connection or `GITHUB_TOKEN`.

- `GET /api/github/mission`
- `GET /api/github/mission?repo=owner/name`

## Specialist team reviews

The Project Brain panel can run an on-demand, sequential review from three specialist roles: architect, security reviewer, and test strategist. It builds a bounded workspace map and sends it with the review goal to the configured model. Configure `FORGE_REVIEW_API_KEY`, `FORGE_REVIEW_BASE_URL`, and `FORGE_REVIEW_MODEL` together to use a dedicated reviewer model; otherwise the primary model is reused. This advisory review does not edit files or claim tests were executed.

- `GET /api/agents/specialists`
- `POST /api/agents/team-review` with `{ "session": "...", "goal": "..." }`

The model layer also supports optional task-specific `FORGE_CODING_*`, `FORGE_REVIEW_*`, and `FORGE_SUMMARY_*` profiles. A profile activates only when its API key, base URL, and model are all set; otherwise Forge uses the normal primary provider and its configured fallback behavior.

## Visual workflow builder

The Project Brain panel can save and run reusable, session-scoped workflows. Supported steps are `map_workspace`, `search_workspace`, `security_scan`, and `list_memory`. Workflows are limited to 20 steps and cannot run arbitrary shell commands or external side effects. Step results are persisted during execution so failed runs retain their completed-step output.

- `GET /api/workflows?sid=<session>`
- `POST /api/workflows` with a session, name, and ordered steps
- `POST /api/workflows/<id>/run?sid=<session>`
- `DELETE /api/workflows/<id>?sid=<session>`

## Evaluation Lab

Project Brain exposes an explicit opt-in evaluation run. The user must confirm before Forge makes model calls; the API caps each run at 10 cases and the UI defaults to five. The run uses no tools and stores a bounded JSON report at `data/eval-results.json` (override with `FORGE_EVAL_RESULTS_PATH`). These are behavioral smoke checks, not a comprehensive benchmark. On an ephemeral Render filesystem, the report may not survive redeploys.

- `GET /api/evals/catalog`
- `POST /api/evals/run` with `{ "confirm": true, "limit": 5 }`

## Run checks

From the repository root:

```bash
python -m pip install -r forge/requirements.txt pytest
python -m compileall -q forge
cd forge
python -m pytest -q
```

The regression tests cover SQLite task checkpoints, task scoping/status validation, session compaction continuity, model failover, workspace path traversal, and fail-closed shell execution.

## Model evaluation

The opt-in evaluation suite makes real requests to the configured model and may consume API tokens:

```bash
cd forge
python evals/run.py
```

It checks basic reasoning, recent-turn reference resolution, answers to prior questions, selective tool judgment, verification honesty, and focused clarification. Results are written to `forge/data/eval-results.json`. These are lightweight behavioral checks, not a claim of parity with Claude or a comprehensive benchmark.

## Useful endpoints

- `GET /healthz` — application health
- `GET /api/status` — configured integrations
- `GET /api/sandbox/status` — effective execution mode
- `GET /api/tasks` — recent task checkpoints and storage status
- `GET /api/tasks/{task_id}` — one checkpoint
- `POST /api/tasks/{task_id}/resume` — resume a blocked/failed/running task
- `GET /api/sessions/{session_id}/tasks` — tasks associated with a conversation
