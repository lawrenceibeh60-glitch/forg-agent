# Forge — coding agent
```
pip install -r requirements.txt   # Python 3.10+
cp .env.example .env
uvicorn app:app --reload          # http://localhost:8000
```
- **Models**: Settings → Models. Pick OpenAI, Anthropic, Gemini, Groq, OpenRouter, DeepSeek, Mistral, xAI, Together, Ollama, Kiro or Custom (any OpenAI-compatible URL), paste the key, name the model. Keys are stored in data/providers.json (plain text, keep that folder private).
- **MCP**: Settings → paste any `{"mcpServers": {...}}` config (stdio or HTTP/SSE). npx servers need Node.
- **Safety**: shell, git, PR, deploy and MCP tools ask for approval unless Auto-approve is on. Set APP_PASSWORD to require a login.
- **Sandbox**: each chat gets its own folder (workspace/<chat-id>): create files, run code, start servers, download as .zip. Set SANDBOX_DOCKER=1 for container isolation.
- **Tools**: 100+ built in. Toggle in Settings → Tools. Add yours with @tool(...) in tools_extra.py or tools_ops.py.
- GitHub token and Render key go in .env (GITHUB_TOKEN, RENDER_API_KEY).

## Making it smarter
Forge's intelligence comes from the model you plug in. For Claude-level results add an Anthropic key and pick a current Claude model; weaker models will be weaker. Forge adds: a Claude-style system prompt (prompts.py), a `think` scratchpad, automatic context compaction for long sessions, retries on rate limits, and project instructions (put FORGE.md, AGENTS.md or CLAUDE.md in any repo and the agent reads it).
