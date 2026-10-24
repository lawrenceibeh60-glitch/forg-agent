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
3. Set `XKIRO_API_KEY` and `APP_PASSWORD`. For interactive GitHub sign-in, register a GitHub OAuth App once and set `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `GITHUB_REDIRECT_URI` (`https://YOUR-SERVICE.onrender.com/auth/github/callback`), and a stable `OAUTH_ENCRYPTION_SECRET` in Render Environment. Users then click **Connect GitHub**; they do not paste a personal GitHub token. The callback URL must exactly match the OAuth App setting. Keep secrets in Render's Environment dashboard, not in Git.
4. Deploy. Render health checks use `/healthz`.

The Blueprint runs the service from `forge/` with `python -m pip install -r requirements.txt` and `uvicorn app:app --host 0.0.0.0 --port $PORT`.

**Persistence note:** Render's default filesystem is ephemeral. Chat sessions, provider settings saved in `data/`, MCP configs and workspace files may be lost on redeploy/restart unless you attach a persistent disk and configure paths accordingly. This app currently uses local JSON files, so a persistent disk is recommended for retaining its state. Do not store production secrets in the repository.

## Features and configuration

- Models: Settings → Models supports OpenAI-compatible providers, Anthropic, xKiro and custom endpoints. Keys added in the UI are stored in `data/providers.json` as plain text, so keep the instance private and use `APP_PASSWORD`.
- MCP: Settings → paste an `{"mcpServers": {...}}` config (stdio or HTTP/SSE). Stdio MCP servers need their runtimes installed.
- Safety: shell, git, PR, deploy and MCP tools ask for approval unless Auto-approve is enabled.
- Sandbox: each chat gets its own `workspace/<chat-id>` folder; code can be created, run and downloaded as a ZIP. `SANDBOX_DOCKER=1` enables optional Docker isolation when Docker is available.
- GitHub supports interactive OAuth sign-in and stores its access token encrypted at rest. `GITHUB_TOKEN` remains an optional service-account fallback. The OAuth app client ID/secret are configured once by the deployment owner, not entered by each user. Render's public REST API still requires a Render API key. Render also provides an OAuth-enabled MCP server, but the current MCP client does not yet implement its browser callback flow; the UI reports Render as connected only when an API key or an actual connected Render MCP server is present.


### Live previews

For runnable web projects, Forge provides `preview_start` and `preview_stop` tools. The preview tool stages workspace files into a separate Render sandbox, installs project dependencies there, starts a detected HTML/static, Vite/React, Flask, or FastAPI/Uvicorn server, and creates a temporary HTTPS link using a Cloudflare Quick Tunnel. A custom server command and port can be supplied when auto-detection is not enough.

Preview sandboxes are temporary (configured for up to one hour), use outbound network access to fetch dependencies and establish the tunnel, and are isolated from the Forge web service. Links are public to anyone who has them; do not put credentials, private datasets, or secrets in a preview. The server-side workspace deliberately excludes environment files and credential material. Use `preview_stop` to terminate the active preview early. This feature still requires Render Sandboxes to be enabled and correctly configured; it does not bypass a Render sandbox provisioning or API failure.
