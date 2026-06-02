"""Token and capability primitives for the Coding Station provider bridge."""

from __future__ import annotations

import hashlib
import json
import secrets
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from core.database import (
    CodingRun,
    CodingProviderToken,
    CodingThread,
    SessionLocal as _DEFAULT_SESSION_LOCAL,
)

TOKEN_PREFIX = "ody_cp_"
TOKEN_LOOKUP_PREFIX_LEN = len(TOKEN_PREFIX) + 10
PROVIDER_TOOL_PATH = "/api/coding/provider/tool"
RUN_SCOPE_RUN_HEADER = "X-Odysseus-Run-ID"
RUN_SCOPE_THREAD_HEADER = "X-Odysseus-Thread-ID"
RUN_SCOPE_PROJECT_HEADER = "X-Odysseus-Project-ID"

CREDENTIAL_CLASS_EXPLICIT = "explicit"
CREDENTIAL_CLASS_RUN = "run"
RUN_CREDENTIAL_TTL_SECONDS = 60 * 60 * 6
RUN_CREDENTIAL_ACTIVE_STATUSES = {"starting", "running"}

_CREDENTIAL_CLASS_METADATA_KEY = "_odysseus_credential_class"
_RUN_ID_METADATA_KEY = "_odysseus_run_id"
_PROJECT_ID_METADATA_KEY = "_odysseus_project_id"

ALL_CAPABILITIES = {
    "memory.read",
    "memory.write",
    "coding.read",
    "coding.write",
    "terminal.read",
    "terminal.start",
    "terminal.stdin",
    "terminal.resize",
    "terminal.stop",
    "thread.messages.read",
    "thread.messages.write",
    "model_config.read",
    "model_config.derive",
    "model_config.restore",
    "task.acquire",
    "task.release",
    "task.heartbeat",
    "agent.state",
}

READ_ONLY_CAPABILITIES = {
    "coding.read",
    "terminal.read",
    "thread.messages.read",
    "model_config.read",
}

CAPABILITY_GROUPS = {
    "all": ALL_CAPABILITIES,
    "*": ALL_CAPABILITIES,
    "read": {
        "memory.read",
        "coding.read",
        "terminal.read",
        "thread.messages.read",
        "model_config.read",
    },
    "write": {
        "memory.write",
        "coding.write",
        "terminal.start",
        "terminal.stdin",
        "terminal.resize",
        "terminal.stop",
        "thread.messages.write",
        "model_config.derive",
        "model_config.restore",
        "task.acquire",
        "task.release",
        "task.heartbeat",
        "agent.state",
    },
    "terminal.write": {"terminal.start", "terminal.stdin", "terminal.resize"},
    "model_config.write": {"model_config.derive", "model_config.restore"},
    "task.write": {"task.acquire", "task.release", "task.heartbeat"},
}

CAPABILITY_ALIASES = {
    "memory:read": "memory.read",
    "memory:write": "memory.write",
    "coding:read": "coding.read",
    "coding:write": "coding.write",
    "thread:read": "coding.read",
    "thread:write": "coding.write",
    "messages.read": "thread.messages.read",
    "messages.write": "thread.messages.write",
    "thread_messages.read": "thread.messages.read",
    "thread_messages.write": "thread.messages.write",
    "model.read": "model_config.read",
    "model.derive": "model_config.derive",
    "model.restore": "model_config.restore",
    "model_config:read": "model_config.read",
    "model_config:derive": "model_config.derive",
    "model_config:restore": "model_config.restore",
}

CROSS_THREAD_METADATA_KEYS = {
    "allow_cross_thread",
    "allowed_thread_ids",
    "cross_thread",
    "cross_thread_capabilities",
    "cross_thread_permissions",
    "thread_permissions",
}

PUBLIC_RESERVED_METADATA_KEYS = {
    "credential_class",
    "credential-class",
    "run_id",
    "run-id",
    "run_scoped",
    "run-scoped",
}

# Kept as a facade-compatible attribute for tests and local monkeypatches. New
# code resolves the current session factory dynamically through _session_local.
SessionLocal = _DEFAULT_SESSION_LOCAL


class CodingProviderError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class ProviderRequestScope:
    run_id: str = ""
    thread_id: str = ""
    project_id: str = ""


@dataclass(frozen=True)
class ProviderContext:
    token_id: str
    thread_id: str
    project_id: str
    owner: str
    session_id: str | None
    capabilities: frozenset[str]
    credential_class: str = CREDENTIAL_CLASS_EXPLICIT
    run_id: str | None = None


def _session_local():
    facade = sys.modules.get("src.coding_provider")
    facade_session = getattr(facade, "SessionLocal", None) if facade is not None else None
    if facade_session is not None and facade_session is not SessionLocal:
        return facade_session
    from core import database

    return database.SessionLocal


def json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except Exception:
        return fallback


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def memory_owner(owner: str) -> str | None:
    return owner or None


def _token_active(row: CodingProviderToken, now: datetime | None = None) -> bool:
    now = now or datetime.utcnow()
    if row.revoked_at is not None:
        return False
    return row.expires_at is None or row.expires_at > now


def _is_cross_thread_metadata_key(key: str) -> bool:
    normalized = str(key).replace("-", "_").lower()
    return normalized in CROSS_THREAD_METADATA_KEYS or normalized.startswith("cross_thread_")


def _is_reserved_public_metadata_key(key: str) -> bool:
    normalized = str(key).strip().lower()
    return (
        normalized.startswith("_odysseus_")
        or normalized in PUBLIC_RESERVED_METADATA_KEYS
        or _is_cross_thread_metadata_key(normalized)
    )


def _public_metadata(value: str | None) -> dict[str, Any]:
    metadata = json_loads(value, {})
    if not isinstance(metadata, dict):
        return {}
    return strip_reserved_metadata(metadata)


def strip_reserved_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        str(key): item
        for key, item in metadata.items()
        if not _is_reserved_public_metadata_key(str(key))
    }


def strip_cross_thread_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return strip_reserved_metadata(metadata)


def _private_metadata(value: str | None) -> dict[str, Any]:
    metadata = json_loads(value, {})
    return metadata if isinstance(metadata, dict) else {}


def _credential_class(metadata: dict[str, Any]) -> str:
    if str(metadata.get(_CREDENTIAL_CLASS_METADATA_KEY) or "").strip().lower() == CREDENTIAL_CLASS_RUN:
        return CREDENTIAL_CLASS_RUN
    return CREDENTIAL_CLASS_EXPLICIT


def token_dict(row: CodingProviderToken) -> dict[str, Any]:
    credential_class = _credential_class(_private_metadata(row.metadata_json))
    return {
        "id": row.id,
        "thread_id": row.thread_id,
        "owner": row.owner or "",
        "name": row.name,
        "token_prefix": row.token_prefix,
        "credential_class": credential_class,
        "capabilities": json_loads(row.capabilities_json, []),
        "metadata": _public_metadata(row.metadata_json),
        "active": _token_active(row),
        "expires_at": _iso(row.expires_at),
        "last_used_at": _iso(row.last_used_at),
        "revoked_at": _iso(row.revoked_at),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _parse_expires_at(expires_at: str | None, ttl_seconds: int | None) -> datetime | None:
    if ttl_seconds is not None:
        try:
            ttl = int(ttl_seconds)
        except (TypeError, ValueError) as exc:
            raise CodingProviderError(400, "ttl_seconds must be an integer") from exc
        if ttl <= 0:
            raise CodingProviderError(400, "ttl_seconds must be positive")
        return datetime.utcnow() + timedelta(seconds=ttl)
    if not expires_at:
        return None
    value = str(expires_at).strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CodingProviderError(400, "expires_at must be an ISO timestamp") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    if parsed <= datetime.utcnow():
        raise CodingProviderError(400, "expires_at must be in the future")
    return parsed


def normalize_capabilities(values: Any) -> list[str]:
    if values is None or values == "":
        return sorted(READ_ONLY_CAPABILITIES)
    if isinstance(values, str):
        raw_items = [item.strip() for item in values.split(",")]
    elif isinstance(values, (list, tuple, set)):
        raw_items = [str(item).strip() for item in values]
    else:
        raise CodingProviderError(400, "capabilities must be a list or comma-separated string")

    capabilities: set[str] = set()
    for item in raw_items:
        if not item:
            continue
        normalized = CAPABILITY_ALIASES.get(item, item).replace(":", ".")
        normalized = CAPABILITY_ALIASES.get(normalized, normalized)
        group = CAPABILITY_GROUPS.get(normalized)
        if group is not None:
            capabilities.update(group)
            continue
        if normalized not in ALL_CAPABILITIES:
            raise CodingProviderError(400, f"Unknown provider capability: {item}")
        capabilities.add(normalized)
    if not capabilities:
        capabilities.update(READ_ONLY_CAPABILITIES)
    return sorted(capabilities)


def require_capability(context: ProviderContext, capability: str) -> None:
    if capability not in context.capabilities:
        raise CodingProviderError(403, f"Provider token lacks capability: {capability}")


def provider_bearer_token_from_header(auth_header: str | None) -> str:
    scheme, _, token = (auth_header or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise CodingProviderError(401, "Provider bearer token required")
    return token.strip()


def is_provider_tool_path(path: str) -> bool:
    return path == PROVIDER_TOOL_PATH or path.startswith(PROVIDER_TOOL_PATH + "/")


def is_provider_tool_bearer(path: str, auth_header: str | None) -> bool:
    if not is_provider_tool_path(path):
        return False
    try:
        token = provider_bearer_token_from_header(auth_header)
    except CodingProviderError:
        return False
    return token.startswith(TOKEN_PREFIX)


def _header_value(headers: Mapping[str, str] | None, name: str) -> str:
    if not headers:
        return ""
    value = headers.get(name)
    if value is None:
        value = headers.get(name.lower())
    if value is None:
        value = headers.get(name.upper())
    return str(value or "").strip()


def provider_request_scope_from_headers(headers: Mapping[str, str] | None) -> ProviderRequestScope:
    return ProviderRequestScope(
        run_id=_header_value(headers, RUN_SCOPE_RUN_HEADER),
        thread_id=_header_value(headers, RUN_SCOPE_THREAD_HEADER),
        project_id=_header_value(headers, RUN_SCOPE_PROJECT_HEADER),
    )


def _mint_provider_token(
    *,
    owner: str,
    thread_id: str,
    name: str | None = None,
    capabilities: Any = None,
    expires_at: str | None = None,
    ttl_seconds: int | None = None,
    metadata: dict[str, Any] | None = None,
    credential_class: str = CREDENTIAL_CLASS_EXPLICIT,
    run_id: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    owner = (owner or "").strip()
    thread_id = (thread_id or "").strip()
    if not thread_id:
        raise CodingProviderError(400, "thread_id is required")
    credential_class = (credential_class or CREDENTIAL_CLASS_EXPLICIT).strip().lower()
    if credential_class not in {CREDENTIAL_CLASS_EXPLICIT, CREDENTIAL_CLASS_RUN}:
        raise CodingProviderError(400, "Unknown provider credential class")
    run_id = (run_id or "").strip()
    project_id = (project_id or "").strip()
    if credential_class == CREDENTIAL_CLASS_RUN and (not run_id or not project_id):
        raise CodingProviderError(400, "run_id and project_id are required for run credentials")
    caps = normalize_capabilities(capabilities)
    expires = _parse_expires_at(expires_at, ttl_seconds)
    safe_name = (name or "Provider").strip()[:100] or "Provider"
    if metadata is not None and not isinstance(metadata, dict):
        raise CodingProviderError(400, "metadata must be an object")
    safe_metadata = strip_reserved_metadata(metadata or {})
    if credential_class == CREDENTIAL_CLASS_RUN:
        safe_metadata.update(
            {
                _CREDENTIAL_CLASS_METADATA_KEY: CREDENTIAL_CLASS_RUN,
                _RUN_ID_METADATA_KEY: run_id,
                _PROJECT_ID_METADATA_KEY: project_id,
            }
        )

    raw_token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    row = CodingProviderToken(
        id=str(uuid.uuid4()),
        thread_id=thread_id,
        owner=owner,
        name=safe_name,
        token_prefix=raw_token[:TOKEN_LOOKUP_PREFIX_LEN],
        token_hash=_token_hash(raw_token),
        capabilities_json=json.dumps(caps),
        metadata_json=json.dumps(safe_metadata),
        expires_at=expires,
    )

    db = _session_local()()
    try:
        thread = (
            db.query(CodingThread)
            .filter(CodingThread.id == thread_id, CodingThread.owner == owner)
            .first()
        )
        if not thread:
            raise CodingProviderError(404, "Thread not found")
        if credential_class == CREDENTIAL_CLASS_RUN:
            if thread.project_id != project_id:
                raise CodingProviderError(400, "Run credential project_id does not match thread")
            run = (
                db.query(CodingRun)
                .filter(CodingRun.id == run_id, CodingRun.owner == owner)
                .first()
            )
            if not run or run.thread_id != thread_id:
                raise CodingProviderError(404, "Run not found")
            if run.status not in RUN_CREDENTIAL_ACTIVE_STATUSES:
                raise CodingProviderError(409, "Run is not active")
        db.add(row)
        db.commit()
        db.refresh(row)
        return {"token": raw_token, "provider_token": token_dict(row)}
    finally:
        db.close()


def mint_provider_token(
    *,
    owner: str,
    thread_id: str,
    name: str | None = None,
    capabilities: Any = None,
    expires_at: str | None = None,
    ttl_seconds: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _mint_provider_token(
        owner=owner,
        thread_id=thread_id,
        name=name,
        capabilities=capabilities,
        expires_at=expires_at,
        ttl_seconds=ttl_seconds,
        metadata=metadata,
        credential_class=CREDENTIAL_CLASS_EXPLICIT,
    )


def mint_run_provider_token(
    *,
    owner: str,
    thread_id: str,
    project_id: str,
    run_id: str,
    name: str | None = None,
    capabilities: Any = "all",
    ttl_seconds: int | None = RUN_CREDENTIAL_TTL_SECONDS,
) -> dict[str, Any]:
    return _mint_provider_token(
        owner=owner,
        thread_id=thread_id,
        name=name or f"Run {str(run_id)[:8]} provider bridge",
        capabilities=capabilities,
        ttl_seconds=ttl_seconds,
        metadata=None,
        credential_class=CREDENTIAL_CLASS_RUN,
        run_id=run_id,
        project_id=project_id,
    )


def list_provider_tokens(owner: str, thread_id: str | None = None, include_revoked: bool = False) -> list[dict[str, Any]]:
    owner = (owner or "").strip()
    db = _session_local()()
    try:
        query = db.query(CodingProviderToken).filter(CodingProviderToken.owner == owner)
        if thread_id:
            query = query.filter(CodingProviderToken.thread_id == thread_id)
        if not include_revoked:
            query = query.filter(CodingProviderToken.revoked_at == None)  # noqa: E711
        rows = query.order_by(CodingProviderToken.created_at.desc()).all()
        return [token_dict(row) for row in rows]
    finally:
        db.close()


def revoke_provider_token(owner: str, token_id: str) -> dict[str, Any]:
    owner = (owner or "").strip()
    token_id = (token_id or "").strip()
    if not token_id:
        raise CodingProviderError(400, "token_id is required")
    db = _session_local()()
    try:
        row = (
            db.query(CodingProviderToken)
            .filter(CodingProviderToken.id == token_id, CodingProviderToken.owner == owner)
            .first()
        )
        if not row:
            raise CodingProviderError(404, "Provider token not found")
        if row.revoked_at is None:
            row.revoked_at = datetime.utcnow()
            row.updated_at = datetime.utcnow()
            db.commit()
            db.refresh(row)
        return token_dict(row)
    finally:
        db.close()


def _require_run_scope(
    *,
    db,
    row: CodingProviderToken,
    thread: CodingThread,
    metadata: dict[str, Any],
    request_scope: ProviderRequestScope | None,
) -> str:
    scope = request_scope or ProviderRequestScope()
    expected_run_id = str(metadata.get(_RUN_ID_METADATA_KEY) or "").strip()
    expected_project_id = str(metadata.get(_PROJECT_ID_METADATA_KEY) or "").strip()
    if not expected_run_id or not expected_project_id:
        raise CodingProviderError(401, "Provider run credential is missing scope metadata")
    if not scope.run_id or not scope.thread_id or not scope.project_id:
        raise CodingProviderError(401, "Provider run credential requires run, thread, and project headers")
    if (
        not secrets.compare_digest(scope.run_id, expected_run_id)
        or not secrets.compare_digest(scope.thread_id, thread.id)
        or not secrets.compare_digest(scope.project_id, expected_project_id)
    ):
        raise CodingProviderError(401, "Provider run credential scope mismatch")
    if thread.project_id != expected_project_id:
        raise CodingProviderError(401, "Provider run credential scope mismatch")
    run = (
        db.query(CodingRun)
        .filter(CodingRun.id == expected_run_id, CodingRun.owner == (row.owner or ""))
        .first()
    )
    if not run:
        raise CodingProviderError(401, "Provider run credential no longer has an active run")
    if run.thread_id != thread.id:
        raise CodingProviderError(401, "Provider run credential scope mismatch")
    if run.status not in RUN_CREDENTIAL_ACTIVE_STATUSES:
        raise CodingProviderError(401, "Provider run credential run is not active")
    return expected_run_id


def authenticate_provider_token(
    raw_token: str | None,
    request_scope: ProviderRequestScope | None = None,
) -> ProviderContext:
    raw_token = (raw_token or "").strip()
    if not raw_token.startswith(TOKEN_PREFIX):
        raise CodingProviderError(401, "Invalid provider token")
    token_hash = _token_hash(raw_token)
    lookup_prefix = raw_token[:TOKEN_LOOKUP_PREFIX_LEN]
    now = datetime.utcnow()
    db = _session_local()()
    try:
        candidates = (
            db.query(CodingProviderToken)
            .filter(CodingProviderToken.token_prefix == lookup_prefix)
            .all()
        )
        for row in candidates:
            if not secrets.compare_digest(row.token_hash or "", token_hash):
                continue
            if not _token_active(row, now):
                raise CodingProviderError(401, "Provider token expired or revoked")
            thread = (
                db.query(CodingThread)
                .filter(CodingThread.id == row.thread_id, CodingThread.owner == (row.owner or ""))
                .first()
            )
            if not thread:
                raise CodingProviderError(401, "Provider token no longer has a thread")
            metadata = _private_metadata(row.metadata_json)
            credential_class = _credential_class(metadata)
            run_id = None
            if credential_class == CREDENTIAL_CLASS_RUN:
                run_id = _require_run_scope(
                    db=db,
                    row=row,
                    thread=thread,
                    metadata=metadata,
                    request_scope=request_scope,
                )
            context = ProviderContext(
                token_id=row.id,
                thread_id=thread.id,
                project_id=thread.project_id,
                owner=row.owner or "",
                session_id=thread.session_id,
                capabilities=frozenset(json_loads(row.capabilities_json, [])),
                credential_class=credential_class,
                run_id=run_id,
            )
            row.last_used_at = now
            row.updated_at = now
            db.commit()
            return context
    finally:
        db.close()
    raise CodingProviderError(401, "Invalid provider token")


__all__ = [
    "ALL_CAPABILITIES",
    "CAPABILITY_ALIASES",
    "CAPABILITY_GROUPS",
    "CodingProviderError",
    "CREDENTIAL_CLASS_EXPLICIT",
    "CREDENTIAL_CLASS_RUN",
    "ProviderContext",
    "ProviderRequestScope",
    "PROVIDER_TOOL_PATH",
    "READ_ONLY_CAPABILITIES",
    "RUN_SCOPE_PROJECT_HEADER",
    "RUN_SCOPE_RUN_HEADER",
    "RUN_SCOPE_THREAD_HEADER",
    "SessionLocal",
    "TOKEN_LOOKUP_PREFIX_LEN",
    "TOKEN_PREFIX",
    "authenticate_provider_token",
    "is_provider_tool_bearer",
    "is_provider_tool_path",
    "json_loads",
    "list_provider_tokens",
    "memory_owner",
    "mint_run_provider_token",
    "mint_provider_token",
    "normalize_capabilities",
    "provider_bearer_token_from_header",
    "provider_request_scope_from_headers",
    "require_capability",
    "revoke_provider_token",
    "strip_reserved_metadata",
    "strip_cross_thread_metadata",
    "token_dict",
]
