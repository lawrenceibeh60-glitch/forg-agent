# Forge — xKiro-powered coding agent

## Run locally

From this directory (`forge/`):

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements.txt pytest
cp .env.example .env  # Windows PowerShell: Copy-Item .env.example .env
# Edit .env and set XKIRO_API_KEY
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

Open http://localhost:8000. Use Python 3.12 rather than Python 3.14 for this pinned MCP/Pydantic dependency set. If your existing virtual environment was created with Python 3.14, recreate it with Python 3.12 before reinstalling requirements. Run `python -m pytest -q` to execute the smoke tests. xKiro is configured as the default provider when `XKIRO_API_KEY` is present. Defaults are `https://api.xkiro.com/v1` and `mistralai/devstral-medium`; override them with `XKIRO_BASE_URL` and `XKIRO_MODEL` if your account uses a different endpoint/model. Legacy `KIRO_*` variables remain supported.

## Deploy to Render

1. Push this repository to GitHub.
2. In Render, choose **New → Blueprint** and select this repository; Render will read the root `render.yaml`.
3. Set the prompted secret values: `XKIRO_API_KEY`, `APP_PASSWORD`, and any integrations you need (`GITHUB_TOKEN`, `RENDER_API_KEY`). Keep secrets in Render's Environment dashboard, not in Git.
4. Deploy. Render health checks use `/healthz`.

The Blueprint runs the service from `forge/` with `python -m pip install -r requirements.txt` and `uvicorn app:app --host 0.0.0.0 --port $PORT`.

**Persistence note:** Render's default filesystem is ephemeral. Chat sessions, provider settings saved in `data/`, MCP configs and workspace files may be lost on redeploy/restart unless you attach a persistent disk and configure paths accordingly. This app currently uses local JSON files, so a persistent disk is recommended for retaining its state. Do not store production secrets in the repository.

## Features and configuration

- Models: Settings → Models supports OpenAI-compatible providers, Anthropic, xKiro and custom endpoints. Keys added in the UI are stored in `data/providers.json` as plain text, so keep the instance private and use `APP_PASSWORD`.
- MCP: Settings → paste an `{"mcpServers": {...}}` config (stdio or HTTP/SSE). Stdio MCP servers need their runtimes installed.
- Safety: shell, git, PR, deploy and MCP tools ask for approval unless Auto-approve is enabled.
- Sandbox: each chat gets its own `workspace/<chat-id>` folder; code can be created, run and downloaded as a ZIP. `SANDBOX_DOCKER=1` enables optional Docker isolation when Docker is available.
- GitHub and Render integrations use `GITHUB_TOKEN` and `RENDER_API_KEY`.
