# Forge Agent

Forge is a streaming, tool-using AI workspace with configurable OpenAI-compatible / Anthropic providers, GitHub and Render integrations, reference-image input, per-session workspaces, task checkpoints, and an opt-in model fallback.

## Runtime design

- **Primary model:** configured in Settings, or from `XKIRO_API_KEY`, `XKIRO_BASE_URL`, and `XKIRO_MODEL`.
- **Agent loop:** streams progress, executes tool calls, preserves recent conversation turns during compaction, checks completion, and nudges verification after edits.
- **Task checkpoints:** SQLite records the goal, status, plan, completed steps, current step, recent tool result, and execution metadata. `GET /api/tasks` lists tasks; `GET /api/tasks/{id}` inspects one; `POST /api/tasks/{id}/resume` resumes a non-completed task using its saved conversation and checkpoint.
- **Conversation history:** `FORGE_SESSION_DIR` selects the directory for session JSON. Writes are atomic and session IDs are validated.
- **Reference images:** attach up to four PNG/JPEG/WEBP/GIF images. The configured model must support vision for image interpretation.
- **Provider failover:** configure all of `FORGE_FALLBACK_API_KEY`, `FORGE_FALLBACK_BASE_URL`, and `FORGE_FALLBACK_MODEL` to opt in. Optional `FORGE_FALLBACK_KIND` is `openai` (default) or `anthropic`. Failover only occurs before the primary emits content; Forge never mixes partial answers from two providers.
- **Sandbox:** command execution uses Render-managed, ephemeral sandboxes when `SANDBOX_RENDER=1` and Render credentials are configured. The web service itself never executes arbitrary project commands in this mode. Workspace sync excludes environment files, keys, git metadata, and dependency caches; output sync is size-limited and never deletes host files. Outbound network is disabled by default (`SANDBOX_NETWORK=0`). Render Sandboxes are early access and must be enabled for the workspace. Provisioning failures fail closed without falling back to the host. Keep `SANDBOX_ALLOW_HOST_EXECUTION=0` in public deployments.
- **Safety boundary:** the Docker runner drops Linux capabilities, uses no-new-privileges, caps memory/CPU/processes, and defaults to no network. Workspace file edits remain available when shell execution is disabled.

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
| `TASK_DB_PATH` | SQLite checkpoint database path; default `data/tasks.sqlite3` |
| `FORGE_SESSION_DIR` | Session history directory; default `data/sessions` |
| `ALLOW_SHELL` | Must be `1` to enable any command runner |
| `SANDBOX_DOCKER` | Must be `1` to use Docker isolation when Render-managed mode is off |
| `SANDBOX_RENDER` | Use Render-managed ephemeral sandboxes when `1` |\n| `RENDER_WORKSPACE_ID` | Render workspace ID required by the managed sandbox SDK |\n| `SANDBOX_NETWORK` | Opt-in outbound network access from sandbox jobs; default off |\n| `SANDBOX_NETWORK_POLICY` | Render sandbox outbound policy; default `deny` |\n| `SANDBOX_COMMAND_TIMEOUT` | Maximum per-command runtime in seconds; default 120 |
| `SANDBOX_ALLOW_HOST_EXECUTION` | Development-only host shell escape hatch; keep `0) on public deployments |
| `APP_PASSWORD` | Optional app access password / encryption fallback |
| `OAUTH_ENCRYPTION_SECRET` | Stable encryption secret for connected OAuth tokens |

Do not put API keys in source control. Configure secrets through the Render dashboard.

## Persistence on Render

SQLite and session files survive process restarts when the filesystem remains intact. **They do not survive every redeploy on an ephemeral filesystem.** For cross-deploy task and conversation persistence, attach a persistent disk to the service and set both `TASK_DB_PATH` and `FORGE_SESSION_DIR` to paths under its mount directory. The current free Render service plan may not support persistent disks; if not, use a supported persistent storage service before relying on checkpoints across deploys.

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
