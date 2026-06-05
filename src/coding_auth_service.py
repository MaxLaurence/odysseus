"""Per-owner subscription OAuth login for coding-agent CLIs (Claude, Codex).

Design: Odysseus does NOT reimplement Anthropic/OpenAI OAuth. It *wraps* each CLI's
own login flow, run inside an isolated, per-owner config directory so multiple
Odysseus users never share one OS keychain / ``~/.claude`` / ``~/.codex``:

  - Claude: ``claude auth login --claudeai`` runs the browser OAuth flow and writes
            credentials into the isolated ``CLAUDE_CONFIG_DIR`` (a credentials file on
            Linux, or macOS Keychain plus account metadata in ``.claude.json``). Agent
            launches point Claude at that config dir and never inject the OAuth token
            through process environment.
  - Codex:  ``codex login`` runs the ChatGPT browser OAuth flow (its own localhost
            callback) and writes ``auth.json`` under ``CODEX_HOME``. We point
            ``CODEX_HOME`` at the isolated dir and detect ``auth.json`` to mark
            logged-in; the same ``CODEX_HOME`` is injected at agent launch.

The login program is run through the EXACT same ``${SHELL} -ilc`` wrapper and dtach
PTY transport as a normal coding run, so the packaged .app inherits the login-shell
PATH (where ``claude``/``codex`` live) and the data-dir-with-a-space is handled by
``shlex.quote`` + env (never naive shell concatenation).

Light helpers (``subscription_launch_env``, ``subscription_logged_in``,
``auth_config_dir``) are import-cheap so ``src/coding_model_config.py`` can call them
at launch-env build time without an import cycle (this module never imports the
runtime or model-config modules).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from core.constants import DATA_DIR
from core.database import CodingProviderAuth, SessionLocal
from src.child_process_env import safe_child_env
from src.coding_pty_bridge import CodingPtyBridge, dtach_bin

logger = logging.getLogger(__name__)

PROVIDERS: tuple[str, ...] = ("claude", "codex")

# Login commands per provider (verified against claude 2.1.x / codex 0.13x).
# Claude: `claude auth login` writes credentials into the ISOLATED CLAUDE_CONFIG_DIR
# (.credentials.json) — far more reliable than scraping `setup-token`'s printed token
# from the PTY (which truncated/garbled it). We read the token from that file later.
_LOGIN_COMMAND = {
    "claude": "claude auth login --claudeai",
    "codex": "codex login",
}
# The CLI binary each provider needs on PATH.
_PROVIDER_BIN = {"claude": "claude", "codex": "codex"}

# How long the completion watcher waits for the login to finish (seconds).
_WATCH_TIMEOUT = 600
_WATCH_INTERVAL = 2.0
_AUTH_REDACTION = "[redacted]"
_AUTH_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)(\bauthorization\s*[:=]\s*bearer\s+)([^\s\"'`,;]+)"),
    re.compile(
        r"(?i)(\b(?:access[_-]?token|refresh[_-]?token|id[_-]?token|api[_-]?key|"
        r"client[_-]?secret|password|secret|credential|credentials)\s*[:=]\s*[\"']?)([^\s\"'`,;}]+)"
    ),
)
_AUTH_BARE_SECRET_PATTERNS = (
    re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{12,}"),
    re.compile(r"\bhf_[A-Za-z0-9]{20,}"),
    re.compile(r"\body_(?:cp|tool|oauth|internal)[A-Za-z0-9_:-]*"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
)


def _redact_auth_output_text(text: str) -> str:
    if not text:
        return text
    redacted = text
    for pattern in _AUTH_SECRET_VALUE_PATTERNS:
        redacted = pattern.sub(lambda match: f"{match.group(1)}{_AUTH_REDACTION}", redacted)
    for pattern in _AUTH_BARE_SECRET_PATTERNS:
        redacted = pattern.sub(_AUTH_REDACTION, redacted)
    return redacted


def _redact_auth_output_bytes(data: bytes) -> bytes:
    if not data:
        return data
    return _redact_auth_output_text(data.decode("utf-8", errors="replace")).encode("utf-8")


def normalize_provider(provider: str | None) -> str:
    value = (provider or "").strip().lower()
    return value if value in PROVIDERS else ""


# --- path helpers (import-cheap; safe for launch-env use) -------------------

def owner_slug(owner: str | None) -> str:
    """Filesystem-safe, stable slug for an owner key (which may be an email).

    Never put a raw email into a path; hash it. Empty owner -> a fixed shared slug."""
    key = (owner or "").strip().lower()
    if not key:
        return "shared"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def auth_root() -> Path:
    return Path(DATA_DIR) / "coding_auth"


def auth_config_dir(owner: str | None, provider: str, *, create: bool = True) -> Path:
    prov = normalize_provider(provider) or provider
    path = auth_root() / owner_slug(owner) / prov
    if create:
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, 0o700)
        except Exception:
            pass
    return path


def _claude_credentials_file(config_dir: Path) -> Path:
    return config_dir / ".credentials.json"


def _codex_auth_file(config_dir: Path) -> Path:
    return config_dir / "auth.json"


def _file_has_content(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 2
    except Exception:
        return False


def _restrict_file(path: Path) -> None:
    """Best-effort: ensure a credential/log file is owner-only (0o600). Provider CLIs
    write under the default umask (often 0o644), so we tighten it ourselves."""
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def _write_json_owner_only(path: Path, data: dict[str, Any]) -> None:
    """Atomically write JSON using 0600 for the temp and final file.

    These files can carry account metadata and, for Linux Claude credentials, OAuth
    tokens. `Path.write_text()` followed by chmod leaves an avoidable window with the
    process umask's default permissions, so keep the file owner-only for its whole
    lifetime.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _restrict_file(path)
    except Exception:
        try:
            os.close(fd)
        except Exception:
            pass
        try:
            tmp.unlink()
        except Exception:
            pass
        raise


# --- Claude credential STORE (file vs macOS Keychain) -----------------------
# `claude auth login` does NOT write `.credentials.json` on macOS — it stores the
# OAuth blob in the login Keychain, under a service name namespaced by CLAUDE_CONFIG_DIR:
#   "Claude Code-credentials-<sha256(CLAUDE_CONFIG_DIR)[:8]>"
# (verified empirically against claude 2.1.x). The account metadata (oauthAccount) IS
# written to `.claude.json`, which we use as the cheap "logged in" signal. On Linux (no
# Keychain) the CLI falls back to writing `.credentials.json`. This abstraction reads/
# writes whichever store holds the blob so we can mint a Bearer token + persist refreshes.

_CLAUDE_KEYCHAIN_PREFIX = "Claude Code-credentials-"


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _claude_keychain_service(config_dir: Path) -> str:
    digest = hashlib.sha256(str(config_dir).encode("utf-8")).hexdigest()[:8]
    return _CLAUDE_KEYCHAIN_PREFIX + digest


def _keychain_account() -> str:
    try:
        import getpass

        return getpass.getuser()
    except Exception:
        return ""


def _read_claude_keychain(config_dir: Path) -> dict[str, Any]:
    if not _is_macos():
        return {}
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", _claude_keychain_service(config_dir), "-w"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return {}
    if proc.returncode != 0:
        return {}
    try:
        data = json.loads((proc.stdout or "").strip())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_claude_keychain(config_dir: Path, data: dict[str, Any]) -> bool:
    if not _is_macos():
        return False
    args = ["security", "add-generic-password", "-U", "-s", _claude_keychain_service(config_dir)]
    acct = _keychain_account()
    if acct:
        args += ["-a", acct]
    args += ["-w", json.dumps(data)]
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=10).returncode == 0
    except Exception:
        return False


def _delete_claude_keychain(config_dir: Path) -> bool:
    if not _is_macos():
        return False
    try:
        proc = subprocess.run(
            ["security", "delete-generic-password", "-s", _claude_keychain_service(config_dir)],
            capture_output=True, text=True, timeout=10,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _clear_claude_account_metadata(config_dir: Path) -> None:
    cfg = config_dir / ".claude.json"
    try:
        if not cfg.is_file():
            return
        data = json.loads(cfg.read_text())
        if not isinstance(data, dict):
            _restrict_file(cfg)
            return
        if "oauthAccount" in data:
            data.pop("oauthAccount", None)
            _write_json_owner_only(cfg, data)
        else:
            _restrict_file(cfg)
    except Exception:
        logger.debug("Failed to clear Claude account metadata", exc_info=True)


def read_claude_credentials_raw(config_dir: Path) -> tuple[str, dict[str, Any]]:
    """Return (store, full_data): store is 'file' | 'keychain' | '' and full_data is the
    parsed credential JSON exactly as stored (so a refresh write-back preserves shape)."""
    creds = _claude_credentials_file(config_dir)
    try:
        if creds.is_file() and creds.stat().st_size > 2:
            data = json.loads(creds.read_text())
            if isinstance(data, dict) and data:
                return "file", data
    except Exception:
        pass
    data = _read_claude_keychain(config_dir)
    if isinstance(data, dict) and data:
        return "keychain", data
    return "", {}


def write_claude_credentials_raw(config_dir: Path, store: str, data: dict[str, Any]) -> None:
    if store == "keychain":
        _write_claude_keychain(config_dir, data)
        return
    creds = _claude_credentials_file(config_dir)
    try:
        _write_json_owner_only(creds, data)
    except Exception:
        logger.debug("Failed to persist Claude credentials file", exc_info=True)


def claude_oauth_blob(data: dict[str, Any]) -> dict[str, Any]:
    """Extract the claudeAiOauth blob (accessToken/refreshToken/expiresAt) from a stored
    credential dict, tolerating both the wrapped and flat shapes."""
    if not isinstance(data, dict):
        return {}
    blob = data.get("claudeAiOauth")
    return blob if isinstance(blob, dict) else data


def claude_login_present(config_dir: Path) -> bool:
    """Cheap login check for the hot status-poll path (NO Keychain subprocess): a written
    `.credentials.json` (Linux) OR an `oauthAccount` block in `.claude.json` (macOS, where
    the token itself lives in the Keychain)."""
    if _file_has_content(_claude_credentials_file(config_dir)):
        return True
    try:
        claude_json = config_dir / ".claude.json"
        if claude_json.is_file():
            acct = json.loads(claude_json.read_text()).get("oauthAccount")
            return isinstance(acct, dict) and bool(acct.get("accountUuid") or acct.get("emailAddress"))
    except Exception:
        pass
    return False


def ensure_claude_noninteractive(config_dir: Path, workspace: str | None = None) -> None:
    """Pre-seed the isolated `.claude.json` so an interactive `claude` launch skips the
    first-run wizard.

    `claude auth login` writes the OAuth login but NOT the onboarding-complete markers, so
    the agent's terminal otherwise runs the full first-run flow (theme/intro/tips + the
    per-folder "do you trust this directory?" prompt) — which looks exactly like a fresh,
    un-logged-in instance even though the credentials are present. We set the same flags a
    normally-onboarded `~/.claude.json` carries:
      - top-level ``hasCompletedOnboarding`` → skips the onboarding wizard
      - ``projects[<workspace>].hasTrustDialogAccepted`` (+ a seen count) → skips the trust
        dialog for the run's working directory.
    Idempotent: only writes when something actually changes. (Theme is intentionally left
    unset — a fully onboarded global config carries no `theme` key and shows no picker.)"""
    cfg = config_dir / ".claude.json"
    try:
        data = json.loads(cfg.read_text()) if cfg.is_file() else {}
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    changed = False
    if not data.get("hasCompletedOnboarding"):
        data["hasCompletedOnboarding"] = True
        changed = True
    ws = (workspace or "").strip()
    if ws:
        projects = data.get("projects")
        if not isinstance(projects, dict):
            projects = {}
            data["projects"] = projects
        entry = projects.get(ws)
        if not isinstance(entry, dict):
            entry = {}
            projects[ws] = entry
        if not entry.get("hasTrustDialogAccepted"):
            entry["hasTrustDialogAccepted"] = True
            changed = True
        if not entry.get("projectOnboardingSeenCount"):
            entry["projectOnboardingSeenCount"] = 1
            changed = True
    if changed:
        try:
            _write_json_owner_only(cfg, data)
        except Exception:
            logger.debug("Failed to seed claude non-interactive flags", exc_info=True)
    elif cfg.exists():
        _restrict_file(cfg)


# --- launch-env injection (called from coding_model_config.build_launch_env) ----

def subscription_logged_in(db, owner: str | None, provider: str) -> bool:
    """Whether the owner has a usable subscription login for the provider.

    Trusts the on-disk credential (token row / config-dir file) over the DB status,
    so a login completed out-of-band still counts."""
    prov = normalize_provider(provider)
    if not prov:
        return False
    if prov == "claude":
        # `claude auth login` stores creds in the isolated CLAUDE_CONFIG_DIR (file on
        # Linux; macOS Keychain + oauthAccount in .claude.json).
        return claude_login_present(auth_config_dir(owner, "claude", create=False))
    if prov == "codex":
        return _file_has_content(_codex_auth_file(auth_config_dir(owner, "codex", create=False)))
    return False


def subscription_launch_env(db, owner: str | None, harness_id: str | None,
                            workspace: str | None = None) -> dict[str, str]:
    """Env to inject when a thread uses ``auth_mode="subscription"``.

    Points the CLI at the owner's isolated credential dir. For Claude we also pre-seed the
    isolated config's onboarding/trust flags (see ``ensure_claude_noninteractive``) so the
    interactive agent terminal doesn't run the first-run wizard and look un-logged-in.
    Returns {} for harnesses without a subscription concept.
    """
    provider = normalize_provider(harness_id)
    if not provider:
        return {}
    if provider == "claude":
        config_dir = auth_config_dir(owner, "claude")
        ensure_claude_noninteractive(config_dir, workspace)
        return {"CLAUDE_CONFIG_DIR": str(config_dir)}
    if provider == "codex":
        return {"CODEX_HOME": str(auth_config_dir(owner, "codex"))}
    return {}


# --- DB helpers -------------------------------------------------------------

def _load_row(db, owner: str | None, provider: str) -> CodingProviderAuth | None:
    try:
        return (
            db.query(CodingProviderAuth)
            .filter(
                CodingProviderAuth.owner == (owner or None),
                CodingProviderAuth.provider == provider,
            )
            .first()
        )
    except Exception:
        return None


def _upsert_row(db, owner: str | None, provider: str, **fields) -> CodingProviderAuth:
    row = _load_row(db, owner, provider)
    if row is None:
        import uuid

        row = CodingProviderAuth(id=str(uuid.uuid4()), owner=owner or None, provider=provider)
        db.add(row)
    for key, value in fields.items():
        setattr(row, key, value)
    row.last_checked_at = datetime.utcnow()
    return row


def _resolve_bin(name: str) -> str | None:
    """Resolve a CLI binary, mirroring dtach_bin()'s env->PATH->common-prefix style so
    a GUI-launched (minimal-PATH) backend still finds it."""
    found = shutil.which(name)
    if found:
        return found
    candidates = [
        os.path.expanduser(f"~/.local/bin/{name}"),
        os.path.expanduser(f"~/.bun/bin/{name}"),
        os.path.expanduser(f"~/.npm-global/bin/{name}"),
        f"/opt/homebrew/bin/{name}",
        f"/usr/local/bin/{name}",
    ]
    for candidate in candidates:
        try:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        except Exception:
            pass
    return None


def _login_shell() -> str:
    shell = os.environ.get("SHELL")
    if shell and os.path.exists(shell):
        return shell
    for candidate in ("/bin/zsh", "/bin/bash"):
        if os.path.exists(candidate):
            return candidate
    return "/bin/bash"


class CodingAuthError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class CodingAuthService:
    """Owns the per-owner subscription-login PTY sessions for claude/codex."""

    def __init__(self) -> None:
        self._pty = CodingPtyBridge(
            backend_available=lambda: dtach_bin() is not None,
            get_owned_run=self._unsupported_run,
            finished_statuses=set(),
            logger=logger,
        )
        self._capture_tasks: dict[str, asyncio.Task] = {}
        self._watch_tasks: dict[str, asyncio.Task] = {}

    async def _unsupported_run(self, run_id: str, owner: str) -> Any:  # pragma: no cover
        raise RuntimeError("auth sessions are not runs")

    def _session_name(self, owner: str | None, provider: str) -> str:
        # Keep well under the unix-socket sun_path limit (see coding_pty_bridge).
        return f"ody-auth-{provider}-{owner_slug(owner)[:12]}"

    # ---- status ----------------------------------------------------------

    def status(self, owner: str | None, provider: str) -> dict[str, Any]:
        prov = normalize_provider(provider)
        if not prov:
            raise CodingAuthError(400, f"Unknown provider: {provider}")
        db = SessionLocal()
        try:
            logged_in = subscription_logged_in(db, owner, prov)
            row = _load_row(db, owner, prov)
            status = row.status if row is not None else "logged_out"
            # Reconcile: disk creds present but row stale -> mark logged_in.
            if logged_in and status != "logged_in":
                row = _upsert_row(
                    db, owner, prov,
                    status="logged_in",
                    config_dir=str(auth_config_dir(owner, prov, create=False)),
                    last_login_at=(row.last_login_at if row and row.last_login_at else datetime.utcnow()),
                    error=None,
                )
                db.commit()
                status = "logged_in"
            elif not logged_in and status == "logged_in":
                row = _upsert_row(db, owner, prov, status="logged_out")
                db.commit()
                status = "logged_out"
            return {
                "provider": prov,
                "status": status,
                "logged_in": bool(logged_in),
                "account_label": (row.account_label if row else None),
                "last_login_at": (row.last_login_at.isoformat() + "Z") if row and row.last_login_at else None,
                "error": (row.error if row else None),
                "available": _resolve_bin(_PROVIDER_BIN[prov]) is not None,
            }
        finally:
            db.close()

    def status_all(self, owner: str | None) -> dict[str, Any]:
        return {prov: self.status(owner, prov) for prov in PROVIDERS}

    # ---- login -----------------------------------------------------------

    async def login(self, owner: str | None, provider: str, *, cols: int = 100, rows: int = 30) -> dict[str, Any]:
        prov = normalize_provider(provider)
        if not prov:
            raise CodingAuthError(400, f"Unknown provider: {provider}")
        if not self._pty_available():
            raise CodingAuthError(503, "Terminal backend (dtach) unavailable; cannot run login")
        binary = _resolve_bin(_PROVIDER_BIN[prov])
        if not binary:
            raise CodingAuthError(
                400,
                f"The '{_PROVIDER_BIN[prov]}' CLI was not found. Install it and ensure it is on your shell PATH.",
            )

        config_dir = auth_config_dir(owner, prov)
        name = self._session_name(owner, prov)
        # Tear down any previous (possibly stale) login session for this owner/provider.
        await self._teardown_session(name)

        log_path = config_dir / "login.log"
        try:
            log_path.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass
        # Pre-create the capture log owner-only (0o600). Login flows can expose
        # sensitive OAuth/status details in terminal output before the watcher scrubs
        # it, so it must never be world-readable under the default umask.
        try:
            os.close(os.open(str(log_path), os.O_CREAT | os.O_WRONLY, 0o600))
        except Exception:
            pass

        script_path = config_dir / "login.sh"
        env = self._login_env(prov, config_dir)
        self._write_login_script(script_path, prov, config_dir)

        result = await self._pty.create_session(name, script_path, env, cwd=str(config_dir))
        if result.returncode != 0:
            err = (result.stderr or b"").decode(errors="replace").strip()
            self._set_status(owner, prov, "error", error=err or "failed to start login session")
            raise CodingAuthError(500, f"Failed to start login: {err or result.returncode}")

        self._capture_tasks[name] = self._pty.start_capture(
            name,
            log_path,
            init_cols=cols,
            init_rows=rows,
            output_redactor=_redact_auth_output_bytes,
        )
        self._set_status(owner, prov, "pending", config_dir=str(config_dir), error=None)
        self._watch_tasks[name] = asyncio.create_task(
            self._watch_completion(owner, prov, name, log_path, config_dir)
        )
        return {
            "provider": prov,
            "status": "pending",
            "session": name,
            "attach": name,
            "config_dir": str(config_dir),
        }

    def _pty_available(self) -> bool:
        return dtach_bin() is not None

    def _login_env(self, provider: str, config_dir: Path) -> dict[str, str]:
        env = safe_child_env({"TERM": "xterm-256color"})
        if provider == "claude":
            env["CLAUDE_CONFIG_DIR"] = str(config_dir)
        elif provider == "codex":
            env["CODEX_HOME"] = str(config_dir)
        return env

    def _write_login_script(self, script_path: Path, provider: str, config_dir: Path) -> None:
        # Same -ilc wrapper as a normal coding run, so the login-shell PATH (where the
        # CLI lives) is inherited even when the backend is a GUI-launched .app.
        user_shell = _login_shell()
        login_command = _LOGIN_COMMAND[provider]
        script_path.write_text(
            "#!/bin/bash\n"
            "set +e\n"
            f"cd {shlex.quote(str(config_dir))}\n"
            'export TERM="${TERM:-xterm-256color}"\n'
            f"{shlex.quote(user_shell)} -ilc {shlex.quote(login_command)}\n"
            'EC=$?\n'
            'echo ""\n'
            'echo "[odysseus] login command exited ($EC). You may close this pane."\n'
            'exit "$EC"\n',
            encoding="utf-8",
        )
        try:
            script_path.chmod(0o700)
        except Exception:
            pass

    async def _watch_completion(self, owner, provider, name, log_path: Path, config_dir: Path) -> None:
        """Poll for login completion (credentials file / printed token), then store +
        flip status. Self-terminates on timeout or when the login session ends."""
        waited = 0.0
        try:
            while waited < _WATCH_TIMEOUT:
                await asyncio.sleep(_WATCH_INTERVAL)
                waited += _WATCH_INTERVAL
                done = await self._try_finalize(owner, provider, name, log_path, config_dir)
                if done:
                    return
                # If the login session has ended without producing creds, stop waiting.
                if not await self._pty.has_session(name):
                    # one last check after the program exited
                    if await self._try_finalize(owner, provider, name, log_path, config_dir):
                        return
                    self._set_status(owner, provider, "logged_out", error="login session ended before completion")
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("auth completion watcher failed for %s", name, exc_info=True)
        finally:
            self._watch_tasks.pop(name, None)

    async def _try_finalize(self, owner, provider, name, log_path: Path, config_dir: Path) -> bool:
        if provider == "claude":
            # `claude auth login` stores creds in the isolated CLAUDE_CONFIG_DIR — a
            # `.credentials.json` file (Linux) or the macOS Keychain (+ oauthAccount in
            # .claude.json). Detect either; no PTY token scraping.
            if claude_login_present(config_dir):
                creds = _claude_credentials_file(config_dir)
                if _file_has_content(creds):
                    _restrict_file(creds)
                # Seed onboarding flags now so the FIRST coding-agent launch in this
                # freshly-logged-in config dir doesn't run the first-run wizard.
                ensure_claude_noninteractive(config_dir)
                await self._teardown_session(name)
                self._scrub_log(log_path)
                self._set_status(
                    owner, provider, "logged_in",
                    config_dir=str(config_dir), last_login_at=datetime.utcnow(), error=None,
                )
                return True
            return False
        if provider == "codex":
            auth_file = _codex_auth_file(config_dir)
            if _file_has_content(auth_file):
                _restrict_file(auth_file)  # the CLI may write it world-readable
                label = self._codex_account_label(config_dir)
                await self._teardown_session(name)
                self._scrub_log(log_path)
                self._set_status(
                    owner, provider, "logged_in",
                    account_label=label,
                    config_dir=str(config_dir), last_login_at=datetime.utcnow(), error=None,
                )
                return True
            return False
        return False

    def _scrub_log(self, log_path: Path) -> None:
        try:
            log_path.write_text("[odysseus] login output scrubbed after success\n", encoding="utf-8")
            _restrict_file(log_path)
        except Exception:
            pass

    def _codex_account_label(self, config_dir: Path) -> str | None:
        try:
            data = json.loads(_codex_auth_file(config_dir).read_text())
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        # Best-effort: surface a plan/email if present, never the token.
        tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
        for key in ("account_id", "email", "chatgpt_plan_type", "plan_type"):
            val = data.get(key) or tokens.get(key)
            if isinstance(val, str) and val:
                return val
        return "ChatGPT" if data else None

    # ---- logout ----------------------------------------------------------

    async def logout(self, owner: str | None, provider: str) -> dict[str, Any]:
        prov = normalize_provider(provider)
        if not prov:
            raise CodingAuthError(400, f"Unknown provider: {provider}")
        name = self._session_name(owner, prov)
        await self._teardown_session(name)
        config_dir = auth_config_dir(owner, prov, create=False)
        if prov == "claude":
            _delete_claude_keychain(config_dir)
            _clear_claude_account_metadata(config_dir)
        for fname in (".credentials.json", "auth.json", "login.log"):
            try:
                (config_dir / fname).unlink()
            except FileNotFoundError:
                pass
            except Exception:
                pass
        self._set_status(owner, prov, "logged_out", token_encrypted=None, error=None)
        return {"provider": prov, "status": "logged_out", "logged_in": False}

    # ---- attach ----------------------------------------------------------

    async def attach(self, websocket, owner: str | None, provider: str, *, cols: int = 100, rows: int = 30) -> None:
        prov = normalize_provider(provider)
        if not prov:
            await websocket.close(code=4400)
            return
        name = self._session_name(owner, prov)
        log_path = auth_config_dir(owner, prov, create=False) / "login.log"
        await self._pty.attach_session(
            websocket,
            name,
            cols=cols,
            rows=rows,
            log_path=log_path,
            output_redactor=_redact_auth_output_bytes,
        )

    # ---- internals -------------------------------------------------------

    def _set_status(self, owner, provider, status, **fields) -> None:
        db = SessionLocal()
        try:
            _upsert_row(db, owner, provider, status=status, **fields)
            db.commit()
        except Exception:
            logger.debug("failed to set auth status %s/%s=%s", owner, provider, status, exc_info=True)
        finally:
            db.close()

    async def _teardown_session(self, name: str) -> None:
        task = self._capture_tasks.pop(name, None)
        if task and not task.done():
            task.cancel()
        watch = self._watch_tasks.pop(name, None)
        if watch and not watch.done() and watch is not asyncio.current_task():
            watch.cancel()
        try:
            if await self._pty.has_session(name):
                await self._pty.kill_session(name)
        except Exception:
            logger.debug("failed to kill auth session %s", name, exc_info=True)


_SERVICE: CodingAuthService | None = None


def get_coding_auth_service() -> CodingAuthService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = CodingAuthService()
    return _SERVICE
