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

## Deploy to Vercel

1. Push this repository to GitHub.
2. Create a Vercel project connected to this repository. Set Root Directory to `forge`, select Python 3.12, and deploy the `feat/forge-agentic-personality-sandbox` branch or merge it into your production branch.
3. Set `XKIRO_API_KEY`, `FORGE_SESSION_SECRET`, `OAUTH_ENCRYPTION_SECRET`, `GITHUB_ALLOWED_USERS`, `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, and `GITHUB_REDIRECT_URI` in Vercel Environment Variables. The callback URL must exactly match the URL registered in your GitHub OAuth App. Users then click **Sign in with GitHub**; they do not paste a personal GitHub token. Keep secrets in Vercel Environment Variables, not in Git.
4. Deploy. Render health checks use `/healthz`.

Set `SANDBOX_VERCEL=1`, `SANDBOX_RENDER=0`, `SANDBOX_DOCKER=0`, `SANDBOX_NETWORK=1`, `ALLOW_SHELL=1`, and `VERCEL_SANDBOX_REGION=bom1`. Networking applies inside the isolated sandbox VM; host execution stays disabled. Register `https://YOUR-VERCEL-DOMAIN/auth/github/callback` in the GitHub OAuth App.

**Persistence note:** Vercel Function filesystems are ephemeral. Named Vercel Sandboxes preserve the per-workspace Linux environment and package installs, but app-level chat/task/provider JSON and SQLite state still require a durable database or object store to survive redeployments and scale across function instances. Do not store production secrets in the repository.

## Features and configuration

- Models: Settings → Models supports OpenAI-compatible providers, Anthropic, xKiro and custom endpoints. Keys added in the UI are stored in `data/providers.json` as plain text, so only approved GitHub accounts should access the instance.
- MCP: Settings → paste an `{"mcpServers": {...}}` config (stdio or HTTP/SSE). Stdio MCP servers need their runtimes installed.
- Safety: shell, git, PR, deploy and MCP tools ask for approval unless Auto-approve is enabled.
- Sandbox: each chat gets its own `workspace/<chat-id>` folder; code can be created, run and downloaded as a ZIP. `SANDBOX_VERCEL=1` selects persistent named Vercel Linux Sandboxes for shell, Python, IPython, Node, package installs, background processes, and live previews. `sandbox_runtime_info` reports the actual OS image and installed runtimes; `linux_package_install` uses the package manager available in that image (apt/dnf/yum/apk). `SANDBOX_NETWORK=1` allows outbound package installation only inside the isolated sandbox; host execution remains disabled.
- GitHub supports interactive OAuth sign-in and stores its access token encrypted at rest. `GITHUB_TOKEN` remains an optional service-account fallback. The OAuth app client ID/secret are configured once by the deployment owner, not entered by each user. Render's public REST API still requires a Render API key. Render also provides an OAuth-enabled MCP server, but the current MCP client does not yet implement its browser callback flow; the UI reports Render as connected only when an API key or an actual connected Render MCP server is present.


### Live previews

For runnable web projects, Forge provides `preview_start` and `preview_stop` tools. The preview tool stages workspace files into a separate Vercel Linux Sandbox, installs project dependencies there, starts a detected HTML/static, Vite/React, Flask, or FastAPI/Uvicorn server, and returns Vercel's exposed-port HTTPS route. A custom server command and port can be supplied when auto-detection is not enough.

Preview sandboxes are temporary (configured for up to 45 minutes on the Hobby limit), use outbound network access to fetch dependencies, and are isolated from the Forge web service. Links are public to anyone who has them; do not put credentials, private datasets, or secrets in a preview. The server-side workspace deliberately excludes environment files and credential material. Use `preview_stop` to terminate the active preview early. This feature requires the Vercel Sandbox SDK and valid Vercel Sandbox access. It does not depend on Render Sandboxes or Cloudflare tunnels.
