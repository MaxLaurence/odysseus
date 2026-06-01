"""Thread-scoped Coding Station provider bridge routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from core.middleware import require_admin
from src.auth_helpers import require_user
from src.coding_provider import (
    CodingProviderError,
    authenticate_provider_token,
    call_provider_tool,
    list_provider_tokens,
    mint_provider_token,
    provider_capabilities_response,
    revoke_provider_token,
)
from src.coding_provider_tokens import provider_bearer_token_from_header, provider_request_scope_from_headers


class ProviderTokenCreate(BaseModel):
    thread_id: str
    name: str | None = None
    capabilities: list[str] | str | None = None
    expires_at: str | None = None
    ttl_seconds: int | None = None
    metadata: dict[str, Any] | None = None


def _owner(request: Request) -> str:
    user = require_user(request)
    if user == "api":
        return (getattr(request.state, "api_token_owner", None) or "").strip()
    return user or ""


def _http_error(exc: CodingProviderError):
    raise HTTPException(exc.status_code, exc.detail)


def setup_coding_provider_routes(memory_manager, session_manager, memory_vector=None) -> APIRouter:
    router = APIRouter(prefix="/api/coding/provider", tags=["coding_provider"])

    @router.get("/capabilities")
    async def provider_capabilities(request: Request):
        require_admin(request)
        _owner(request)
        return provider_capabilities_response()

    @router.post("/tokens")
    async def mint_token(request: Request, body: ProviderTokenCreate):
        require_admin(request)
        owner = _owner(request)
        try:
            return mint_provider_token(
                owner=owner,
                thread_id=body.thread_id,
                name=body.name,
                capabilities=body.capabilities,
                expires_at=body.expires_at,
                ttl_seconds=body.ttl_seconds,
                metadata=body.metadata,
            )
        except CodingProviderError as exc:
            _http_error(exc)

    @router.post("/mint")
    async def mint_token_alias(request: Request, body: ProviderTokenCreate):
        return await mint_token(request, body)

    @router.get("/tokens")
    async def list_tokens(
        request: Request,
        thread_id: str | None = Query(None),
        include_revoked: bool = Query(False),
    ):
        require_admin(request)
        owner = _owner(request)
        return {
            "provider_tokens": list_provider_tokens(
                owner=owner,
                thread_id=thread_id,
                include_revoked=include_revoked,
            )
        }

    @router.delete("/tokens/{token_id}")
    async def revoke_token(request: Request, token_id: str):
        require_admin(request)
        owner = _owner(request)
        try:
            return {"provider_token": revoke_provider_token(owner, token_id)}
        except CodingProviderError as exc:
            _http_error(exc)

    @router.post("/tokens/{token_id}/revoke")
    async def revoke_token_alias(request: Request, token_id: str):
        return await revoke_token(request, token_id)

    @router.post("/tool")
    async def tool_call(request: Request, body: dict[str, Any]):
        try:
            context = authenticate_provider_token(
                provider_bearer_token_from_header(request.headers.get("authorization", "")),
                provider_request_scope_from_headers(request.headers),
            )
            return await call_provider_tool(
                context,
                body,
                memory_manager=memory_manager,
                session_manager=session_manager,
                memory_vector=memory_vector,
            )
        except CodingProviderError as exc:
            _http_error(exc)

    return router
