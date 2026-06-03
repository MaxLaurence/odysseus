"""Shared helpers for Coding Station provider tool handlers."""

from __future__ import annotations

import json
import re
import sys
from typing import Any

from core.database import SessionLocal as _DEFAULT_SESSION_LOCAL
from src.coding_provider_tokens import CodingProviderError

MAX_MEMORY_TEXT = 20_000
MAX_MESSAGE_TEXT = 200_000

SECRET_RESULT_KEYS = {
    "api_key",
    "authorization",
    "headers",
    "password",
    "secret",
    "token",
    "token_hash",
}

INTERNAL_RUN_KEYS = {"tmux_session", "run_dir", "log_path"}

SAFE_RESULT_KEYS = {
    "thread_id",
    "project_id",
    "run_id",
    "status",
    "name",
    "title",
    "token_capabilities",
}

SECRET_RESULT_TERMS = {
    "auth",
    "authorization",
    "bearer",
    "cookie",
    "credential",
    "credentials",
    "header",
    "headers",
    "jwt",
    "key",
    "passwd",
    "password",
    "secret",
    "token",
}

INTERNAL_RESULT_TERMS = {
    "dir",
    "directory",
    "file",
    "filepath",
    "log",
    "logs",
    "path",
    "session",
    "state",
}

_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]+")

# Facade-compatible attribute for tests and local monkeypatches.
SessionLocal = _DEFAULT_SESSION_LOCAL


def session_local():
    facade = sys.modules.get("src.coding_provider")
    facade_session = getattr(facade, "SessionLocal", None) if facade is not None else None
    if facade_session is not None and facade_session is not _DEFAULT_SESSION_LOCAL:
        return facade_session

    tools = sys.modules.get("src.coding_provider_tools")
    tools_session = getattr(tools, "SessionLocal", None) if tools is not None else None
    if tools_session is not None and tools_session is not _DEFAULT_SESSION_LOCAL:
        return tools_session

    if SessionLocal is not _DEFAULT_SESSION_LOCAL:
        return SessionLocal

    from core import database

    return database.SessionLocal


def _normalized_result_key(key: Any) -> tuple[str, tuple[str, ...], str]:
    normalized = _NON_ALNUM_RE.sub("_", _CAMEL_BOUNDARY_RE.sub("_", str(key))).strip("_").lower()
    parts = tuple(part for part in normalized.split("_") if part)
    return normalized, parts, "".join(parts)


def is_sensitive_result_key(key: Any) -> bool:
    normalized, parts, compact = _normalized_result_key(key)
    if not normalized or normalized in SAFE_RESULT_KEYS:
        return False
    if normalized in SECRET_RESULT_KEYS or normalized in INTERNAL_RUN_KEYS:
        return True
    if SECRET_RESULT_TERMS.intersection(parts):
        return True
    if INTERNAL_RESULT_TERMS.intersection(parts):
        return True
    return compact in {
        "apikey",
        "accesstoken",
        "bearertoken",
        "refreshtoken",
        "idtoken",
        "tokenhash",
        "filepath",
        "statepath",
        "logpath",
        "tmuxsession",
    }


def sanitize_result(value: Any) -> Any:
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            if is_sensitive_result_key(key):
                continue
            clean[key] = sanitize_result(item)
        return clean
    if isinstance(value, list):
        return [sanitize_result(item) for item in value]
    return value


def int_arg(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def str_arg(args: dict[str, Any], key: str, default: str | None = None) -> str | None:
    """Stripped string for ``args[key]``, or ``default`` when absent/blank.

    Collapses the ``str(args.get(k)).strip() if args.get(k) else None`` pattern
    that otherwise repeats across every provider-tool write handler."""
    value = args.get(key)
    if value is None:
        return default
    text = str(value).strip()
    return text or default


async def manage_coding(action: str, owner: str, args: dict[str, Any]) -> dict[str, Any]:
    from src.tool_implementations import do_manage_coding

    payload = dict(args)
    payload["action"] = action
    result = await do_manage_coding(json.dumps(payload), owner=owner)
    if isinstance(result, dict) and result.get("error"):
        raise CodingProviderError(int(result.get("status_code") or 400), str(result.get("error")))
    return result


__all__ = [
    "INTERNAL_RUN_KEYS",
    "INTERNAL_RESULT_TERMS",
    "MAX_MEMORY_TEXT",
    "MAX_MESSAGE_TEXT",
    "SAFE_RESULT_KEYS",
    "SECRET_RESULT_TERMS",
    "SECRET_RESULT_KEYS",
    "SessionLocal",
    "int_arg",
    "is_sensitive_result_key",
    "manage_coding",
    "sanitize_result",
    "session_local",
    "str_arg",
]
