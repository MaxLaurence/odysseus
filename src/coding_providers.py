"""Agent -> provider -> model -> effort resolution for Code Station.

The Code Station UI lets a user pick, per coding thread: an **agent** (harness),
a **provider**, a **model**, and an **effort** level. This module is the backend
for the second/third/fourth selectors and for the create-time default cascade.

A "provider" is one of:
  - ``subscription:<claude|codex>`` — the owner's managed CLI OAuth login
    (``auth_mode="subscription"``); only offered for the claude/codex harnesses.
  - ``endpoint:<id>`` — a configured Odysseus ``ModelEndpoint`` api-key
    (``auth_mode="endpoint"``); offered for every harness.

Defaults cascade thread-arg > project default > global setting.
"""

from __future__ import annotations

import json
from typing import Any

from core.database import ModelEndpoint
from src.auth_helpers import owner_filter
from src.coding_auth_service import PROVIDERS as SUBSCRIPTION_PROVIDERS
from src.coding_auth_service import normalize_provider, subscription_logged_in
from src.coding_effort import EFFORT_LEVELS, effort_supported, normalize_effort
from src.settings import get_setting

AUTH_MODES: tuple[str, ...] = ("none", "endpoint", "subscription")

# Harnesses that can authenticate via a managed subscription login.
SUBSCRIPTION_HARNESSES: frozenset[str] = frozenset(SUBSCRIPTION_PROVIDERS)

# Suggested models for subscription providers. These are convenience suggestions for
# the picker only — the model field accepts free text, so a custom/newer id always
# works (the CLI validates). Claude accepts aliases (opus/sonnet/haiku) or full ids.
_SUBSCRIPTION_MODELS: dict[str, list[str]] = {
    "claude": ["opus", "sonnet", "haiku"],
    "codex": ["gpt-5-codex", "gpt-5", "o4-mini"],
}


def effort_choices() -> list[str]:
    return list(EFFORT_LEVELS)


def _endpoint_models(endpoint: ModelEndpoint) -> list[str]:
    models: list[str] = []
    for attr in ("pinned_models", "cached_models"):
        raw = getattr(endpoint, attr, None)
        try:
            values = json.loads(raw) if raw else []
        except Exception:
            values = []
        if isinstance(values, list):
            for model in values:
                if isinstance(model, str) and model and model not in models:
                    models.append(model)
    return models


def list_providers_for_harness(db, owner: str | None, harness_id: str | None) -> dict[str, Any]:
    """Return the provider catalog for a harness: subscription option (if applicable)
    plus the owner's configured LLM ModelEndpoints, each with its model list."""
    harness = (harness_id or "").strip().lower()
    providers: list[dict[str, Any]] = []

    sub_provider = normalize_provider(harness)
    if harness in SUBSCRIPTION_HARNESSES and sub_provider:
        providers.append({
            "id": f"subscription:{sub_provider}",
            "kind": "subscription",
            "provider": sub_provider,
            "label": f"{sub_provider.title()} subscription",
            "auth_mode": "subscription",
            "endpoint_id": "",
            "logged_in": subscription_logged_in(db, owner, sub_provider),
            "models": list(_SUBSCRIPTION_MODELS.get(sub_provider, [])),
            "allow_custom_model": True,
        })

    query = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
    if owner:
        query = owner_filter(query, ModelEndpoint, owner)
    for endpoint in query.order_by(ModelEndpoint.created_at).all():
        if (endpoint.model_type or "llm") != "llm":
            continue
        providers.append({
            "id": f"endpoint:{endpoint.id}",
            "kind": "endpoint",
            "provider": "endpoint",
            "label": endpoint.name or endpoint.base_url,
            "auth_mode": "endpoint",
            "endpoint_id": endpoint.id,
            "base_url": endpoint.base_url,
            "logged_in": True,
            "models": _endpoint_models(endpoint),
            "allow_custom_model": True,
        })

    return {
        "harness_id": harness,
        "providers": providers,
        "effort_levels": list(EFFORT_LEVELS),
        "effort_supported": effort_supported(harness),
    }


# --- create-time default cascade (thread arg > project default > global setting) ----

def cascade_effort(arg: str | None, project: Any, *, global_default: str | None = None) -> str:
    candidates = [
        arg,
        getattr(project, "default_effort", None),
        global_default if global_default is not None else get_setting("coding_default_effort", ""),
    ]
    for candidate in candidates:
        level = normalize_effort(candidate)
        if level:
            return level
    return ""


def cascade_auth_mode(arg: str | None, project: Any, *, global_default: str | None = None) -> str:
    fallback = global_default if global_default is not None else get_setting("coding_default_auth_mode", "none")
    for candidate in (arg, getattr(project, "default_auth_mode", None), fallback):
        if candidate is None:
            continue
        value = str(candidate).strip().lower()
        if value in AUTH_MODES:
            return value
    return "none"


def normalize_auth_mode(value: str | None) -> str:
    cleaned = (value or "").strip().lower()
    return cleaned if cleaned in AUTH_MODES else "none"


def expand_provider_selector(provider: str | None) -> dict[str, str] | None:
    """Map a UI ``provider`` selector id to (auth_mode, model_endpoint_id).

    Accepts ``"subscription:claude"``, ``"endpoint:<id>"``, or a bare endpoint id /
    "subscription". Returns None when the selector is empty (caller keeps existing)."""
    raw = (provider or "").strip()
    if not raw:
        return None
    if raw.startswith("subscription:") or raw == "subscription":
        return {"auth_mode": "subscription", "model_endpoint_id": ""}
    if raw.startswith("endpoint:"):
        return {"auth_mode": "endpoint", "model_endpoint_id": raw.split(":", 1)[1]}
    if raw in ("none", "endpoint"):
        return {"auth_mode": raw, "model_endpoint_id": ""}
    # Bare value: treat as an endpoint id.
    return {"auth_mode": "endpoint", "model_endpoint_id": raw}
