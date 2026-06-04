"""Resolve a usable subscription OAuth credential for app-wide LLM calls.

The per-owner subscription LOGIN is handled by ``src/coding_auth_service.py`` (it runs
``claude auth login`` / ``codex login`` in isolated config dirs). THIS module turns the
stored login into a live access token + the provider-specific call parameters used by the
subscription gateway (``src/coding_oauth_gateway.py``):

  - **anthropic / Claude** — the ``accessToken`` from the ``.credentials.json`` that
    ``claude auth login`` writes into the isolated ``CLAUDE_CONFIG_DIR`` (the
    ``claudeAiOauth`` blob). Refreshed via ``platform.claude.com/v1/oauth/token`` when
    near expiry. Used as ``Authorization: Bearer`` + ``anthropic-beta:
    claude-code-20250219,oauth-2025-04-20`` against ``https://api.anthropic.com``.
  - **openai / Codex (ChatGPT subscription)** — the ``access_token`` from the isolated
    ``CODEX_HOME/auth.json``; refreshed via ``auth.openai.com/oauth/token`` with the
    stored ``refresh_token`` when near expiry. Used as ``Authorization: Bearer`` +
    ``chatgpt-account-id`` against the Codex Responses backend
    ``https://chatgpt.com/backend-api/codex``.

These are UNOFFICIAL uses of subscription tokens (the providers built them for their own
CLIs); they can change. Keep all provider-specific constants here so a break is a
localized fix.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from core.database import SessionLocal
from src.coding_auth_service import (
    _codex_auth_file,
    _load_row,
    _restrict_file,
    auth_config_dir,
    claude_oauth_blob,
    normalize_provider,
    read_claude_credentials_raw,
    write_claude_credentials_raw,
)

logger = logging.getLogger(__name__)

ANTHROPIC_BASE = "https://api.anthropic.com"
# Claude Code subscription OAuth requires BOTH beta flags on every request (the CLI
# sends them together). Sending only oauth-2025-04-20 can get the token rejected.
ANTHROPIC_OAUTH_BETA = "claude-code-20250219,oauth-2025-04-20"

# Codex / ChatGPT-subscription constants (public Codex CLI client).
OPENAI_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OPENAI_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_RESPONSES_BASE = "https://chatgpt.com/backend-api/codex"
CODEX_ORIGINATOR = "codex_cli_rs"

_REFRESH_SKEW = 120  # refresh when within this many seconds of expiry


def _jwt_payload(token: str) -> dict[str, Any]:
    """Best-effort decode of a JWT payload (no signature verification)."""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part.encode()))
    except Exception:
        return {}


def _token_expired(access_token: str) -> bool:
    exp = _jwt_payload(access_token).get("exp")
    if not isinstance(exp, (int, float)):
        return False  # can't tell -> assume valid; the gateway refreshes on a 401
    return time.time() >= (float(exp) - _REFRESH_SKEW)


def _chatgpt_account_id(tokens: dict[str, Any]) -> str:
    """Find the ChatGPT account id from auth.json tokens (explicit field or JWT claim)."""
    for key in ("account_id", "chatgpt_account_id"):
        val = tokens.get(key)
        if isinstance(val, str) and val:
            return val
    for tok_key in ("id_token", "access_token"):
        claims = _jwt_payload(tokens.get(tok_key) or "")
        auth_claim = claims.get("https://api.openai.com/auth")
        if isinstance(auth_claim, dict):
            acct = auth_claim.get("chatgpt_account_id") or auth_claim.get("account_id")
            if isinstance(acct, str) and acct:
                return acct
    return ""


# --- Anthropic / Claude -----------------------------------------------------
# `claude auth login` stores the OAuth access+refresh tokens (claudeAiOauth blob) in the
# isolated CLAUDE_CONFIG_DIR: a `.credentials.json` file on Linux, or the macOS login
# Keychain (read/written via the store abstraction in coding_auth_service). We read the
# access token from whichever store holds it and refresh it ourselves when near expiry,
# exactly like Pi — so calls go DIRECT to the Messages API with the Claude Code identity
# (no CLI wrap), and refreshed tokens are written back so the claude CLI sees them too.
ANTHROPIC_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
ANTHROPIC_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"


def _blob_access(blob: dict[str, Any]) -> str:
    for key in ("accessToken", "access_token", "token"):
        val = blob.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _claude_token(owner: str | None) -> str:
    """Synchronous read of the stored access token (no refresh)."""
    _store, data = read_claude_credentials_raw(auth_config_dir(owner, "claude", create=False))
    return _blob_access(claude_oauth_blob(data))


async def _refresh_claude(config_dir: Path, store: str, data: dict[str, Any], blob: dict[str, Any], refresh_token: str) -> str:
    body = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": ANTHROPIC_OAUTH_CLIENT_ID}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(ANTHROPIC_TOKEN_URL, json=body)
        if resp.status_code != 200:
            logger.warning("Claude token refresh failed: %s %s", resp.status_code, resp.text[:200])
            return ""
        payload = resp.json()
    except Exception:
        logger.debug("Claude token refresh request errored", exc_info=True)
        return ""
    new_access = (payload.get("access_token") or "").strip()
    if not new_access:
        return ""
    blob["accessToken"] = new_access
    if payload.get("refresh_token"):
        blob["refreshToken"] = payload["refresh_token"]  # refresh tokens rotate — persist
    if payload.get("expires_in"):
        blob["expiresAt"] = int(time.time() * 1000) + int(payload["expires_in"]) * 1000
    # Write back to the SAME store (file or Keychain), preserving wrapped/flat shape.
    out = data if blob is data else {"claudeAiOauth": blob}
    write_claude_credentials_raw(config_dir, store, out)
    return new_access


async def _claude_access(owner: str | None) -> str:
    """Live access token, refreshed if near expiry."""
    config_dir = auth_config_dir(owner, "claude", create=False)
    store, data = read_claude_credentials_raw(config_dir)
    if not data:
        return ""
    blob = claude_oauth_blob(data)
    access = _blob_access(blob)
    refresh = (blob.get("refreshToken") or blob.get("refresh_token") or "").strip()
    expires_at = blob.get("expiresAt") or blob.get("expires_at")
    if access and refresh and isinstance(expires_at, (int, float)):
        if time.time() * 1000 >= (float(expires_at) - _REFRESH_SKEW * 1000):
            access = await _refresh_claude(config_dir, store, data, blob, refresh) or access
    return access


# --- OpenAI / Codex (ChatGPT subscription) ----------------------------------

def _read_codex_auth(owner: str | None) -> tuple[Path, dict[str, Any]]:
    auth_file = _codex_auth_file(auth_config_dir(owner, "codex", create=False))
    try:
        data = json.loads(auth_file.read_text())
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    return auth_file, data


async def _refresh_codex(auth_file: Path, data: dict[str, Any]) -> dict[str, Any]:
    """Refresh the Codex access_token via the OAuth token endpoint, write back auth.json,
    return the updated tokens dict. On failure, returns the existing tokens unchanged."""
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    refresh_token = (tokens.get("refresh_token") or "").strip()
    if not refresh_token:
        return tokens
    body = {
        "client_id": OPENAI_OAUTH_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": "openid profile email",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(OPENAI_TOKEN_URL, json=body)
        if resp.status_code != 200:
            logger.warning("Codex token refresh failed: %s %s", resp.status_code, resp.text[:200])
            return tokens
        payload = resp.json()
    except Exception:
        logger.debug("Codex token refresh request errored", exc_info=True)
        return tokens
    new_tokens = dict(tokens)
    for key in ("access_token", "id_token"):
        if payload.get(key):
            new_tokens[key] = payload[key]
    # Refresh tokens may rotate — persist the new one if returned.
    if payload.get("refresh_token"):
        new_tokens["refresh_token"] = payload["refresh_token"]
    data["tokens"] = new_tokens
    try:
        from datetime import datetime
        data["last_refresh"] = datetime.utcnow().isoformat() + "Z"
        auth_file.write_text(json.dumps(data))
        _restrict_file(auth_file)
    except Exception:
        logger.debug("Failed to persist refreshed Codex auth.json", exc_info=True)
    return new_tokens


async def _codex_access(owner: str | None) -> dict[str, str]:
    auth_file, data = _read_codex_auth(owner)
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    access = (tokens.get("access_token") or "").strip()
    if not access:
        return {}
    if _token_expired(access):
        tokens = await _refresh_codex(auth_file, data)
        access = (tokens.get("access_token") or "").strip()
    if not access:
        return {}
    return {"access_token": access, "account_id": _chatgpt_account_id(tokens)}


# --- public API -------------------------------------------------------------

async def resolve_subscription_credential(owner: str | None, provider: str) -> dict[str, Any] | None:
    """Return the live call parameters for an owner's subscription, or None if not
    logged in. Shape:
      anthropic -> {provider, base_url, access_token, headers:{anthropic-beta:..}}
      openai    -> {provider, base_url, access_token, headers:{chatgpt-account-id, originator}}
    """
    prov = normalize_provider(provider)
    if prov == "claude":
        token = await _claude_access(owner)
        if not token:
            return None
        return {
            "provider": "anthropic",
            "base_url": ANTHROPIC_BASE,
            "access_token": token,
            "headers": {"anthropic-beta": ANTHROPIC_OAUTH_BETA},
        }
    if prov == "codex":
        creds = await _codex_access(owner)
        if not creds.get("access_token"):
            return None
        headers = {"originator": CODEX_ORIGINATOR}
        if creds.get("account_id"):
            headers["chatgpt-account-id"] = creds["account_id"]
        return {
            "provider": "openai",
            "base_url": CODEX_RESPONSES_BASE,
            "access_token": creds["access_token"],
            "headers": headers,
        }
    return None
