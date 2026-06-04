"""Local OpenAI-compatible "subscription gateway".

A ModelEndpoint can point its base_url at this gateway (served by
``routes/llm_oauth_routes.py``) with an opaque per-owner token as its api_key. The
gateway then lets the WHOLE app (chat, research, auto-title, even the Pi harness's
``odysseus`` provider) use a Claude / ChatGPT **subscription** through the normal
endpoint path — no changes to the dozens of ``llm_core`` call sites.

Facade: standard OpenAI Chat Completions (``/v1/chat/completions`` + ``/v1/models``),
because that is what the app's ``llm_core`` speaks to any endpoint. Internally it
translates to the provider's native API using the live OAuth token from
``src/coding_provider_oauth.py``:
  - Claude  -> Anthropic Messages (reuses ``llm_core``'s Anthropic translation, with
    ``Authorization: Bearer`` + ``anthropic-beta`` for subscription OAuth).
  - Codex   -> OpenAI **Responses** backend (``/responses``) with the Codex headers.

The gateway token is ``odyoauth:<encrypted(owner\x1fprovider)>`` (Fernet via
``secret_storage``) — stateless, no DB row, and only the server can mint it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator

import httpx

from src import secret_storage
from src.coding_provider_oauth import CODEX_ORIGINATOR, resolve_subscription_credential

logger = logging.getLogger(__name__)

_TOKEN_PREFIX = "odyoauth:"
_SEP = "\x1f"

# Curated fallback models per subscription provider (used when a live list can't be
# fetched). Anthropic exposes a live GET /v1/models, so its list is fetched dynamically;
# the Codex/ChatGPT backend has NO public model-list endpoint, so its set is curated and
# must be kept current (OpenAI rotates these — older ids like gpt-5/gpt-5-codex were
# retired from ChatGPT-signin Codex in April 2026).
_CURATED_MODELS: dict[str, list[str]] = {
    "claude": ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
    "codex": ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex", "gpt-5.3-codex-spark", "gpt-5.2"],
}


# --- gateway capability token ----------------------------------------------

def mint_gateway_token(owner: str | None, provider: str) -> str:
    return _TOKEN_PREFIX + secret_storage.encrypt(f"{owner or ''}{_SEP}{provider}")


def parse_gateway_token(token: str | None) -> tuple[str, str] | None:
    raw = (token or "").strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    if not raw.startswith(_TOKEN_PREFIX):
        return None
    try:
        decoded = secret_storage.decrypt(raw[len(_TOKEN_PREFIX):])
    except Exception:
        return None
    if _SEP not in decoded:
        return None
    owner, provider = decoded.split(_SEP, 1)
    return owner, provider


def curated_models(provider: str) -> list[str]:
    return list(_CURATED_MODELS.get(provider, []))


def _models_payload(provider: str, ids: list[str]) -> dict[str, Any]:
    return {
        "object": "list",
        "data": [{"id": m, "object": "model", "owned_by": provider} for m in ids],
    }


def models_response(provider: str) -> dict[str, Any]:
    """Synchronous curated list (kept for callers/tests that don't need live data)."""
    return _models_payload(provider, curated_models(provider))


def models_response_cached(owner: str | None, provider: str) -> dict[str, Any]:
    """Instant OpenAI /v1/models payload from cache/curated (no network)."""
    return _models_payload(provider, cached_or_curated(owner, provider))


# Short in-process cache of live model lists, keyed by (owner, provider).
# CRITICAL: the endpoint reachability probe hits /v1/models on a ~1s timeout, so the
# route MUST answer instantly from this cache/curated and NEVER block on a network call
# (a live Anthropic fetch or a Codex token refresh easily exceeds 1s -> the endpoint
# gets wrongly marked offline). The live list is refreshed in the BACKGROUND.
_MODELS_CACHE: dict[tuple, tuple[float, list[str]]] = {}
_MODELS_CACHE_TTL = 300
_REFRESH_INFLIGHT: set[tuple] = set()


def cached_or_curated(owner: str | None, provider: str) -> list[str]:
    """Instant, no-network: fresh cached live list if we have one, else curated."""
    cached = _MODELS_CACHE.get((owner or "", provider))
    if cached and (_now() - cached[0]) < _MODELS_CACHE_TTL:
        return list(cached[1])
    return curated_models(provider)


def schedule_models_refresh(owner: str | None, provider: str) -> None:
    """Fire-and-forget background refresh of the live model list (no-op if cache fresh
    or no running loop). Keeps /v1/models instant while still picking up live models."""
    key = (owner or "", provider)
    cached = _MODELS_CACHE.get(key)
    if cached and (_now() - cached[0]) < _MODELS_CACHE_TTL:
        return
    if key in _REFRESH_INFLIGHT:
        return
    try:
        import asyncio
        asyncio.get_running_loop().create_task(_refresh_models(owner, provider))
    except RuntimeError:
        pass  # no event loop (e.g. sync caller) — skip; creation path uses list_models()


async def _refresh_models(owner: str | None, provider: str) -> None:
    key = (owner or "", provider)
    _REFRESH_INFLIGHT.add(key)
    try:
        ids = await _fetch_live_models(owner, provider)
        if ids:
            _MODELS_CACHE[key] = (_now(), list(ids))
    except Exception:
        logger.debug("background model refresh failed for %s/%s", owner, provider, exc_info=True)
    finally:
        _REFRESH_INFLIGHT.discard(key)


async def _fetch_live_models(owner: str | None, provider: str) -> list[str]:
    """Blocking live fetch (makes network calls). Anthropic has a live /v1/models;
    Codex/ChatGPT has no list endpoint, so curated. Falls back to curated on error."""
    cred = await resolve_subscription_credential(owner, provider)
    if not cred:
        return curated_models(provider)
    if cred["provider"] == "anthropic":
        try:
            headers = {"Authorization": f"Bearer {cred['access_token']}", "anthropic-version": "2023-06-01"}
            headers.update(cred.get("headers") or {})
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(f"{cred['base_url']}/v1/models?limit=1000", headers=headers)
            if resp.status_code == 200:
                ids = [m.get("id") for m in (resp.json().get("data") or [])
                       if isinstance(m, dict) and m.get("id")]
                if ids:
                    return ids
        except Exception:
            logger.debug("anthropic /v1/models fetch failed; using curated", exc_info=True)
    return curated_models(provider)


async def list_models(owner: str | None, provider: str) -> list[str]:
    """BLOCKING list (cached or live). Use at endpoint CREATION where a brief network
    wait is fine and we want the full current list stored. Routes must use
    ``cached_or_curated`` + ``schedule_models_refresh`` instead (probe is time-tight)."""
    cached = _MODELS_CACHE.get((owner or "", provider))
    if cached and (_now() - cached[0]) < _MODELS_CACHE_TTL:
        return list(cached[1])
    ids = await _fetch_live_models(owner, provider)
    result = ids or curated_models(provider)
    _MODELS_CACHE[(owner or "", provider)] = (_now(), list(result))
    return result


async def models_response_live(owner: str | None, provider: str) -> dict[str, Any]:
    return _models_payload(provider, await list_models(owner, provider))


# --- OpenAI Chat Completions shaping ---------------------------------------

def _now() -> int:
    return int(time.time())


def _completion_json(model: str, text: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-ody-oauth",
        "object": "chat.completion",
        "created": _now(),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
    }


def _chunk(model: str, content: str | None, finish: str | None = None) -> str:
    delta = {"content": content} if content else {}
    obj = {
        "id": "chatcmpl-ody-oauth",
        "object": "chat.completion.chunk",
        "created": _now(),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(obj)}\n\n"


def _flatten_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                parts.append(c.get("text") or c.get("content") or "")
            elif isinstance(c, str):
                parts.append(c)
        return "".join(parts)
    return "" if content is None else str(content)


# --- OpenAI Responses (Codex/ChatGPT subscription) translation -------------

def _to_responses_payload(body: dict[str, Any], stream: bool) -> dict[str, Any]:
    messages = body.get("messages") or []
    instructions: list[str] = []
    input_items: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role") or "user"
        # Tool-call round-trips: assistant tool_calls -> function_call items; the matching
        # tool results (role="tool") -> function_call_output items. Without these the model
        # can't see its own past tool use and re-calls tools in a loop.
        if role == "assistant" and isinstance(m.get("tool_calls"), list):
            text = _flatten_content(m.get("content"))
            if text:
                input_items.append({"role": "assistant", "content": [{"type": "output_text", "text": text}]})
            for tc in m["tool_calls"]:
                fn = tc.get("function") or {}
                input_items.append({
                    "type": "function_call",
                    "call_id": tc.get("id") or "",
                    "name": fn.get("name") or "",
                    "arguments": fn.get("arguments") or "{}",
                })
            continue
        if role == "tool":
            input_items.append({
                "type": "function_call_output",
                "call_id": m.get("tool_call_id") or "",
                "output": _flatten_content(m.get("content")),
            })
            continue
        text = _flatten_content(m.get("content"))
        if role == "system":
            instructions.append(text)
            continue
        ctype = "output_text" if role == "assistant" else "input_text"
        input_items.append({"role": role, "content": [{"type": ctype, "text": text}]})
    payload: dict[str, Any] = {
        "model": body.get("model"),
        "input": input_items,
        "store": False,
        "stream": bool(stream),
    }
    if instructions:
        payload["instructions"] = "\n\n".join(i for i in instructions if i)
    # The Responses API flattens function tool defs to the top level (no nested "function").
    tools = _responses_tools(body.get("tools"))
    if tools:
        payload["tools"] = tools
        if body.get("tool_choice") is not None:
            payload["tool_choice"] = body["tool_choice"]
    return payload


def _responses_tools(tools: Any) -> list[dict[str, Any]]:
    """Convert OpenAI chat-completions tool defs to Responses function tools."""
    out: list[dict[str, Any]] = []
    for t in tools or []:
        if isinstance(t, dict) and t.get("type") == "function" and isinstance(t.get("function"), dict):
            fn = t["function"]
            out.append({
                "type": "function",
                "name": fn.get("name"),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
            })
    return out


def _responses_text(data: dict[str, Any]) -> str:
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    texts: list[str] = []
    for item in (data.get("output") or []):
        if not isinstance(item, dict):
            continue
        for c in (item.get("content") or []):
            if isinstance(c, dict) and c.get("type") in ("output_text", "text") and isinstance(c.get("text"), str):
                texts.append(c["text"])
    return "".join(texts)


def _responses_headers(cred: dict[str, Any]) -> dict[str, str]:
    h = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cred['access_token']}",
        "originator": CODEX_ORIGINATOR,
    }
    h.update(cred.get("headers") or {})
    return h


# --- in-process subscription core (used by llm_core AND the HTTP route) -----
# These are the single source of truth for calling a Claude/Codex subscription. The
# app's LLM layer (src/llm_core.py) calls them IN-PROCESS for the `odyoauth://` provider
# — no loopback HTTP, no dynamic port. The HTTP route below is a thin OpenAI-compatible
# wrapper kept for any external caller.

async def subscription_complete(owner: str | None, provider: str, body: dict[str, Any]) -> str:
    """Non-streaming: return the assistant TEXT from the owner's subscription, DIRECT to
    the provider (Pi-style, no CLI wrap):
      - **claude** → Anthropic Messages API with the Claude Code identity + OAuth bearer
        (token read from ``claude auth login``'s credentials file + refreshed).
      - **codex** → the ChatGPT/Codex Responses backend with the OAuth bearer."""
    cred = await resolve_subscription_credential(owner, provider)
    if not cred:
        raise GatewayError(401, f"Not logged in to {provider} subscription")
    model = body.get("model") or (curated_models(provider) or [""])[0]
    temperature = body.get("temperature", 0.7)
    max_tokens = body.get("max_tokens") or body.get("max_completion_tokens") or 4096

    if cred["provider"] == "anthropic":
        payload = _anthropic_payload({**body, "model": model}, max_tokens, temperature, stream=False)
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(f"{cred['base_url']}/v1/messages", headers=_anthropic_headers(cred), json=payload)
        if resp.status_code != 200:
            raise GatewayError(502, f"Anthropic messages {resp.status_code}: {resp.text[:300]}")
        return _anthropic_text(resp.json())

    payload = _to_responses_payload({**body, "model": model}, stream=False)
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(f"{cred['base_url']}/responses", headers=_responses_headers(cred), json=payload)
    if resp.status_code != 200:
        raise GatewayError(502, f"Codex responses {resp.status_code}: {resp.text[:300]}")
    return _responses_text(resp.json())


async def subscription_stream_text(owner: str | None, provider: str, body: dict[str, Any]) -> AsyncIterator[str]:
    """Streaming TEXT deltas (no tool calls) — used by the OpenAI-compatible HTTP facade."""
    async for evt in subscription_stream_events(owner, provider, body):
        if evt.get("type") == "text" and evt.get("text"):
            yield evt["text"]
        elif evt.get("type") == "error":
            yield f"[odysseus] {provider} {evt.get('status', '')}: {evt.get('error', '')}"


async def subscription_stream_events(owner: str | None, provider: str, body: dict[str, Any],
                                     cred: dict[str, Any] | None = None) -> AsyncIterator[dict[str, Any]]:
    """Streaming with TOOL support, DIRECT to the provider (no CLI wrap). Yields structured
    events for the agent loop:
      {"type": "text", "text": str}
      {"type": "tool_calls", "calls": [{"id","name","arguments"}]}
      {"type": "error", "status": int, "error": str}
    Claude is normally streamed via llm_core's native Anthropic path (full tool parity);
    this Anthropic branch is a text-only safety net. Codex (ChatGPT Responses backend) is
    streamed HERE, translating Responses SSE — including function calls — into the events.
    Pass a pre-resolved ``cred`` to avoid re-reading credentials when the caller already has
    one (llm_core does)."""
    if cred is None:
        cred = await resolve_subscription_credential(owner, provider)
    model = body.get("model") or (curated_models(provider) or [""])[0]
    if not cred:
        yield {"type": "error", "status": 401, "error": f"not logged in to {provider} subscription"}
        return
    if cred["provider"] == "anthropic":
        async for delta in _anthropic_stream_text(cred, model, body):
            yield {"type": "text", "text": delta}
        return
    async for evt in _codex_stream_events(cred, model, body):
        yield evt


async def _codex_stream_events(cred: dict[str, Any], model: str, body: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    """Stream the Codex Responses backend SSE; yield text + accumulated function calls."""
    payload = _to_responses_payload({**body, "model": model}, stream=True)
    # item_id -> {call_id, name, arguments}
    fcalls: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            async with client.stream("POST", f"{cred['base_url']}/responses",
                                     headers=_responses_headers(cred), json=payload) as resp:
                if resp.status_code != 200:
                    text = await resp.aread()
                    yield {"type": "error", "status": resp.status_code,
                           "error": text.decode(errors="replace")[:300]}
                    return
                async for raw in resp.aiter_lines():
                    raw = (raw or "").strip()
                    if not raw.startswith("data:"):
                        continue
                    data = raw[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        obj = json.loads(data)
                    except Exception:
                        continue
                    etype = obj.get("type") or ""
                    if etype == "response.output_text.delta":
                        delta = obj.get("delta")
                        if isinstance(delta, str) and delta:
                            yield {"type": "text", "text": delta}
                    elif etype == "response.output_item.added":
                        item = obj.get("item") or {}
                        if item.get("type") == "function_call":
                            iid = item.get("id") or f"item_{len(order)}"
                            if iid not in fcalls:
                                order.append(iid)
                            fcalls[iid] = {
                                "call_id": item.get("call_id") or item.get("id") or "",
                                "name": item.get("name") or "",
                                "arguments": item.get("arguments") or "",
                            }
                    elif etype == "response.function_call_arguments.delta":
                        iid = obj.get("item_id")
                        if iid in fcalls and isinstance(obj.get("delta"), str):
                            fcalls[iid]["arguments"] += obj["delta"]
                    elif etype == "response.function_call_arguments.done":
                        iid = obj.get("item_id")
                        if iid in fcalls and isinstance(obj.get("arguments"), str) and obj["arguments"]:
                            fcalls[iid]["arguments"] = obj["arguments"]
                    elif etype in ("response.completed", "response.incomplete"):
                        break
                    elif etype in ("error", "response.failed"):
                        yield {"type": "error", "status": 502, "error": json.dumps(obj)[:300]}
                        return
    except Exception as exc:
        logger.debug("codex subscription stream failed", exc_info=True)
        yield {"type": "error", "status": 502, "error": str(exc)}
        return
    if fcalls:
        calls = []
        for i, iid in enumerate(order):
            tb = fcalls[iid]
            calls.append({"id": tb["call_id"] or f"call_{i}", "name": tb["name"], "arguments": tb["arguments"] or "{}"})
        yield {"type": "tool_calls", "calls": calls}


# --- HTTP route wrappers (OpenAI-compatible facade) ------------------------

async def chat_completion(owner: str | None, provider: str, body: dict[str, Any]) -> dict[str, Any]:
    model = body.get("model") or (curated_models(provider) or [""])[0]
    return _completion_json(model, await subscription_complete(owner, provider, body))


async def chat_completion_stream(owner: str | None, provider: str, body: dict[str, Any]) -> AsyncIterator[str]:
    model = body.get("model") or (curated_models(provider) or [""])[0]
    async for delta in subscription_stream_text(owner, provider, body):
        yield _chunk(model, delta)
    yield _chunk(model, None, finish="stop")
    yield "data: [DONE]\n\n"


# Claude Code subscription OAuth tokens are scoped to Claude Code: Anthropic rejects
# them (401) unless the request presents the Claude Code identity as the FIRST system
# block. This exact string is what the CLI sends; it's required for the token to work
# against /v1/messages (see Meridian / claude-max-api-proxy and litellm's claude-code
# tutorial). This is an UNOFFICIAL use of the subscription and may break.
CLAUDE_CODE_SYSTEM = "You are Claude Code, Anthropic's official CLI for Claude."


def _anthropic_headers(cred: dict[str, Any]) -> dict[str, str]:
    h = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cred['access_token']}",
        "anthropic-version": "2023-06-01",
    }
    h.update(cred.get("headers") or {})  # carries anthropic-beta (oauth-2025-04-20,...)
    return h


def _anthropic_payload(body: dict[str, Any], max_tokens: int, temperature: float, stream: bool) -> dict[str, Any]:
    """Translate an OpenAI chat-completions body to an Anthropic Messages request,
    leading the `system` array with the Claude Code identity so the subscription OAuth
    token is accepted."""
    user_system: list[str] = []
    convo: list[dict[str, Any]] = []
    for m in body.get("messages") or []:
        role = m.get("role") or "user"
        text = _flatten_content(m.get("content"))
        if role == "system":
            if text:
                user_system.append(text)
        else:
            convo.append({"role": "assistant" if role == "assistant" else "user", "content": text})
    system_blocks = [{"type": "text", "text": CLAUDE_CODE_SYSTEM}]
    if user_system:
        system_blocks.append({"type": "text", "text": "\n\n".join(user_system)})
    payload: dict[str, Any] = {
        "model": body.get("model"),
        "max_tokens": int(max_tokens) if max_tokens else 4096,
        "system": system_blocks,
        "messages": convo,
        "stream": bool(stream),
    }
    if temperature is not None:
        payload["temperature"] = temperature
    return payload


def _anthropic_text(data: dict[str, Any]) -> str:
    parts = []
    for block in (data.get("content") or []):
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts)


async def _anthropic_stream_text(cred: dict[str, Any], model: str, body: dict[str, Any]) -> AsyncIterator[str]:
    """Stream Anthropic Messages SSE directly; yield assistant TEXT deltas."""
    temperature = body.get("temperature", 0.7)
    max_tokens = body.get("max_tokens") or body.get("max_completion_tokens") or 4096
    payload = _anthropic_payload({**body, "model": model}, max_tokens, temperature, stream=True)
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            async with client.stream("POST", f"{cred['base_url']}/v1/messages",
                                     headers=_anthropic_headers(cred), json=payload) as resp:
                if resp.status_code != 200:
                    text = await resp.aread()
                    yield f"[odysseus] anthropic {resp.status_code}: {text.decode(errors='replace')[:300]}"
                    return
                async for raw in resp.aiter_lines():
                    raw = (raw or "").strip()
                    if not raw.startswith("data:"):
                        continue
                    data = raw[5:].strip()
                    if not data:
                        continue
                    try:
                        obj = json.loads(data)
                    except Exception:
                        continue
                    etype = obj.get("type")
                    if etype == "content_block_delta":
                        delta = obj.get("delta") or {}
                        if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                            yield delta["text"]
                    elif etype == "message_stop":
                        break
    except Exception as exc:
        logger.debug("anthropic subscription stream failed", exc_info=True)
        yield f"\n[odysseus] stream error: {exc}"


class GatewayError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
