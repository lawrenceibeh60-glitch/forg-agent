"""Universal model layer: any OpenAI-compatible API (OpenAI, Gemini, Groq, OpenRouter, DeepSeek, Mistral, xAI, Together, Ollama, Kiro, custom) + native Anthropic."""
import os, re, json, uuid, asyncio, httpx
from pathlib import Path

PF = Path("data/providers.json")
PRESETS = {
    "openai": ("https://api.openai.com/v1", "openai"), "anthropic": ("https://api.anthropic.com", "anthropic"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "openai"),
    "groq": ("https://api.groq.com/openai/v1", "openai"), "openrouter": ("https://openrouter.ai/api/v1", "openai"),
    "deepseek": ("https://api.deepseek.com/v1", "openai"), "mistral": ("https://api.mistral.ai/v1", "openai"),
    "xai": ("https://api.x.ai/v1", "openai"), "together": ("https://api.together.xyz/v1", "openai"),
    "ollama": ("http://localhost:11434/v1", "openai"),
    "xkiro": (os.getenv("XKIRO_BASE_URL", "https://api.xkiro.com/v1"), "openai"),
    "kiro": (os.getenv("KIRO_BASE_URL", os.getenv("XKIRO_BASE_URL", "https://api.xkiro.com/v1")), "openai"),
    "custom": ("", "openai"),
}

def load():
    # Environment configuration must remain effective even when a previous UI
    # session created providers.json. Never overwrite a user-configured xKiro
    # provider, but seed/update it from deployment env when missing.
    d = json.loads(PF.read_text()) if PF.exists() else {"active": None, "providers": []}
    if not isinstance(d, dict):
        d = {"active": None, "providers": []}
    d.setdefault("providers", [])
    d.setdefault("active", None)
    xkiro_key = os.getenv("XKIRO_API_KEY") or os.getenv("KIRO_API_KEY")
    if xkiro_key:
        p = next((p for p in d["providers"] if p.get("id") == "xkiro"), None)
        if p is None:
            p = {"id": "xkiro", "name": "xKiro", "kind": "openai"}
            d["providers"].insert(0, p)
        # Deployment environment is authoritative for the managed xKiro entry.
        p.update({"name": "xKiro", "base_url": os.getenv("XKIRO_BASE_URL") or os.getenv("KIRO_BASE_URL") or "https://api.xkiro.com/v1",
                  "kind": "openai", "key": xkiro_key,
                  "model": os.getenv("XKIRO_MODEL") or os.getenv("KIRO_MODEL") or "mistralai/devstral-medium"})
        if not d.get("active") or not any(x.get("id") == d["active"] for x in d["providers"]):
            d["active"] = "xkiro"
    return d

def save(d): PF.parent.mkdir(exist_ok=True); PF.write_text(json.dumps(d, indent=1))

def public():
    d = load()
    return {"active": d["active"], "presets": {k: v[0] for k, v in PRESETS.items()},
            "providers": [{**p, "key": "…" + p["key"][-4:] if p["key"] else ""} for p in d["providers"]]}

def add(preset, key, model, base_url="", name=""):
    d = load(); url, kind = PRESETS.get(preset, PRESETS["custom"])
    pid = uuid.uuid4().hex[:6]
    d["providers"].append({"id": pid, "name": name or f"{preset}:{model}", "base_url": base_url or url, "kind": kind, "key": key, "model": model})
    d["active"] = pid; save(d); return pid

def remove(pid):
    d = load(); d["providers"] = [p for p in d["providers"] if p["id"] != pid]
    if d["active"] == pid: d["active"] = d["providers"][0]["id"] if d["providers"] else None
    save(d)

def activate(pid): d = load(); d["active"] = pid; save(d)

def active():
    d = load(); p = next((p for p in d["providers"] if p["id"] == d["active"]), None)
    if not p: raise RuntimeError("No model configured. Open Settings → Models and add an API key.")
    return p

def fallback_provider():
    """Return an explicitly configured fallback; never silently invent credentials."""
    key = os.getenv("FORGE_FALLBACK_API_KEY", "").strip()
    model = os.getenv("FORGE_FALLBACK_MODEL", "").strip()
    base_url = os.getenv("FORGE_FALLBACK_BASE_URL", "").strip()
    if not (key and model and base_url):
        return None
    kind = os.getenv("FORGE_FALLBACK_KIND", "openai").strip().lower()
    if kind not in {"openai", "anthropic"}:
        return None
    return {"id": "env-fallback", "name": "Configured fallback", "key": key,
            "model": model, "base_url": base_url.rstrip("/"), "kind": kind}


async def stream(messages, tools):
    """Stream from the primary model, retry transient failures, then optionally fail over.

    Fallback is only attempted before any response content has been emitted. This
    avoids mixing two model responses or duplicating partial answers/tool calls.
    """
    primary = active()
    providers = [primary]
    fallback = fallback_provider()
    if fallback and fallback["base_url"] != primary.get("base_url"):
        providers.append(fallback)
    last_error = None
    for provider_index, p in enumerate(providers):
        for attempt in range(3):
            started = False
            try:
                async for item in (_anthropic if p["kind"] == "anthropic" else _openai)(p, messages, tools):
                    started = True
                    yield item
                return
            except Exception as exc:
                last_error = exc
                if started:
                    raise
                transient = bool(re.search(r"\b(429|5\d\d)\b", str(exc))) or isinstance(
                    exc, (httpx.TimeoutException, httpx.NetworkError)
                )
                if transient and attempt < 2:
                    await asyncio.sleep(3 * 2 ** attempt)
                    continue
                if provider_index + 1 < len(providers):
                    print("Primary model unavailable before output; trying configured fallback:", type(exc).__name__)
                    break
                raise
    if last_error:
        raise last_error
    raise RuntimeError("No model provider could be selected.")

async def _openai(p, messages, tools):
    body = {"model": p["model"], "messages": messages, "stream": True}
    if tools: body["tools"] = tools
    h = {"Authorization": f"Bearer {p['key']}"} if p["key"] else {}
    text, calls = "", {}
    async with httpx.AsyncClient(timeout=None) as c:
        async with c.stream("POST", p["base_url"].rstrip("/") + "/chat/completions", json=body, headers=h) as r:
            if r.status_code >= 400: raise RuntimeError(f"{p['name']} {r.status_code}: {(await r.aread()).decode()[:300]}")
            async for line in r.aiter_lines():
                if not line.startswith("data:"): continue
                d = line[5:].strip()
                if d == "[DONE]": break
                ch = json.loads(d).get("choices") or []
                if not ch: continue
                dl = ch[0].get("delta", {})
                if dl.get("content"): text += dl["content"]; yield "text", dl["content"]
                for t in dl.get("tool_calls") or []:
                    c_ = calls.setdefault(t.get("index", 0), {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                    c_["id"] = t.get("id") or c_["id"]
                    f = t.get("function", {})
                    c_["function"]["name"] += f.get("name") or ""; c_["function"]["arguments"] += f.get("arguments") or ""
    m = {"role": "assistant", "content": text or None}
    if calls: m["tool_calls"] = [dict(v, id=v["id"] or uuid.uuid4().hex[:12]) for v in calls.values()]
    yield "message", m

async def _anthropic(p, messages, tools):
    system, out = "", []
    for m in messages:
        if m["role"] == "system": system = m["content"]
        elif m["role"] == "user":
            content = m["content"]
            if isinstance(content, list):
                blocks = []
                for part in content:
                    if part.get("type") == "text":
                        blocks.append({"type": "text", "text": part.get("text", "")})
                    elif part.get("type") == "image_url":
                        url = part.get("image_url", {}).get("url", "")
                        match = re.fullmatch(r"data:image/(png|jpeg|webp|gif);base64,([A-Za-z0-9+/=]+)", url)
                        if match:
                            media = "image/jpeg" if match.group(1) == "jpeg" else "image/" + match.group(1)
                            blocks.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": match.group(2)}})
                out.append({"role": "user", "content": blocks or [{"type": "text", "text": "[Unsupported image attachment]"}]})
            else:
                out.append({"role": "user", "content": content})
        elif m["role"] == "assistant":
            b = ([{"type": "text", "text": m["content"]}] if m.get("content") else []) + [
                {"type": "tool_use", "id": t["id"], "name": t["function"]["name"], "input": json.loads(t["function"]["arguments"] or "{}")}
                for t in m.get("tool_calls", [])]
            out.append({"role": "assistant", "content": b})
        else:
            blk = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list): out[-1]["content"].append(blk)
            else: out.append({"role": "user", "content": [blk]})
    body = {"model": p["model"], "max_tokens": 8192, "system": system, "messages": out,
            "tools": [{"name": t["function"]["name"], "description": t["function"]["description"], "input_schema": t["function"]["parameters"]} for t in tools]}
    async with httpx.AsyncClient(timeout=300) as c:
        r = await c.post(p["base_url"].rstrip("/") + "/v1/messages", json=body,
                         headers={"x-api-key": p["key"], "anthropic-version": "2023-06-01"})
    if r.status_code >= 400: raise RuntimeError(f"anthropic {r.status_code}: {r.text[:300]}")
    blocks = r.json()["content"]
    text = "".join(b["text"] for b in blocks if b["type"] == "text")
    calls = [{"id": b["id"], "type": "function", "function": {"name": b["name"], "arguments": json.dumps(b["input"])}} for b in blocks if b["type"] == "tool_use"]
    if text: yield "text", text
    m = {"role": "assistant", "content": text or None}
    if calls: m["tool_calls"] = calls
    yield "message", m
