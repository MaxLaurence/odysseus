"""OpenAI-compatible subscription gateway routes.

A ModelEndpoint whose base_url is ``http://127.0.0.1:<port>/api/llm-oauth/v1`` and whose
api_key is an ``odyoauth:…`` token routes the whole app through a user's Claude / ChatGPT
**subscription** (see src/coding_oauth_gateway.py). These routes self-authenticate via that
opaque token (decrypts to owner+provider) and are exempted from session auth in app.py
(``AUTH_EXEMPT_PREFIXES`` includes ``/api/llm-oauth``), because ``llm_core`` calls them
server-to-server without a session cookie.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from src.coding_oauth_gateway import (
    GatewayError,
    chat_completion,
    chat_completion_stream,
    models_response_cached,
    parse_gateway_token,
    schedule_models_refresh,
)


def gateway_base_url(request: "Request | None" = None) -> str:
    """Loopback base URL for the gateway, on the port the backend ACTUALLY bound.

    Prefer the launcher-set ODYSSEUS_PORT / APP_PORT env (authoritative for the bound
    port); the live request's port is an unreliable fallback (in the packaged app it
    can be an ephemeral/proxy port). NEVER default to 7000 — macOS AirPlay holds it.
    Note: the stored port is only a hint — ``resolve_url`` rewrites gateway URLs to the
    live port on every use, so a stale stored port self-heals across restarts."""
    port = os.environ.get("ODYSSEUS_PORT") or os.environ.get("APP_PORT")
    if not port and request is not None:
        try:
            port = request.url.port
        except Exception:
            port = None
    port = port or "7860"
    return f"http://127.0.0.1:{port}/api/llm-oauth/v1"


def _auth(request: Request) -> tuple[str, str]:
    parsed = parse_gateway_token(request.headers.get("authorization", ""))
    if not parsed:
        raise HTTPException(401, "Invalid or missing subscription gateway token")
    owner, provider = parsed
    return owner, provider


def setup_llm_oauth_routes() -> APIRouter:
    router = APIRouter(prefix="/api/llm-oauth", tags=["llm_oauth_gateway"])

    @router.get("/v1/models")
    async def list_models(request: Request):
        owner, provider = _auth(request)
        # Answer INSTANTLY from cache/curated (the reachability probe is ~1s); refresh
        # the live list in the background so it's current next time. Never block here.
        schedule_models_refresh(owner, provider)
        return models_response_cached(owner, provider)

    @router.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        owner, provider = _auth(request)
        try:
            body: dict[str, Any] = await request.json()
        except Exception:
            raise HTTPException(400, "Invalid JSON body")
        if not isinstance(body, dict):
            raise HTTPException(400, "Body must be a JSON object")
        if body.get("stream"):
            async def _gen():
                async for chunk in chat_completion_stream(owner, provider, body):
                    yield chunk
            return StreamingResponse(_gen(), media_type="text/event-stream")
        try:
            return await chat_completion(owner, provider, body)
        except GatewayError as exc:
            raise HTTPException(exc.status_code, exc.detail)

    return router
