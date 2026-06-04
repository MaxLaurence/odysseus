"""Per-owner subscription OAuth login routes for coding-agent providers (claude/codex).

These drive each CLI's OWN login flow inside an isolated, per-owner config dir
(see src/coding_auth_service.py). The login PTY streams to a Code Station pane via
the ``/attach`` WebSocket (reusing the same dtach transport as a normal run), so the
user completes the browser OAuth and the service detects the resulting credential.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect

from core.middleware import require_admin
from routes.coding_routes import _websocket_admin_allowed, _websocket_owner
from src.auth_helpers import require_user
from src.coding_auth_service import CodingAuthError, get_coding_auth_service
from src.coding_auth_service import normalize_provider


def _owner(request: Request) -> str:
    user = require_user(request)
    if user == "api":
        return (getattr(request.state, "api_token_owner", None) or "").strip()
    return user or ""


def _http_error(exc: CodingAuthError):
    raise HTTPException(exc.status_code, exc.detail)


def setup_coding_auth_routes() -> APIRouter:
    router = APIRouter(prefix="/api/coding/provider/auth", tags=["coding_provider_auth"])
    service = get_coding_auth_service()

    @router.get("/status")
    async def status_all(request: Request):
        owner = _owner(request)
        return {"providers": service.status_all(owner)}

    @router.get("/{provider}/status")
    async def status_one(request: Request, provider: str):
        owner = _owner(request)
        try:
            return service.status(owner, provider)
        except CodingAuthError as exc:
            _http_error(exc)

    @router.post("/{provider}/login")
    async def login(request: Request, provider: str):
        require_admin(request)
        owner = _owner(request)
        try:
            return await service.login(owner, provider)
        except CodingAuthError as exc:
            _http_error(exc)

    @router.post("/{provider}/logout")
    async def logout(request: Request, provider: str):
        require_admin(request)
        owner = _owner(request)
        try:
            return await service.logout(owner, provider)
        except CodingAuthError as exc:
            _http_error(exc)

    @router.post("/{provider}/endpoint")
    async def create_subscription_endpoint(request: Request, provider: str):
        """Create (or refresh) a model ENDPOINT that routes the whole app through this
        owner's subscription via the OpenAI-compatible gateway. Requires being logged in."""
        require_admin(request)
        owner = _owner(request)
        prov = normalize_provider(provider)
        if not prov:
            raise HTTPException(400, f"Unknown provider: {provider}")
        if not service.status(owner, prov).get("logged_in"):
            raise HTTPException(409, f"Log in to {prov} before adding it as a model provider")
        from src.coding_oauth_gateway import list_models
        models = await list_models(owner, prov)
        return _upsert_subscription_endpoint(owner, prov, models=models)

    @router.websocket("/{provider}/attach")
    async def attach(websocket: WebSocket, provider: str):
        # WebSocket auth mirrors the run PTY route: BaseHTTPMiddleware doesn't run for
        # websockets, so validate the session cookie + admin here.
        owner = _websocket_owner(websocket)
        if owner is None or not _websocket_admin_allowed(websocket, owner):
            await websocket.close(code=1008)
            return

        def _int_param(name: str, default: int) -> int:
            try:
                return int(websocket.query_params.get(name, default))
            except (TypeError, ValueError):
                return default

        cols = _int_param("cols", 100)
        rows = _int_param("rows", 30)
        await websocket.accept()
        try:
            await service.attach(websocket, owner, provider, cols=cols, rows=rows)
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            try:
                await websocket.close()
            except Exception:
                pass

    return router


def _upsert_subscription_endpoint(owner: str, provider: str, models: list[str] | None = None) -> dict:
    """Create/refresh the subscription ModelEndpoint for (owner, provider). Idempotent
    on (owner, name). The base_url is the opaque ``odyoauth://<provider>`` address
    (handled in-process by llm_core — NO loopback host/port), and api_key carries the
    owner+provider capability token."""
    import json
    import uuid

    from core.database import ModelEndpoint, SessionLocal
    from src.coding_oauth_gateway import curated_models, mint_gateway_token

    label = "Claude subscription" if provider == "claude" else "ChatGPT (Codex) subscription"
    base_url = f"odyoauth://{provider}"
    token = mint_gateway_token(owner, provider)
    model_list = models if models else curated_models(provider)
    models_json = json.dumps(model_list)

    db = SessionLocal()
    try:
        existing = (
            db.query(ModelEndpoint)
            .filter(ModelEndpoint.owner == (owner or None), ModelEndpoint.name == label)
            .first()
        )
        if existing is None:
            existing = ModelEndpoint(id=str(uuid.uuid4())[:8], name=label, owner=owner or None)
            db.add(existing)
        existing.base_url = base_url
        existing.api_key = token
        existing.is_enabled = True
        existing.model_type = "llm"
        existing.cached_models = models_json
        existing.pinned_models = models_json
        existing.supports_tools = True
        db.commit()
        db.refresh(existing)
        return {
            "endpoint_id": existing.id,
            "name": existing.name,
            "base_url": existing.base_url,
            "provider": provider,
            "models": model_list,
        }
    finally:
        db.close()
