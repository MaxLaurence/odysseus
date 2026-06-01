"""Odysseus model config derivation for coding terminals."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import urlparse, urlunparse

from core.database import CodingModelConfigSnapshot, ModelEndpoint
from src.auth_helpers import owner_filter
from src.endpoint_resolver import normalize_base
from src.settings import get_user_setting, load_settings


def _json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except Exception:
        return fallback


def _first_cached_model(endpoint: ModelEndpoint) -> str:
    models = _json_loads(getattr(endpoint, "cached_models", None), [])
    if isinstance(models, list) and models:
        return str(models[0])
    return ""


def _ollama_host_from_endpoint(endpoint_url: str) -> str:
    parsed = urlparse(endpoint_url or "")
    host = parsed.hostname or ""
    if not host:
        return ""
    if parsed.port != 11434 and "ollama" not in host.lower():
        return ""
    return urlunparse((parsed.scheme or "http", parsed.netloc, "", "", "", "")).rstrip("/")


def build_safe_cli_env(endpoint_url: str, model: str | None) -> dict[str, str]:
    """Build JSON-safe CLI env values without secrets."""
    endpoint_url = (endpoint_url or "").strip().rstrip("/")
    model = (model or "").strip()
    env: dict[str, str] = {}
    if endpoint_url:
        env["OPENAI_BASE_URL"] = endpoint_url
        env["OPENAI_API_BASE"] = endpoint_url
        ollama_host = _ollama_host_from_endpoint(endpoint_url)
        if ollama_host:
            env["OLLAMA_HOST"] = ollama_host
    if model:
        env["OPENAI_MODEL"] = model
    return env


def _endpoint_config(endpoint: ModelEndpoint | None, model: str | None = "") -> dict[str, Any]:
    if not endpoint:
        model = (model or "").strip()
        return {
            "endpoint_id": "",
            "endpoint_url": "",
            "model": model,
            "env": build_safe_cli_env("", model),
        }
    base = normalize_base(endpoint.base_url)
    selected_model = (model or "").strip() or _first_cached_model(endpoint)
    return {
        "endpoint_id": endpoint.id,
        "endpoint_url": base,
        "model": selected_model,
        "env": build_safe_cli_env(base, selected_model),
    }


def derive_odysseus_model_config(db, owner: str | None = "") -> dict[str, Any]:
    """Resolve the current Odysseus default endpoint/model for a caller."""
    owner = owner or ""
    settings = load_settings()
    endpoint_id = (
        get_user_setting("default_endpoint_id", owner, settings.get("default_endpoint_id", ""))
        or ""
    ).strip()
    model = (
        get_user_setting("default_model", owner, settings.get("default_model", ""))
        or ""
    ).strip()

    endpoint = None
    if endpoint_id:
        query = db.query(ModelEndpoint).filter(
            ModelEndpoint.id == endpoint_id,
            ModelEndpoint.is_enabled == True,
        )
        if owner:
            query = owner_filter(query, ModelEndpoint, owner)
        endpoint = query.first()

    if endpoint is None and not endpoint_id:
        query = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)
        if owner:
            query = owner_filter(query, ModelEndpoint, owner)
        endpoint = query.order_by(ModelEndpoint.created_at).first()

    config = _endpoint_config(endpoint, model)
    config["source"] = "odysseus-default"
    if endpoint_id and not endpoint:
        config["endpoint_id"] = endpoint_id
    return config


def thread_model_config(db, thread) -> dict[str, Any]:
    """Resolve a thread's configured endpoint/model into safe response data."""
    endpoint = None
    endpoint_id = (getattr(thread, "model_endpoint_id", None) or "").strip()
    if endpoint_id:
        query = db.query(ModelEndpoint).filter(
            ModelEndpoint.id == endpoint_id,
            ModelEndpoint.is_enabled == True,
        )
        owner = getattr(thread, "owner", None) or ""
        if owner:
            query = owner_filter(query, ModelEndpoint, owner)
        endpoint = query.first()
    config = _endpoint_config(endpoint, getattr(thread, "model", "") or "")
    if endpoint_id and not endpoint:
        config["endpoint_id"] = endpoint_id
    config["source"] = "thread"
    return config


def build_launch_env(db, owner: str | None, endpoint_id: str | None, model: str | None) -> dict[str, str]:
    """Build per-run env, including secrets only for process injection."""
    endpoint = None
    endpoint_id = (endpoint_id or "").strip()
    if endpoint_id:
        query = db.query(ModelEndpoint).filter(
            ModelEndpoint.id == endpoint_id,
            ModelEndpoint.is_enabled == True,
        )
        if owner:
            query = owner_filter(query, ModelEndpoint, owner)
        endpoint = query.first()
    config = _endpoint_config(endpoint, model or "")
    env = dict(config["env"])
    api_key = getattr(endpoint, "api_key", None) if endpoint else None
    if api_key:
        env["OPENAI_API_KEY"] = api_key
        env["CODEX_API_KEY"] = api_key
        host = (urlparse(config["endpoint_url"]).hostname or "").lower()
        if host.endswith("anthropic.com"):
            env["ANTHROPIC_API_KEY"] = api_key
    return env


def apply_current_model_config(db, thread, owner: str | None = "") -> tuple[dict[str, Any], CodingModelConfigSnapshot]:
    """Snapshot a thread's current config, then set it to Odysseus default."""
    previous = {
        "endpoint_id": getattr(thread, "model_endpoint_id", None) or "",
        "model": getattr(thread, "model", None) or "",
    }
    current = derive_odysseus_model_config(db, owner)
    snapshot = CodingModelConfigSnapshot(
        id=str(uuid.uuid4()),
        thread_id=thread.id,
        owner=owner or getattr(thread, "owner", None),
        source="odysseus-default",
        endpoint_id=current.get("endpoint_id") or "",
        model=current.get("model") or "",
        payload_json=json.dumps({"previous": previous, "applied": current}),
    )
    db.add(snapshot)
    thread.model_endpoint_id = current.get("endpoint_id") or ""
    thread.model = current.get("model") or ""
    thread.updated_at = datetime.utcnow()
    return current, snapshot


def restore_previous_model_config(db, thread, owner: str | None = "") -> tuple[dict[str, Any], CodingModelConfigSnapshot] | None:
    """Restore the latest un-restored snapshot for a thread."""
    query = db.query(CodingModelConfigSnapshot).filter(
        CodingModelConfigSnapshot.thread_id == thread.id,
        CodingModelConfigSnapshot.restored_at == None,  # noqa: E711
    )
    if owner:
        query = query.filter(CodingModelConfigSnapshot.owner == owner)
    snapshot = query.order_by(CodingModelConfigSnapshot.created_at.desc()).first()
    if not snapshot:
        return None

    payload = _json_loads(snapshot.payload_json, {})
    previous = payload.get("previous") if isinstance(payload, dict) else {}
    if not isinstance(previous, dict):
        previous = {}
    thread.model_endpoint_id = previous.get("endpoint_id") or ""
    thread.model = previous.get("model") or ""
    thread.updated_at = datetime.utcnow()
    snapshot.restored_at = datetime.utcnow()
    restored = thread_model_config(db, thread)
    restored["source"] = "restored-snapshot"
    return restored, snapshot
