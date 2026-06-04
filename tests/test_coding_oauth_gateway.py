"""Tests for the app-wide subscription OAuth path: the credential resolver, the
OpenAI-compatible gateway translation, and the gateway-backed ModelEndpoint."""

from __future__ import annotations

import asyncio
import base64
import json
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, ModelEndpoint
from src import coding_oauth_gateway as gw
from src import coding_provider_oauth as oauth


@pytest.fixture(autouse=True)
def _clear_models_cache():
    gw._MODELS_CACHE.clear()
    yield
    gw._MODELS_CACHE.clear()


# --------------------------------------------------------------------------- #
# Gateway capability token
# --------------------------------------------------------------------------- #

def test_gateway_token_roundtrip():
    tok = gw.mint_gateway_token("alice@example.com", "codex")
    assert tok.startswith("odyoauth:")
    assert gw.parse_gateway_token(tok) == ("alice@example.com", "codex")
    # Tolerates a Bearer prefix (how it arrives in the Authorization header).
    assert gw.parse_gateway_token("Bearer " + tok) == ("alice@example.com", "codex")


def test_gateway_token_rejects_garbage():
    assert gw.parse_gateway_token("") is None
    assert gw.parse_gateway_token("Bearer not-a-token") is None
    assert gw.parse_gateway_token("odyoauth:deadbeef") is None  # not valid ciphertext


def test_curated_models_and_models_response():
    assert "claude-opus-4-8" in gw.curated_models("claude")
    # Current ChatGPT-signin Codex set (gpt-5/gpt-5-codex were retired April 2026).
    assert "gpt-5.3-codex" in gw.curated_models("codex")
    assert "gpt-5-codex" not in gw.curated_models("codex")
    resp = gw.models_response("codex")
    assert resp["object"] == "list"
    assert {m["id"] for m in resp["data"]} == set(gw.curated_models("codex"))


def test_anthropic_beta_includes_both_flags():
    # Claude Code subscription OAuth needs both beta flags on every request.
    assert "claude-code-20250219" in oauth.ANTHROPIC_OAUTH_BETA
    assert "oauth-2025-04-20" in oauth.ANTHROPIC_OAUTH_BETA


# --------------------------------------------------------------------------- #
# Direct-to-provider (odyoauth://) wiring — the in-process path
# --------------------------------------------------------------------------- #

def test_odyoauth_detected_as_subscription_provider():
    from src.llm_core import _detect_provider
    assert _detect_provider("odyoauth://claude") == "odysseus_oauth"
    assert _detect_provider("odyoauth://codex") == "odysseus_oauth"
    assert _detect_provider("https://api.anthropic.com") == "anthropic"


def test_endpoint_resolver_handles_odyoauth():
    from src.endpoint_resolver import build_chat_url, build_models_url, build_headers, resolve_url, normalize_base
    base = "odyoauth://claude"
    # Opaque address passes through every URL transform untouched (no HTTP shape).
    assert resolve_url(base) == base
    assert normalize_base(base) == base
    assert build_chat_url(base) == base
    assert build_models_url(base) == base
    # The api_key (capability token) becomes the bearer llm_core parses.
    assert build_headers("odyoauth:abc", base) == {"Authorization": "Bearer odyoauth:abc"}


def test_llm_core_routes_odyoauth_to_inprocess(monkeypatch):
    # Non-streaming completions (titles, summaries — no tools) route in-process to the
    # subscription. Streaming (the agent loop, WITH tools) has its own native paths below.
    import asyncio as _asyncio
    from src import coding_oauth_gateway as gwmod
    from src.endpoint_resolver import build_headers
    from src.llm_core import llm_call_async

    captured = {}
    async def _fake_complete(owner, provider, body):
        captured.update(owner=owner, provider=provider, model=body.get("model"))
        return "subscription answer"
    monkeypatch.setattr(gwmod, "subscription_complete", _fake_complete)

    token = gwmod.mint_gateway_token("tester", "claude")
    headers = build_headers(token, "odyoauth://claude")
    out = _asyncio.run(llm_call_async("odyoauth://claude", "claude-opus-4-8", [{"role": "user", "content": "hi"}], headers=headers))
    assert out == "subscription answer"
    assert captured == {"owner": "tester", "provider": "claude", "model": "claude-opus-4-8"}


def _fake_sse_stream(monkeypatch, lines):
    """Patch llm_core's http client so a streamed POST replays `lines` as SSE."""
    from src import llm_core
    class _Stream:
        status_code = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def aiter_lines(self):
            for ln in lines:
                yield ln
        async def aread(self): return b""
    class _Client:
        def stream(self, method, url, **kw):
            _Client.sent = {"url": url, **kw}
            return _Stream()
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: _Client())
    return _Client


def test_stream_llm_oauth_claude_native_with_tools(monkeypatch):
    # The crux: an OAuth Claude stream must go through llm_core's NATIVE Anthropic path so
    # the agent gets real tool_calls — AND the request must carry the tools, the OAuth
    # Bearer, and the Claude Code identity as system[0].
    import asyncio as _asyncio
    from src import llm_core
    from src.endpoint_resolver import build_headers
    from src import coding_oauth_gateway as gwmod

    async def _cred(owner, provider):
        return {"provider": "anthropic", "base_url": "https://api.anthropic.com",
                "access_token": "sk-ant-oat01-REAL", "headers": {"anthropic-beta": "claude-code-20250219,oauth-2025-04-20"}}
    monkeypatch.setattr("src.coding_provider_oauth.resolve_subscription_credential", _cred)
    sse = [
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text"}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Let me look"}}',
        'data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"toolu_1","name":"manage_coding"}}',
        'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"action\\":\\"list\\"}"}}',
        'data: {"type":"message_stop"}',
    ]
    Client = _fake_sse_stream(monkeypatch, sse)

    token = gwmod.mint_gateway_token("tester", "claude")
    headers = build_headers(token, "odyoauth://claude")
    tools = [{"type": "function", "function": {"name": "manage_coding", "description": "drive Code Station", "parameters": {"type": "object", "properties": {}}}}]

    async def _collect():
        out = []
        async for c in llm_core.stream_llm("odyoauth://claude", "claude-opus-4-8",
                                           [{"role": "system", "content": "ODY SYSTEM"}, {"role": "user", "content": "look in coding"}],
                                           headers=headers, tools=tools):
            out.append(c)
        return out
    chunks = _asyncio.run(_collect())
    body = Client.sent["json"]
    # tools forwarded (OpenAI -> Anthropic), Claude Code identity leads system, OAuth bearer kept.
    assert body["tools"][0]["name"] == "manage_coding"
    assert body["system"][0]["text"] == gwmod.CLAUDE_CODE_SYSTEM
    assert body["system"][1]["text"] == "ODY SYSTEM"
    assert Client.sent["headers"]["Authorization"] == "Bearer sk-ant-oat01-REAL"
    assert Client.sent["headers"]["anthropic-beta"] == "claude-code-20250219,oauth-2025-04-20"
    assert Client.sent["url"].endswith("/v1/messages")
    # The agent receives a native tool_calls event.
    tool_evt = next(json.loads(c[5:].strip()) for c in chunks if c.startswith("data:") and '"tool_calls"' in c)
    assert tool_evt["calls"][0]["name"] == "manage_coding"
    assert json.loads(tool_evt["calls"][0]["arguments"]) == {"action": "list"}


def test_stream_llm_oauth_codex_with_tools(monkeypatch):
    # Codex streams via the gateway's Responses translation; its function calls become the
    # app's native tool_calls SSE, and the request carries the converted tools.
    import asyncio as _asyncio
    from src import llm_core, coding_oauth_gateway as gwmod
    from src.endpoint_resolver import build_headers

    async def _cred(owner, provider):
        return {"provider": "openai", "base_url": oauth.CODEX_RESPONSES_BASE,
                "access_token": "tok", "headers": {"chatgpt-account-id": "acct_1"}}
    # stream_llm resolves via coding_provider_oauth, then threads the cred into the gateway.
    monkeypatch.setattr("src.coding_provider_oauth.resolve_subscription_credential", _cred)
    sse = [
        'data: {"type":"response.output_text.delta","delta":"Checking"}',
        'data: {"type":"response.output_item.added","item":{"type":"function_call","id":"item1","call_id":"call_a","name":"manage_coding","arguments":""}}',
        'data: {"type":"response.function_call_arguments.delta","item_id":"item1","delta":"{\\"action\\":"}',
        'data: {"type":"response.function_call_arguments.done","item_id":"item1","arguments":"{\\"action\\":\\"list\\"}"}',
        'data: {"type":"response.completed"}',
    ]
    sent = {}
    class _Stream:
        status_code = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def aiter_lines(self):
            for ln in sse:
                yield ln
        async def aread(self): return b""
    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def stream(self, method, url, **kw):
            sent.update(url=url, **kw)
            return _Stream()
    monkeypatch.setattr(gwmod.httpx, "AsyncClient", _Client)

    token = gwmod.mint_gateway_token("tester", "codex")
    headers = build_headers(token, "odyoauth://codex")
    tools = [{"type": "function", "function": {"name": "manage_coding", "description": "x", "parameters": {"type": "object", "properties": {}}}}]

    async def _collect():
        out = []
        async for c in llm_core.stream_llm("odyoauth://codex", "gpt-5.5",
                                           [{"role": "user", "content": "look in coding"}], headers=headers, tools=tools):
            out.append(c)
        return out
    chunks = _asyncio.run(_collect())
    assert sent["json"]["tools"][0]["name"] == "manage_coding"  # converted to Responses tool
    assert sent["url"].endswith("/responses")
    deltas = [json.loads(c[5:].strip()).get("delta") for c in chunks if c.startswith("data:") and '"delta"' in c]
    assert "Checking" in deltas
    tool_evt = next(json.loads(c[5:].strip()) for c in chunks if c.startswith("data:") and '"tool_calls"' in c)
    assert tool_evt["calls"][0]["name"] == "manage_coding"
    assert tool_evt["calls"][0]["id"] == "call_a"
    assert json.loads(tool_evt["calls"][0]["arguments"]) == {"action": "list"}


def test_to_responses_payload_forwards_tools_and_tool_calls():
    # Tool defs convert to flat Responses functions; assistant tool_calls + tool results
    # become function_call / function_call_output items so multi-turn tool use works.
    body = {
        "model": "gpt-5.5",
        "tools": [{"type": "function", "function": {"name": "f", "description": "d", "parameters": {"type": "object"}}}],
        "tool_choice": "auto",
        "messages": [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "result-text"},
        ],
    }
    p = gw._to_responses_payload(body, stream=True)
    assert p["tools"] == [{"type": "function", "name": "f", "description": "d", "parameters": {"type": "object"}}]
    assert p["tool_choice"] == "auto"
    fc = [i for i in p["input"] if i.get("type") == "function_call"]
    fo = [i for i in p["input"] if i.get("type") == "function_call_output"]
    assert fc and fc[0]["call_id"] == "c1" and fc[0]["name"] == "f"
    assert fo and fo[0]["call_id"] == "c1" and fo[0]["output"] == "result-text"


def test_sync_llm_call_routes_odyoauth(monkeypatch):
    # The synchronous llm_call (session titles, vision) must also route subscriptions.
    from src import coding_oauth_gateway as gwmod
    from src.endpoint_resolver import build_headers
    from src.llm_core import llm_call
    async def _fake_complete(owner, provider, body):
        return "sync sub answer"
    monkeypatch.setattr(gwmod, "subscription_complete", _fake_complete)
    token = gwmod.mint_gateway_token("tester", "codex")
    headers = build_headers(token, "odyoauth://codex")
    out = llm_call("odyoauth://codex", "gpt-5.5", [{"role": "user", "content": "hi"}], headers=headers)
    assert out == "sync sub answer"


def test_probe_endpoint_odyoauth_returns_curated_no_network():
    from routes.model_routes import _probe_endpoint, _ping_endpoint
    assert _probe_endpoint("odyoauth://codex") == gw.curated_models("codex")
    assert _ping_endpoint("odyoauth://claude")["reachable"] is True


def test_gateway_url_not_detected_as_ollama():
    # The gateway's /api/llm-oauth path must NOT be mistaken for native Ollama (which
    # would force the Ollama request format and send chat to .../chat instead of
    # .../chat/completions — the bug that made every subscription chat fall back).
    from src.llm_core import _detect_provider, _is_ollama_native_url
    gw_url = "http://127.0.0.1:58874/api/llm-oauth/v1"
    assert _is_ollama_native_url(gw_url) is False
    assert _detect_provider(gw_url) == "openai"
    # A genuine local Ollama is still detected.
    assert _is_ollama_native_url("http://127.0.0.1:11434/api") is True


def test_gateway_url_rewritten_to_live_port(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_PORT", "7860")
    from src.endpoint_resolver import build_chat_url, build_models_url, resolve_url
    stale = "http://127.0.0.1:58874/api/llm-oauth/v1"  # dead port from a previous launch
    # resolve_url heals the stale port to the live one...
    assert resolve_url(stale) == "http://127.0.0.1:7860/api/llm-oauth/v1"
    # ...and the chat URL is OpenAI-compatible /chat/completions (NOT ollama /chat).
    assert build_chat_url(stale) == "http://127.0.0.1:7860/api/llm-oauth/v1/chat/completions"
    assert build_models_url(stale) == "http://127.0.0.1:7860/api/llm-oauth/v1/models"


def test_orphan_guard_matches_gateway_across_port_changes(monkeypatch):
    # The "Selected model endpoint was removed" guard must match a subscription-gateway
    # session even when the stored ports differ from a previous launch.
    monkeypatch.setenv("ODYSSEUS_PORT", "49639")
    from routes.chat_routes import _session_url_matches_endpoint as match
    assert match("http://127.0.0.1:58874/api/llm-oauth/v1/chat/completions",
                 "http://127.0.0.1:60185/api/llm-oauth/v1") is True
    # Normal endpoints keep exact matching.
    assert match("http://127.0.0.1:1337/v1/chat/completions", "http://127.0.0.1:1337/v1") is True
    assert match("http://127.0.0.1:1337/v1", "http://127.0.0.1:9999/v1") is False


def test_gateway_endpoint_classified_as_api_not_local():
    # The loopback gateway must NOT be treated as a local model server (else it lands
    # in the "Local" section and the local-reachability probe wrongly marks it offline).
    from routes.model_routes import _classify_endpoint
    assert _classify_endpoint("http://127.0.0.1:7860/api/llm-oauth/v1") == "api"
    assert _classify_endpoint("http://127.0.0.1:1337/v1") == "local"   # a real local server
    assert _classify_endpoint("https://api.anthropic.com") == "api"


def test_gateway_base_url_uses_request_port_not_airplay_7000(monkeypatch):
    from routes import llm_oauth_routes as r
    monkeypatch.delenv("ODYSSEUS_PORT", raising=False)
    monkeypatch.delenv("APP_PORT", raising=False)

    class _U:
        port = 7860
    class _Req:
        url = _U()
    # Prefer the live request's port (the port the backend actually bound to).
    assert "127.0.0.1:7860/api/llm-oauth/v1" in r.gateway_base_url(_Req())
    # Never default to 7000 — macOS AirPlay holds it.
    assert ":7000" not in r.gateway_base_url(None)
    # Honors ODYSSEUS_PORT when no request.
    monkeypatch.setenv("ODYSSEUS_PORT", "7001")
    assert "127.0.0.1:7001/api/llm-oauth/v1" in r.gateway_base_url(None)


# --------------------------------------------------------------------------- #
# OpenAI Responses translation
# --------------------------------------------------------------------------- #

def test_to_responses_payload():
    body = {
        "model": "gpt-5-codex",
        "messages": [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ],
        "stream": False,
    }
    p = gw._to_responses_payload(body, stream=False)
    assert p["model"] == "gpt-5-codex"
    assert p["store"] is False
    assert p["instructions"] == "be terse"
    # system goes to instructions; user -> input_text, assistant -> output_text
    assert p["input"][0] == {"role": "user", "content": [{"type": "input_text", "text": "hello"}]}
    assert p["input"][1] == {"role": "assistant", "content": [{"type": "output_text", "text": "hi"}]}


def test_responses_text_parsing():
    assert gw._responses_text({"output_text": "fast path"}) == "fast path"
    data = {"output": [{"type": "message", "content": [
        {"type": "output_text", "text": "a"}, {"type": "output_text", "text": "b"}]}]}
    assert gw._responses_text(data) == "ab"


def test_openai_chunk_and_completion_shape():
    chunk = gw._chunk("m", "hi")
    assert chunk.startswith("data: ") and chunk.endswith("\n\n")
    obj = json.loads(chunk[6:].strip())
    assert obj["object"] == "chat.completion.chunk"
    assert obj["choices"][0]["delta"]["content"] == "hi"
    comp = gw._completion_json("m", "answer")
    assert comp["choices"][0]["message"]["content"] == "answer"
    assert comp["choices"][0]["finish_reason"] == "stop"


# --------------------------------------------------------------------------- #
# Credential resolver
# --------------------------------------------------------------------------- #

def _jwt(payload: dict) -> str:
    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{b64({'alg': 'none'})}.{b64(payload)}.sig"


def test_jwt_helpers_and_account_id():
    tok = _jwt({"exp": time.time() + 9999, "https://api.openai.com/auth": {"chatgpt_account_id": "acct_9"}})
    assert oauth._jwt_payload(tok).get("exp")
    assert not oauth._token_expired(tok)
    assert oauth._token_expired(_jwt({"exp": time.time() - 10}))
    assert oauth._chatgpt_account_id({"id_token": tok}) == "acct_9"
    assert oauth._chatgpt_account_id({"account_id": "explicit"}) == "explicit"


def test_resolve_claude_credential(monkeypatch, tmp_path):
    from src import coding_auth_service as a
    monkeypatch.setattr(a, "DATA_DIR", str(tmp_path))
    cdir = a.auth_config_dir("tester", "claude")
    (cdir / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-XYZ", "refreshToken": "rt",
        "expiresAt": int((time.time() + 9999) * 1000),
    }}))
    cred = asyncio.run(oauth.resolve_subscription_credential("tester", "claude"))
    assert cred["provider"] == "anthropic"
    assert cred["base_url"] == oauth.ANTHROPIC_BASE
    assert cred["access_token"] == "sk-ant-oat01-XYZ"
    assert cred["headers"]["anthropic-beta"] == oauth.ANTHROPIC_OAUTH_BETA


def test_resolve_claude_not_logged_in(monkeypatch, tmp_path):
    from src import coding_auth_service as a
    monkeypatch.setattr(a, "DATA_DIR", str(tmp_path))  # empty dir, no creds file
    assert asyncio.run(oauth.resolve_subscription_credential("nobody", "claude")) is None


def test_resolve_codex_credential(monkeypatch, tmp_path):
    from src import coding_auth_service as a
    monkeypatch.setattr(a, "DATA_DIR", str(tmp_path))
    config_dir = a.auth_config_dir("tester", "codex")
    access = _jwt({"exp": time.time() + 9999, "https://api.openai.com/auth": {"chatgpt_account_id": "acct_42"}})
    (config_dir / "auth.json").write_text(json.dumps({
        "tokens": {"access_token": access, "refresh_token": "v1:refresh:abc"},
    }))
    cred = asyncio.run(oauth.resolve_subscription_credential("tester", "codex"))
    assert cred["provider"] == "openai"
    assert cred["base_url"] == oauth.CODEX_RESPONSES_BASE
    assert cred["access_token"] == access
    assert cred["headers"]["chatgpt-account-id"] == "acct_42"
    assert cred["headers"]["originator"] == oauth.CODEX_ORIGINATOR


# --------------------------------------------------------------------------- #
# Gateway chat_completion (mocked upstreams)
# --------------------------------------------------------------------------- #

def test_anthropic_payload_leads_with_claude_code_identity():
    body = {"model": "claude-opus-4-8", "messages": [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "yo"},
    ]}
    p = gw._anthropic_payload(body, 4096, 0.7, stream=False)
    # The subscription token is accepted only if the FIRST system block is the
    # Claude Code identity; the user's system follows.
    assert p["system"][0]["text"] == gw.CLAUDE_CODE_SYSTEM
    assert p["system"][1]["text"] == "be terse"
    assert p["messages"] == [{"role": "user", "content": "yo"}]
    assert p["max_tokens"] == 4096


def _mock_http(monkeypatch, sent: dict, json_resp: dict):
    class _Resp:
        status_code = 200
        def json(self):
            return json_resp
    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, headers=None, json=None):
            sent.update(url=url, headers=headers, body=json)
            return _Resp()
    monkeypatch.setattr(gw.httpx, "AsyncClient", _Client)


def test_chat_completion_claude_direct(monkeypatch):
    # Claude goes DIRECT to the Messages API (no CLI wrap) with the Claude Code identity.
    async def _cred(owner, provider):
        assert provider == "claude"
        return {"provider": "anthropic", "base_url": oauth.ANTHROPIC_BASE,
                "access_token": "sk-ant-oat01-REAL", "headers": {"anthropic-beta": oauth.ANTHROPIC_OAUTH_BETA}}
    monkeypatch.setattr(gw, "resolve_subscription_credential", _cred)
    sent = {}
    _mock_http(monkeypatch, sent, {"content": [{"type": "text", "text": "claude says hi"}]})
    out = asyncio.run(gw.chat_completion("tester", "claude", {"model": "claude-opus-4-8", "messages": [{"role": "user", "content": "yo"}]}))
    assert out["choices"][0]["message"]["content"] == "claude says hi"
    assert sent["url"].endswith("/v1/messages")
    assert sent["headers"]["Authorization"] == "Bearer sk-ant-oat01-REAL"
    assert sent["headers"]["anthropic-beta"] == oauth.ANTHROPIC_OAUTH_BETA
    assert sent["body"]["system"][0]["text"] == gw.CLAUDE_CODE_SYSTEM


def test_codex_goes_direct_to_responses(monkeypatch):
    async def _cred(owner, provider):
        assert provider == "codex"
        return {"provider": "openai", "base_url": oauth.CODEX_RESPONSES_BASE,
                "access_token": "tok", "headers": {"chatgpt-account-id": "acct_1"}}
    monkeypatch.setattr(gw, "resolve_subscription_credential", _cred)
    sent = {}
    _mock_http(monkeypatch, sent, {"output": [{"type": "message", "content": [{"type": "output_text", "text": "codex reply"}]}]})
    out = asyncio.run(gw.chat_completion("tester", "codex", {"model": "gpt-5.5", "messages": [{"role": "user", "content": "yo"}]}))
    assert out["choices"][0]["message"]["content"] == "codex reply"
    assert sent["url"].endswith("/responses")
    assert sent["headers"]["Authorization"] == "Bearer tok"
    assert sent["headers"]["chatgpt-account-id"] == "acct_1"
    assert sent["body"]["store"] is False


def test_chat_completion_not_logged_in(monkeypatch):
    async def _none(owner, provider):
        return None
    monkeypatch.setattr(gw, "resolve_subscription_credential", _none)
    with pytest.raises(gw.GatewayError):
        asyncio.run(gw.chat_completion("tester", "claude", {"model": "x", "messages": [{"role": "user", "content": "hi"}]}))


def test_claude_token_read_from_credentials_file(monkeypatch, tmp_path):
    # Token acquisition: read the access token from `claude auth login`'s credentials
    # file (reliable) — NOT scraped from setup-token's PTY output.
    from src import coding_auth_service as a
    from src import coding_provider_oauth as o
    monkeypatch.setattr(a, "DATA_DIR", str(tmp_path))
    cdir = a.auth_config_dir("tester", "claude")
    (cdir / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-FROMFILE", "refreshToken": "rt",
        "expiresAt": int((time.time() + 9999) * 1000),
    }}))
    cred = asyncio.run(o.resolve_subscription_credential("tester", "claude"))
    assert cred["provider"] == "anthropic"
    assert cred["access_token"] == "sk-ant-oat01-FROMFILE"
    assert "claude-code-20250219" in cred["headers"]["anthropic-beta"]
    # subscription_logged_in keys off the credentials file too.
    assert a.subscription_logged_in(None, "tester", "claude") is True


def test_claude_keychain_service_name_derivation():
    # macOS stores the blob under "Claude Code-credentials-<sha256(CLAUDE_CONFIG_DIR)[:8]>".
    import hashlib
    from pathlib import Path
    from src import coding_auth_service as a
    cdir = Path("/some/isolated/claude")
    expected = "Claude Code-credentials-" + hashlib.sha256(str(cdir).encode()).hexdigest()[:8]
    assert a._claude_keychain_service(cdir) == expected


def _fake_security(blob_for_read=None, write_sink=None):
    """Stand-in for subprocess.run(['security', ...]) — emulates find/add generic-password."""
    class _Res:
        def __init__(self, rc, out=""):
            self.returncode, self.stdout, self.stderr = rc, out, ""
    def run(args, **kw):
        verb = args[1] if len(args) > 1 else ""
        if verb == "find-generic-password":
            if blob_for_read is None:
                return _Res(44)  # SecKeychain "item not found"
            return _Res(0, json.dumps(blob_for_read))
        if verb == "add-generic-password":
            if write_sink is not None:
                write_sink.append(args[-1])  # the JSON payload after -w
            return _Res(0)
        return _Res(1)
    return run


def test_claude_token_read_from_keychain(monkeypatch, tmp_path):
    # macOS: no .credentials.json file — the token lives in the Keychain. Token acquisition
    # must read it from there (via `security find-generic-password`).
    from src import coding_auth_service as a
    from src import coding_provider_oauth as o
    monkeypatch.setattr(a, "DATA_DIR", str(tmp_path))
    a.auth_config_dir("tester", "claude")  # create dir, but NO .credentials.json
    monkeypatch.setattr(a, "_is_macos", lambda: True)
    monkeypatch.setattr(a.subprocess, "run", _fake_security(blob_for_read={
        "claudeAiOauth": {"accessToken": "sk-ant-oat01-FROMKEYCHAIN", "refreshToken": "rt",
                          "expiresAt": int((time.time() + 9999) * 1000)}}))
    cred = asyncio.run(o.resolve_subscription_credential("tester", "claude"))
    assert cred is not None and cred["access_token"] == "sk-ant-oat01-FROMKEYCHAIN"


def test_claude_login_present_via_claude_json(monkeypatch, tmp_path):
    # macOS detection signal: oauthAccount in .claude.json (token itself is in Keychain),
    # checked WITHOUT spawning `security` on the hot status-poll path.
    from src import coding_auth_service as a
    monkeypatch.setattr(a, "DATA_DIR", str(tmp_path))
    cdir = a.auth_config_dir("tester", "claude")
    assert a.claude_login_present(cdir) is False
    (cdir / ".claude.json").write_text(json.dumps({"oauthAccount": {
        "accountUuid": "u-123", "emailAddress": "me@example.com"}}))
    assert a.claude_login_present(cdir) is True


def test_claude_creds_prefer_file_then_keychain(monkeypatch, tmp_path):
    from src import coding_auth_service as a
    monkeypatch.setattr(a, "DATA_DIR", str(tmp_path))
    cdir = a.auth_config_dir("tester", "claude")
    monkeypatch.setattr(a, "_is_macos", lambda: True)
    monkeypatch.setattr(a.subprocess, "run", _fake_security(blob_for_read={"claudeAiOauth": {"accessToken": "KEY"}}))
    # File wins when present...
    (cdir / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "FILE"}}))
    store, data = a.read_claude_credentials_raw(cdir)
    assert store == "file" and a.claude_oauth_blob(data)["accessToken"] == "FILE"
    # ...Keychain is the fallback when no file.
    (cdir / ".credentials.json").unlink()
    store, data = a.read_claude_credentials_raw(cdir)
    assert store == "keychain" and a.claude_oauth_blob(data)["accessToken"] == "KEY"


def test_claude_credentials_writeback_routes_to_keychain(monkeypatch, tmp_path):
    from src import coding_auth_service as a
    monkeypatch.setattr(a, "DATA_DIR", str(tmp_path))
    cdir = a.auth_config_dir("tester", "claude")
    monkeypatch.setattr(a, "_is_macos", lambda: True)
    sink = []
    monkeypatch.setattr(a.subprocess, "run", _fake_security(write_sink=sink))
    a.write_claude_credentials_raw(cdir, "keychain", {"claudeAiOauth": {"accessToken": "NEW"}})
    assert sink and json.loads(sink[0])["claudeAiOauth"]["accessToken"] == "NEW"
    # ...and there's no stray plaintext .credentials.json on disk for the keychain store.
    assert not (cdir / ".credentials.json").exists()


# --------------------------------------------------------------------------- #
# Live model listing
# --------------------------------------------------------------------------- #

def test_list_models_not_logged_in_falls_back_to_curated(monkeypatch):
    async def _none(owner, provider):
        return None
    monkeypatch.setattr(gw, "resolve_subscription_credential", _none)
    assert asyncio.run(gw.list_models("tester", "codex")) == gw.curated_models("codex")


def test_list_models_codex_is_curated_current(monkeypatch):
    async def _cred(owner, provider):
        return {"provider": "openai", "base_url": oauth.CODEX_RESPONSES_BASE, "access_token": "t", "headers": {}}
    monkeypatch.setattr(gw, "resolve_subscription_credential", _cred)
    ids = asyncio.run(gw.list_models("tester", "codex"))
    assert "gpt-5.3-codex" in ids and "gpt-5-codex" not in ids


def test_cached_or_curated_is_instant_and_no_network(monkeypatch):
    # The /v1/models route path must NOT call resolve/network (probe is ~1s).
    called = {"n": 0}
    async def _boom(owner, provider):
        called["n"] += 1
        return None
    monkeypatch.setattr(gw, "resolve_subscription_credential", _boom)
    assert gw.cached_or_curated("tester", "codex") == gw.curated_models("codex")
    assert called["n"] == 0
    # A populated cache (from a prior background refresh) is returned as-is.
    gw._MODELS_CACHE[("tester", "claude")] = (gw._now(), ["claude-a", "claude-b"])
    assert gw.cached_or_curated("tester", "claude") == ["claude-a", "claude-b"]
    assert called["n"] == 0


def test_list_models_anthropic_is_live(monkeypatch):
    async def _cred(owner, provider):
        return {"provider": "anthropic", "base_url": oauth.ANTHROPIC_BASE, "access_token": "t",
                "headers": {"anthropic-beta": oauth.ANTHROPIC_OAUTH_BETA}}
    monkeypatch.setattr(gw, "resolve_subscription_credential", _cred)

    class _Resp:
        status_code = 200
        def json(self):
            return {"data": [{"id": "claude-opus-4-8"}, {"id": "claude-sonnet-4-6"}, {"id": "claude-future-9"}]}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None):
            assert headers["Authorization"].startswith("Bearer ")
            assert "anthropic-beta" in headers
            return _Resp()

    monkeypatch.setattr(gw.httpx, "AsyncClient", _Client)
    ids = asyncio.run(gw.list_models("tester", "claude"))
    assert ids == ["claude-opus-4-8", "claude-sonnet-4-6", "claude-future-9"]


# --------------------------------------------------------------------------- #
# llm_core Anthropic OAuth header mode
# --------------------------------------------------------------------------- #

def test_llm_core_anthropic_oauth_keeps_bearer():
    from src.llm_core import _build_anthropic_headers
    # OAuth (anthropic-beta present) -> keep Bearer, do NOT convert to x-api-key.
    h = _build_anthropic_headers({"Authorization": "Bearer sk-ant-oat01-X", "anthropic-beta": "oauth-2025-04-20"})
    assert h["Authorization"] == "Bearer sk-ant-oat01-X"
    assert "x-api-key" not in h
    assert h["anthropic-beta"] == "oauth-2025-04-20"
    # API key (no beta) -> convert to x-api-key (legacy behavior preserved).
    h2 = _build_anthropic_headers({"Authorization": "Bearer my-api-key"})
    assert h2["x-api-key"] == "my-api-key"
    assert "Authorization" not in h2


# --------------------------------------------------------------------------- #
# Gateway-backed ModelEndpoint creation
# --------------------------------------------------------------------------- #

def test_upsert_subscription_endpoint(monkeypatch, tmp_path):
    import core.database as database

    engine = create_engine(f"sqlite:///{tmp_path}/t.sqlite", connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", SessionLocal)

    from routes.coding_auth_routes import _upsert_subscription_endpoint
    result = _upsert_subscription_endpoint("tester", "claude")
    assert result["provider"] == "claude"
    assert "claude-opus-4-8" in result["models"]

    db = SessionLocal()
    try:
        ep = db.query(ModelEndpoint).filter(ModelEndpoint.id == result["endpoint_id"]).first()
        assert ep is not None
        assert ep.name == "Claude subscription"
        # Opaque in-process address — NO loopback host/port to go stale.
        assert ep.base_url == "odyoauth://claude"
        # api_key carries the capability token (decrypts to owner+provider).
        assert gw.parse_gateway_token(ep.api_key) == ("tester", "claude")
        # Idempotent: a second call updates the same row, doesn't duplicate.
        again = _upsert_subscription_endpoint("tester", "claude")
        assert again["endpoint_id"] == result["endpoint_id"]
        assert db.query(ModelEndpoint).filter(ModelEndpoint.name == "Claude subscription").count() == 1
    finally:
        db.close()
        engine.dispose()
