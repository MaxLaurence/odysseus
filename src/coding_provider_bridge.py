"""Run-scoped provider-tool bridge for Coding Station runtime launches."""

from __future__ import annotations

import hashlib
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import urlsplit, urlunsplit


PROVIDER_BRIDGE_ENV_KEYS = (
    "ODYSSEUS_TOOL_URL",
    "ODYSSEUS_TOOL_TOKEN",
    "ODYSSEUS_THREAD_ID",
    "ODYSSEUS_PROJECT_ID",
    "ODYSSEUS_RUN_ID",
)
PROVIDER_TOOL_PATH = "/api/coding/provider/tool"
PROVIDER_BASE_PATH = "/api/coding/provider"
PROVIDER_BASE_URL_ENV_KEYS = (
    "ODYSSEUS_PUBLIC_BASE_URL",
    "ODYSSEUS_BASE_URL",
    "APP_BASE_URL",
    "PUBLIC_BASE_URL",
)
DEFAULT_APP_PORT = "7000"


@dataclass(frozen=True)
class ProviderBridgeScope:
    owner: str
    project_id: str
    thread_id: str
    run_id: str


@dataclass(frozen=True)
class ProviderBridgeRecord:
    token_hash: str
    owner: str
    project_id: str
    thread_id: str
    run_id: str
    created_at: float
    backend_token_id: str = ""
    revoked_at: float | None = None

    @property
    def scope(self) -> ProviderBridgeScope:
        return ProviderBridgeScope(
            owner=self.owner,
            project_id=self.project_id,
            thread_id=self.thread_id,
            run_id=self.run_id,
        )


class CodingProviderBridgeService:
    """Runtime authority for run-scoped provider-tool credentials."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._tokens: dict[str, ProviderBridgeRecord] = {}
        self._run_tokens: dict[str, str] = {}

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create_run_env(self, scope: ProviderBridgeScope) -> dict[str, str]:
        token = self.create_run_token(scope)
        return {
            "ODYSSEUS_TOOL_URL": provider_tool_url(),
            "ODYSSEUS_TOOL_TOKEN": token,
            "ODYSSEUS_THREAD_ID": scope.thread_id,
            "ODYSSEUS_PROJECT_ID": scope.project_id,
            "ODYSSEUS_RUN_ID": scope.run_id,
            # Point an `ody` invoked inside a pane at the right control socket + owner.
            "ODYSSEUS_ODY_SOCKET": ody_socket_path(),
            "ODYSSEUS_OWNER": scope.owner,
        }

    def create_run_token(self, scope: ProviderBridgeScope) -> str:
        backend = _mint_backend_run_token(scope)
        token = str(backend.get("token") or "")
        backend_token_id = str(backend.get("token_id") or "")
        if not token.startswith("ody_cp_") or not backend_token_id:
            raise RuntimeError("Provider bridge mint did not return a usable provider HTTP token")

        token_hash = self._hash_token(token)
        record = ProviderBridgeRecord(
            token_hash=token_hash,
            owner=scope.owner,
            project_id=scope.project_id,
            thread_id=scope.thread_id,
            run_id=scope.run_id,
            created_at=time.time(),
            backend_token_id=backend_token_id,
        )
        with self._lock:
            old_hash = self._run_tokens.get(scope.run_id)
            if old_hash:
                old = self._tokens.get(old_hash)
                if old and old.revoked_at is None:
                    _revoke_backend_run_token(old)
                    self._tokens[old_hash] = _revoked_record(old)
            self._tokens[token_hash] = record
            self._run_tokens[scope.run_id] = token_hash
        return token

    def validate_token(self, token: str, run_id: str | None = None) -> ProviderBridgeScope | None:
        token_hash = self._hash_token(token or "")
        with self._lock:
            record = self._tokens.get(token_hash)
        if not record or record.revoked_at is not None:
            return None
        if run_id and not secrets.compare_digest(record.run_id, run_id):
            return None
        return record.scope

    def revoke_run_token(self, *, token: str | None = None, run_id: str | None = None) -> bool:
        token_hash = self._hash_token(token) if token else None
        with self._lock:
            if token_hash is None and run_id:
                token_hash = self._run_tokens.get(run_id)
            if not token_hash:
                return False
            record = self._tokens.get(token_hash)
            if not record or record.revoked_at is not None:
                return False
            _revoke_backend_run_token(record)
            self._tokens[token_hash] = _revoked_record(record)
            if self._run_tokens.get(record.run_id) == token_hash:
                self._run_tokens.pop(record.run_id, None)
            return True

    def metadata_for_env(self, env: dict[str, str]) -> dict[str, Any]:
        return {
            "enabled": True,
            "url": env.get("ODYSSEUS_TOOL_URL", ""),
            "thread_id": env.get("ODYSSEUS_THREAD_ID", ""),
            "project_id": env.get("ODYSSEUS_PROJECT_ID", ""),
            "run_id": env.get("ODYSSEUS_RUN_ID", ""),
            "token_issued": bool(env.get("ODYSSEUS_TOOL_TOKEN")),
        }


_SERVICE = CodingProviderBridgeService()


def get_provider_bridge_service() -> CodingProviderBridgeService:
    return _SERVICE


def provider_tool_url() -> str:
    explicit_tool_url = _env_value("ODYSSEUS_TOOL_URL")
    if explicit_tool_url:
        return _normalize_provider_tool_url(explicit_tool_url)

    for key in PROVIDER_BASE_URL_ENV_KEYS:
        base_url = _env_value(key)
        if base_url:
            return _normalize_provider_tool_url(base_url, treat_as_app_base=True)

    # ODYSSEUS_PORT is the real port the backend bound to (the macOS launcher picks
    # a free port dynamically and sets it). Prefer it — defaulting to 7000 points
    # the harness at macOS AirPlay Receiver, which squats on :7000 and 403s.
    port = _env_value("ODYSSEUS_PORT") or _env_value("APP_PORT") or DEFAULT_APP_PORT
    return _normalize_provider_tool_url(f"http://localhost:{port}")


def _env_value(key: str) -> str:
    return (os.environ.get(key) or "").strip()


def _normalize_provider_tool_url(url: str, *, treat_as_app_base: bool = False) -> str:
    value = url.strip().rstrip("/")
    if not value:
        return value

    parsed = urlsplit(value)
    path = parsed.path.rstrip("/")
    if not parsed.scheme or not parsed.netloc:
        return value

    if path in ("", PROVIDER_BASE_PATH, PROVIDER_TOOL_PATH):
        path = PROVIDER_TOOL_PATH
    elif treat_as_app_base:
        path = f"{path}{PROVIDER_TOOL_PATH}" if path.startswith("/") else PROVIDER_TOOL_PATH

    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def scripts_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "scripts"


def ody_socket_path() -> str:
    """Resolve the ody control-plane socket path: ``$ODYSSEUS_ODY_SOCKET`` else
    ``<data_dir>/ody.sock``.

    Mirrors ``src/coding_socket.default_socket_path`` / ``src/coding_cli._socket_path``
    without importing the runtime (avoids an import cycle): resolve ``DATA_DIR``
    from ``core.constants``, falling back to ``./data/ody.sock``.
    """
    explicit = (os.environ.get("ODYSSEUS_ODY_SOCKET") or "").strip()
    if explicit:
        return explicit
    try:
        from core.constants import DATA_DIR

        base = (DATA_DIR or "").strip()
    except Exception:
        base = ""
    if not base:
        base = os.path.join(os.getcwd(), "data")
    return os.path.join(base, "ody.sock")


PROVIDER_TOOL_URL_FILENAME = "ody-tool-url"


def provider_tool_url_file() -> str:
    """Stable path where the running backend publishes its CURRENT provider-tool
    URL. Lives next to the ody socket (``<data_dir>/ody-tool-url``) — a path that
    does NOT change across backend restarts, even though the backend's HTTP port
    does. Long-lived agent runs re-read it so a restart on a new port doesn't
    orphan their ``odysseus-tool`` calls with a stale baked-in ``ODYSSEUS_TOOL_URL``.
    """
    return str(Path(ody_socket_path()).parent / PROVIDER_TOOL_URL_FILENAME)


def read_published_provider_tool_url() -> str:
    """Return the backend's currently-published provider-tool URL, or "" if none.

    Read fresh on every call (no caching) so a backend restart on a new port is
    picked up immediately by already-running agent runs.
    """
    try:
        with open(provider_tool_url_file(), encoding="utf-8") as handle:
            value = handle.read().strip()
    except OSError:
        return ""
    return _normalize_provider_tool_url(value) if value else ""


def publish_provider_tool_url(url: str | None = None) -> str:
    """Publish the backend's current provider-tool URL to the stable file so
    running agents can re-resolve the (dynamic) port after a restart. Call once at
    backend startup. Best-effort: returns the URL written, or "" on failure.

    Resolves the URL from the server's own env (``provider_tool_url()`` → the
    current ``ODYSSEUS_PORT``) unless an explicit ``url`` is given. Writes
    atomically (temp + ``os.replace``) so a concurrent reader never sees a partial
    file.
    """
    resolved = _normalize_provider_tool_url(url) if url else provider_tool_url()
    if not resolved:
        return ""
    path = Path(provider_tool_url_file())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(resolved, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        return ""
    return resolved


def unpublish_provider_tool_url() -> None:
    """Remove the published URL file on graceful shutdown (best-effort). A new
    startup overwrites it anyway, so a leftover file from a crash is harmless."""
    try:
        os.remove(provider_tool_url_file())
    except OSError:
        pass


def bridge_env_status() -> dict[str, Any]:
    env = {key: os.environ.get(key, "") for key in PROVIDER_BRIDGE_ENV_KEYS}
    return {
        **env,
        "ODYSSEUS_TOOL_TOKEN": "***" if env.get("ODYSSEUS_TOOL_TOKEN") else "",
        "configured": all(bool(env.get(key)) for key in PROVIDER_BRIDGE_ENV_KEYS),
    }


def with_scripts_on_path(env: dict[str, str]) -> dict[str, str]:
    result = dict(env)
    path = result.get("PATH", "")
    script_path = str(scripts_dir())
    parts = [part for part in path.split(os.pathsep) if part]
    if not parts or parts[0] != script_path:
        parts = [part for part in parts if part != script_path]
        result["PATH"] = script_path + (os.pathsep + os.pathsep.join(parts) if parts else "")
    return result


def _mint_backend_run_token(scope: ProviderBridgeScope) -> dict[str, str]:
    from src.coding_provider_tokens import TOKEN_PREFIX, mint_run_provider_token

    result = mint_run_provider_token(
        owner=scope.owner,
        thread_id=scope.thread_id,
        project_id=scope.project_id,
        run_id=scope.run_id,
    )
    if not isinstance(result, dict):
        raise RuntimeError("Provider bridge mint did not return a token payload")
    provider_token = result.get("provider_token") or {}
    token = str(result.get("token") or "")
    token_id = str((provider_token or {}).get("id") or "")
    if not token.startswith(TOKEN_PREFIX) or not token_id:
        raise RuntimeError("Provider bridge mint did not return a usable provider HTTP token")
    return {"token": token, "token_id": token_id}


def _revoke_backend_run_token(record: ProviderBridgeRecord) -> None:
    if not record.backend_token_id:
        return
    try:
        from src.coding_provider import revoke_provider_token

        revoke_provider_token(record.owner, record.backend_token_id)
    except Exception:
        pass


def _revoked_record(record: ProviderBridgeRecord) -> ProviderBridgeRecord:
    return ProviderBridgeRecord(
        token_hash=record.token_hash,
        owner=record.owner,
        project_id=record.project_id,
        thread_id=record.thread_id,
        run_id=record.run_id,
        created_at=record.created_at,
        backend_token_id=record.backend_token_id,
        revoked_at=time.time(),
    )
