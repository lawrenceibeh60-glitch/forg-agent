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
    # Environment configuration must remain usable even when the app has already
    # created a providers.json file (for example, on an earlier boot without a key).
    if PF.exists():
        d = json.loads(PF.read_text())
    else:
        d = {"active": None, "providers": []}

    # Prefer current xKiro variable names; retain legacy KIRO_* compatibility.
    xkiro_key = os.getenv("XKIRO_API_KEY") or os.getenv("KIRO_API_KEY")
    providers = d.setdefault("providers", [])
    active_id = d.get("active")
    active_exists = any(p.get("id") == active_id for p in providers)

    # Seed xKiro from Render environment variables if it has not already been
    # saved in Settings. Do not override a valid provider the user selected.
    if xkiro_key and not any(p.get("id") == "xkiro" for p in providers):
        providers.append({
            "id": "xkiro", "name": "xKiro",
            "base_url": os.getenv("XKIRO_BASE_URL") or os.getenv("KIRO_BASE_URL") or "https://api.xkiro.com/v1",
            "kind": "openai", "key": xkiro_key,
            "model": os.getenv("XKIRO_MODEL") or os.getenv("KIRO_MODEL") or "mistralai/devstral-medium"
        })
        if not active_exists:
            d["active"] = "xkiro"
    elif not active_exists and xkiro_key:
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

async def stream(messages, tools):
    """Yields ('text', chunk) as it arrives, then ('message', assistant_message)."""
    p = active()
    for i in range(3):  # retry rate limits / 5xx before any output has streamed
        started = False
        try:
            async for x in (_anthropic if p["kind"] == "anthropic" else _openai)(p, messages, tools):
                started = True; yield x
            return
        except RuntimeError as e:
            if started or i == 2 or not re.search(r"\b(429|5\d\d)\b", str(e)): raise
            await asyncio.sleep(3 * 2 ** i)

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
        elif m["role"] == "user": out.append({"role": "user", "content": m["content"]})
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
