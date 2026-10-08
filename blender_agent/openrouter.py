"""OpenRouter client: model catalogue and streaming chat completions with tools.

No third-party SDK - uses ``requests`` which Blender already bundles, so the
addon stays dependency-free.
"""

import base64
import json
import os
import threading
import time

import bpy

CACHE_VERSION = 3
_lock = threading.RLock()
_models = []          # raw catalogue entries (normalised dicts)
_meta_by_id = {}      # id -> display meta

FALLBACK_MODELS = [
    {"id": "anthropic/claude-sonnet-4.5", "name": "Claude Sonnet 4.5 (fallback)",
     "context_length": 200000, "modalities": "text+image"},
    {"id": "openai/gpt-5", "name": "GPT-5 (fallback)",
     "context_length": 200000, "modalities": "text+image"},
    {"id": "google/gemini-2.5-pro", "name": "Gemini 2.5 Pro (fallback)",
     "context_length": 1000000, "modalities": "text+image"},
    {"id": "deepseek/deepseek-chat", "name": "DeepSeek Chat (fallback)",
     "context_length": 64000, "modalities": "text"},
]


def _cache_path():
    d = bpy.utils.user_resource("CONFIG", path="blender_agent", create=True)
    return os.path.join(d, "models.json")


def _cache_file():
    d = bpy.utils.user_resource("CONFIG", path="blender_agent", create=True)
    return os.path.join(d, "models_cache_v%d.json" % CACHE_VERSION)


def _price(v):
    try:
        return "%.2f" % (float(v) * 1_000_000)
    except (TypeError, ValueError):
        return "-"


def _normalise(entry):
    arch = entry.get("architecture") or {}
    mods = arch.get("input_modalities") or []
    ctx = entry.get("context_length") or entry.get("top_provider", {}).get("context_length")
    pricing = entry.get("pricing") or {}
    return {
        "id": entry.get("id", ""),
        "name": entry.get("name") or entry.get("id", ""),
        "context_length": ctx,
        "modalities": "+".join(mods) if mods else "text",
        "supports_tools": bool(entry.get("supported_parameters") and
                               "tools" in entry["supported_parameters"]) or True,
        "prompt_price": _price(pricing.get("prompt")),
        "completion_price": _price(pricing.get("completion")),
        "image_price": _price(pricing.get("image")),
        "description": (entry.get("description") or "")[:400],
    }


def models():
    with _lock:
        return list(_models)


def model_meta(model_id):
    with _lock:
        return _meta_by_id.get(model_id)


def _cache_enabled():
    """Tests set BLENDER_AGENT_NO_MODEL_CACHE so a mock catalogue never lands in
    the real user config directory."""
    return not os.environ.get("BLENDER_AGENT_NO_MODEL_CACHE")


def set_models(raw_entries):
    global _models, _meta_by_id
    norm = [_normalise(e) for e in raw_entries if e.get("id")]
    norm.sort(key=lambda m: m["name"].lower())
    with _lock:
        _models = norm
        _meta_by_id = {m["id"]: m for m in norm}
    if not _cache_enabled():
        return
    try:
        with open(_cache_file(), "w") as fh:
            json.dump(norm, fh)
    except OSError:
        pass


def load_cache():
    if models():
        return True
    try:
        with open(_cache_file()) as fh:
            cached = json.load(fh)
    except (OSError, ValueError):
        return False
    if cached:
        global _models, _meta_by_id
        with _lock:
            _models = cached
            _meta_by_id = {m["id"]: m for m in cached}
        return True
    return False


def fetch_models(api_key=None, base_url=None, timeout=30):
    """GET /models - public endpoint, no key required."""
    import requests

    base = (base_url or "https://openrouter.ai/api/v1").rstrip("/")
    headers = {"Accept": "application/json"}
    token = (api_key or "").strip()
    if token:
        headers["Authorization"] = "Bearer %s" % token
    r = requests.get(base + "/models", headers=headers, timeout=timeout)
    r.raise_for_status()
    payload = r.json()
    entries = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        raise RuntimeError("unexpected /models payload")
    set_models(entries)
    return models()


def model_enum_items(self, context):
    if not models():
        load_cache()
    items = [("custom", "Custom model id...", "Type a model id by hand")]
    for m in models():
        items.append((m["id"], m["name"], m["id"]))
    if len(items) == 1:
        for m in FALLBACK_MODELS:
            items.append((m["id"], m["name"], m["id"]))
    return items


class OpenRouterError(RuntimeError):
    pass


def build_client(prefs):
    base = (prefs.base_url or "https://openrouter.ai/api/v1").rstrip("/")
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "HTTP-Referer": prefs.http_referer or "https://github.com/jacoblbagent/blender-agent",
        "X-Title": prefs.app_title or "Blender Agent",
    }
    key = (prefs.api_key or "").strip()
    if key:
        headers["Authorization"] = "Bearer %s" % key
    return base, headers, key


def validate_key(prefs):
    """GET /key - returns (ok, message)."""
    import requests

    base, headers, key = build_client(prefs)
    if not key:
        return False, "No API key set"
    headers.pop("Accept", None)
    try:
        r = requests.get(base + "/key", headers=headers, timeout=20)
    except Exception as exc:  # noqa: BLE001 - surface any transport error
        return False, "Network error: %s" % exc
    if r.status_code >= 400:
        return False, "HTTP %s: %s" % (r.status_code, r.text[:300])
    data = r.json().get("data", {})
    label = data.get("label") or "key"
    usage = data.get("usage")
    limit = data.get("limit")
    extra = []
    if limit is not None:
        extra.append("limit=%s" % limit)
    if usage is not None:
        extra.append("usage=%s" % round(usage, 4))
    return True, "OK - %s %s" % (label, " ".join(extra))


def _retryable(exc, status=None):
    if status is not None and status >= 500:
        return True
    if status is not None:
        return False
    name = type(exc).__name__
    return name in ("ConnectionError", "Timeout", "ReadTimeout", "ChunkedEncodingError",
                    "SSLError", "ConnectTimeout")


def stream_chat(prefs, messages, tools=None, cancel_flag=None, abort_box=None):
    """Generator of events from a chat completion request.

    Yields dicts:
        {"type": "content",   "text": str}
        {"type": "reasoning", "text": str}
        {"type": "tool_calls","tool_calls": [...]}
        {"type": "done",      "finish_reason": str, "usage": {...}}
        {"type": "error",     "message": str}
    """
    import requests

    base, headers, key = build_client(prefs)
    if not key:
        yield {"type": "error", "message": "No OpenRouter API key set - open "
              "Edit > Preferences > Add-ons > Blender Agent."}
        return

    model = prefs.resolved_model()
    if not model:
        yield {"type": "error", "message": "No model selected."}
        return

    payload = {
        "model": model,
        "messages": messages,
        "temperature": prefs.temperature,
        "top_p": prefs.top_p,
        "stream": bool(prefs.stream),
    }
    if prefs.max_tokens:
        payload["max_tokens"] = prefs.max_tokens
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    url = base + "/chat/completions"
    attempts = 0
    while True:
        attempts += 1
        try:
            if payload["stream"]:
                yield from _stream_once(requests, url, headers, payload, prefs, cancel_flag,
                                        abort_box)
            else:
                yield from _blocking_once(requests, url, headers, payload, prefs)
            return
        except OpenRouterError as exc:
            yield {"type": "error", "message": str(exc)}
            return
        except Exception as exc:  # noqa: BLE001
            if attempts < 3 and _retryable(exc):
                time.sleep(1.5 * attempts)
                continue
            yield {"type": "error", "message": "%s: %s" % (type(exc).__name__, exc)}
            return


def _http_error(r):
    detail = ""
    try:
        j = r.json()
        err = j.get("error") or {}
        detail = err.get("message") or json.dumps(j)[:300]
    except ValueError:
        detail = (r.text or "")[:300]
    hint = ""
    if r.status_code == 401:
        hint = " (check the API key)"
    elif r.status_code == 402:
        hint = " (out of credits)"
    elif r.status_code == 404:
        hint = " (unknown model id?)"
    elif r.status_code == 429:
        hint = " (rate limited)"
    return OpenRouterError("HTTP %s%s: %s" % (r.status_code, hint, detail))


def _stream_once(requests, url, headers, payload, prefs, cancel_flag, abort_box=None):
    # Connection: close stops urllib3 from holding the socket open (a live SSE
    # stream is never fully drained, and reusing that connection would block).
    headers = dict(headers)
    headers["Connection"] = "close"
    r = requests.post(url, headers=headers, json=payload, stream=True,
                      timeout=(20, prefs.timeout))
    if abort_box is not None:
        abort_box["close"] = r.close
    try:
        if r.status_code >= 400:
            raise _http_error(r)
        tool_calls = {}
        finish = None
        usage = None
        for raw in r.iter_lines(decode_unicode=False):
            if cancel_flag is not None and cancel_flag.is_set():
                return
            if not raw:
                continue
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            if chunk.get("error"):
                raise OpenRouterError(str(chunk["error"])[:400])
            if chunk.get("usage"):
                usage = chunk["usage"]
            choices = chunk.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            finish = choice.get("finish_reason") or finish
            delta = choice.get("delta") or choice.get("message") or {}
            reasoning = delta.get("reasoning") or delta.get("reasoning_content")
            if reasoning:
                yield {"type": "reasoning", "text": reasoning}
            if delta.get("content"):
                yield {"type": "content", "text": delta["content"]}
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = tool_calls.setdefault(idx, {"id": "", "type": "function",
                                                   "function": {"name": "", "arguments": ""}})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]
        merged = [tool_calls[k] for k in sorted(tool_calls)]
        if merged:
            for slot in merged:
                slot["function"]["arguments"] = slot["function"]["arguments"] or "{}"
            yield {"type": "tool_calls", "tool_calls": merged}
        yield {"type": "done", "finish_reason": finish or "stop", "usage": usage}
    finally:
        if abort_box is not None:
            abort_box["close"] = None
        try:
            r.close()
        except Exception:  # noqa: BLE001
            pass


def _blocking_once(requests, url, headers, payload, prefs):
    body = dict(payload, stream=False)
    r = requests.post(url, headers=headers, json=body, timeout=(20, prefs.timeout))
    if r.status_code >= 400:
        raise _http_error(r)
    data = r.json()
    if data.get("error"):
        raise OpenRouterError(str(data["error"])[:400])
    choices = data.get("choices") or []
    if not choices:
        yield {"type": "done", "finish_reason": "stop", "usage": data.get("usage")}
        return
    msg = choices[0].get("message") or {}
    if msg.get("reasoning"):
        yield {"type": "reasoning", "text": msg["reasoning"]}
    if msg.get("content"):
        yield {"type": "content", "text": msg["content"]}
    if msg.get("tool_calls"):
        yield {"type": "tool_calls", "tool_calls": msg["tool_calls"]}
    yield {"type": "done", "finish_reason": choices[0].get("finish_reason", "stop"),
           "usage": data.get("usage")}


def image_part(path, max_bytes=4_500_000):
    """Encode a PNG/JPG as an OpenRouter image_url content part."""
    try:
        with open(path, "rb") as fh:
            blob = fh.read(max_bytes)
    except OSError as exc:
        return None
    ext = os.path.splitext(path)[1].lower().lstrip(".") or "png"
    mime = "image/jpeg" if ext in ("jpg", "jpeg") else "image/png"
    return {
        "type": "image_url",
        "image_url": {"url": "data:%s;base64,%s" % (mime, base64.b64encode(blob).decode())},
    }
