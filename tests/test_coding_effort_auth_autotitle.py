"""Tests for the agent/provider/model/effort selection, subscription-auth launch
env, and coding-thread auto-title features."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, CodingProject, CodingProviderAuth, CodingThread, ModelEndpoint
from src import coding_effort, coding_providers
from src.coding_provider_launch import build_coding_agent_launch_plan


# --------------------------------------------------------------------------- #
# coding_effort (pure)
# --------------------------------------------------------------------------- #

def test_normalize_effort():
    assert coding_effort.normalize_effort("HIGH") == "high"
    assert coding_effort.normalize_effort("  low ") == "low"
    assert coding_effort.normalize_effort("bogus") == ""
    assert coding_effort.normalize_effort(None) == ""


def test_codex_effort_args():
    assert coding_effort.codex_effort_config_args("medium") == ["-c", "model_reasoning_effort=medium"]
    # "max" maps down to codex's top tier "high".
    assert coding_effort.codex_effort_config_args("max") == ["-c", "model_reasoning_effort=high"]
    assert coding_effort.codex_effort_config_args("") == []
    assert coding_effort.codex_effort_config_args("nope") == []


def test_claude_effort_env():
    env = coding_effort.claude_effort_env("high")
    assert env["MAX_THINKING_TOKENS"] == "24000"
    assert env["CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING"] == "1"
    assert coding_effort.claude_effort_env("") == {}


def test_effort_supported():
    assert coding_effort.effort_supported("codex")
    assert coding_effort.effort_supported("claude")
    assert not coding_effort.effort_supported("generic")
    assert not coding_effort.effort_supported("pi")


# --------------------------------------------------------------------------- #
# build_coding_agent_launch_plan effort/auth wiring
# --------------------------------------------------------------------------- #

def test_codex_launch_plan_includes_effort_arg(tmp_path):
    plan = build_coding_agent_launch_plan(
        harness_id="codex", base_command="codex", model="gpt-5", endpoint_url="",
        run_dir=tmp_path, effort="medium", auth_mode="subscription",
    )
    assert "model_reasoning_effort=medium" in plan.command
    assert plan.metadata["effort"] == "medium"
    assert plan.metadata["effort_applied"] is True
    assert plan.metadata["auth_mode"] == "subscription"


def test_claude_launch_plan_effort_is_env_not_arg(tmp_path):
    plan = build_coding_agent_launch_plan(
        harness_id="claude", base_command="claude", model="opus", endpoint_url="",
        run_dir=tmp_path, effort="high", auth_mode="subscription",
    )
    # Claude effort is applied via env (coding_model_config), never as a CLI arg.
    assert "model_reasoning_effort" not in plan.command
    assert plan.metadata["effort"] == "high"
    assert plan.metadata["effort_applied"] is True
    # Model selection now reaches the Claude CLI.
    assert "--model opus" in plan.command


def test_generic_harness_effort_not_applied(tmp_path):
    plan = build_coding_agent_launch_plan(
        harness_id="generic", base_command="bash", model="", endpoint_url="",
        run_dir=tmp_path, effort="high",
    )
    assert plan.metadata["effort"] == "high"
    assert plan.metadata["effort_applied"] is False
    assert "model_reasoning_effort" not in plan.command


# --------------------------------------------------------------------------- #
# coding_providers cascade + selector expansion
# --------------------------------------------------------------------------- #

class _FakeProject:
    def __init__(self, default_effort=None, default_auth_mode=None):
        self.default_effort = default_effort
        self.default_auth_mode = default_auth_mode


def test_expand_provider_selector():
    assert coding_providers.expand_provider_selector("subscription:claude") == {
        "auth_mode": "subscription", "model_endpoint_id": ""}
    assert coding_providers.expand_provider_selector("endpoint:ep1") == {
        "auth_mode": "endpoint", "model_endpoint_id": "ep1"}
    assert coding_providers.expand_provider_selector("none") == {
        "auth_mode": "none", "model_endpoint_id": ""}
    assert coding_providers.expand_provider_selector("") is None
    # Bare value is treated as an endpoint id.
    assert coding_providers.expand_provider_selector("ep-bare") == {
        "auth_mode": "endpoint", "model_endpoint_id": "ep-bare"}


def test_cascade_effort_thread_then_project_then_global(monkeypatch):
    monkeypatch.setattr(coding_providers, "get_setting", lambda key, default=None: "low")
    # Thread arg wins.
    assert coding_providers.cascade_effort("high", _FakeProject(default_effort="medium")) == "high"
    # Falls back to project default.
    assert coding_providers.cascade_effort(None, _FakeProject(default_effort="medium")) == "medium"
    # Falls back to global setting.
    assert coding_providers.cascade_effort(None, _FakeProject()) == "low"


def test_cascade_auth_mode(monkeypatch):
    monkeypatch.setattr(coding_providers, "get_setting", lambda key, default=None: "none")
    assert coding_providers.cascade_auth_mode("subscription", _FakeProject()) == "subscription"
    assert coding_providers.cascade_auth_mode(None, _FakeProject(default_auth_mode="endpoint")) == "endpoint"
    assert coding_providers.cascade_auth_mode(None, _FakeProject()) == "none"
    # invalid values ignored
    assert coding_providers.cascade_auth_mode("bogus", _FakeProject()) == "none"


# --------------------------------------------------------------------------- #
# DB-backed: build_launch_env auth/effort + providers catalog + auto-title
# --------------------------------------------------------------------------- #

@pytest.fixture
def db_store(tmp_path, monkeypatch):
    from src import coding_auth_service

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "t.sqlite"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    # Point the auth-service credential dirs at the temp data dir.
    monkeypatch.setattr(coding_auth_service, "DATA_DIR", str(data_dir))
    try:
        yield SessionLocal, data_dir
    finally:
        engine.dispose()


def _seed_project_thread(db, **thread_kwargs):
    project = CodingProject(id="p1", owner="tester", name="Proj", root_path="/tmp/x",
                            default_harness="codex")
    db.add(project)
    defaults = dict(id="t1", project_id="p1", owner="tester", title="Untitled Thread",
                    cwd="/tmp/x", harness_id="codex", status="idle", metadata_json="{}",
                    created_at=datetime.utcnow(), updated_at=datetime.utcnow())
    defaults.update(thread_kwargs)
    thread = CodingThread(**defaults)
    db.add(thread)
    db.commit()
    return project, thread


def test_build_launch_env_claude_apikey_sets_anthropic_envs(db_store):
    from src.coding_model_config import build_launch_env

    SessionLocal, _ = db_store
    db = SessionLocal()
    try:
        ep = ModelEndpoint(id="ep1", owner="tester", name="Anthropic-compatible",
                           base_url="https://proxy.example.com/v1", api_key="secret-key",
                           is_enabled=True, cached_models=json.dumps(["claude-sonnet-4-6"]))
        db.add(ep)
        db.commit()
        env = build_launch_env(db, "tester", "ep1", "claude-sonnet-4-6",
                               harness_id="claude", auth_mode="endpoint", effort="medium")
        # Closes the gap: a Claude harness against ANY api-key endpoint gets the Anthropic envs.
        assert env["ANTHROPIC_BASE_URL"] == "https://proxy.example.com/v1"
        assert env["ANTHROPIC_API_KEY"] == "secret-key"
        assert env["ANTHROPIC_MODEL"] == "claude-sonnet-4-6"
        # Claude effort applied via env.
        assert env["MAX_THINKING_TOKENS"] == "10000"
    finally:
        db.close()


def test_build_launch_env_subscription_codex_sets_codex_home_no_apikey(db_store):
    from src.coding_model_config import build_launch_env

    SessionLocal, data_dir = db_store
    db = SessionLocal()
    try:
        ep = ModelEndpoint(id="ep1", owner="tester", name="OpenAI", base_url="https://api.openai.com/v1",
                           api_key="should-not-leak", is_enabled=True, cached_models=json.dumps(["gpt-5"]))
        db.add(ep)
        db.commit()
        # Subscription mode must NOT inject the endpoint api-key, and must point CODEX_HOME
        # at the owner's isolated dir.
        env = build_launch_env(db, "tester", "ep1", "gpt-5",
                               harness_id="codex", auth_mode="subscription", effort="low")
        assert "OPENAI_API_KEY" not in env
        assert "CODEX_HOME" in env
        assert str(data_dir) in env["CODEX_HOME"]
        assert env["CODEX_HOME"].endswith("/codex")
    finally:
        db.close()


def test_build_launch_env_subscription_claude_uses_config_dir_and_onboards(db_store):
    # Subscription Claude points the CLI at the isolated config dir (the CLI finds the
    # OAuth login there — file or macOS Keychain) and pre-seeds onboarding/trust flags so
    # the interactive agent terminal doesn't run the first-run wizard (looking un-logged-in).
    import json as _json
    from pathlib import Path
    from src.coding_model_config import build_launch_env
    from src import coding_auth_service as a

    SessionLocal, data_dir = db_store
    db = SessionLocal()
    try:
        env = build_launch_env(db, "tester", "", "claude-opus-4-8",
                               harness_id="claude", auth_mode="subscription",
                               workspace="/tmp/my-repo")
        assert "CLAUDE_CONFIG_DIR" in env
        assert env["CLAUDE_CONFIG_DIR"].endswith("/claude")
        assert str(data_dir) in env["CLAUDE_CONFIG_DIR"]
        # Token is NOT injected as an env var anymore — the CLI reads its own login.
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
        # Onboarding wizard + per-folder trust dialog are pre-accepted in .claude.json.
        cfg = _json.loads((Path(env["CLAUDE_CONFIG_DIR"]) / ".claude.json").read_text())
        assert cfg["hasCompletedOnboarding"] is True
        assert cfg["projects"]["/tmp/my-repo"]["hasTrustDialogAccepted"] is True
    finally:
        db.close()


def test_ensure_claude_noninteractive_is_idempotent_and_preserves(tmp_path):
    from src import coding_auth_service as a
    import json as _json
    import os as _os
    import stat as _stat
    cfg = tmp_path / ".claude.json"
    cfg.write_text(_json.dumps({"oauthAccount": {"emailAddress": "x@y.z"}, "theme": "ocean"}))
    _os.chmod(cfg, 0o644)
    a.ensure_claude_noninteractive(tmp_path, workspace="/repo")
    d = _json.loads(cfg.read_text())
    assert d["hasCompletedOnboarding"] is True
    assert d["projects"]["/repo"]["hasTrustDialogAccepted"] is True
    # Existing keys preserved (login + user theme untouched).
    assert d["oauthAccount"]["emailAddress"] == "x@y.z"
    assert d["theme"] == "ocean"
    assert oct(_stat.S_IMODE(_os.stat(cfg).st_mode)) == "0o600"
    # Second call is a no-op (no crash, flags stay).
    a.ensure_claude_noninteractive(tmp_path, workspace="/repo")
    assert _json.loads(cfg.read_text())["hasCompletedOnboarding"] is True
    assert oct(_stat.S_IMODE(_os.stat(cfg).st_mode)) == "0o600"


def test_list_providers_for_harness(db_store):
    SessionLocal, _ = db_store
    db = SessionLocal()
    try:
        db.add(ModelEndpoint(id="ep1", owner="tester", name="Local vLLM",
                             base_url="http://localhost:8002/v1", is_enabled=True,
                             model_type="llm", cached_models=json.dumps(["qwen3"])))
        db.commit()
        catalog = coding_providers.list_providers_for_harness(db, "tester", "claude")
        kinds = {p["kind"] for p in catalog["providers"]}
        assert "subscription" in kinds   # claude offers a subscription provider
        assert "endpoint" in kinds       # plus the configured endpoint
        assert catalog["effort_supported"] is True
        sub = next(p for p in catalog["providers"] if p["kind"] == "subscription")
        assert sub["id"] == "subscription:claude"
        assert sub["auth_mode"] == "subscription"

        # A non-subscription harness offers only endpoints.
        generic = coding_providers.list_providers_for_harness(db, "tester", "generic")
        assert all(p["kind"] == "endpoint" for p in generic["providers"])
        assert generic["effort_supported"] is False
    finally:
        db.close()


def test_autotitle_heuristic_fallback(db_store, monkeypatch):
    from src import coding_autotitle

    SessionLocal, _ = db_store
    # No utility model available -> heuristic fallback.
    monkeypatch.setattr(coding_autotitle, "_llm_title", _async_return(""))
    db = SessionLocal()
    try:
        _seed_project_thread(db, title="Untitled Thread")
    finally:
        db.close()

    # Point the autotitle module's SessionLocal at the test DB.
    monkeypatch.setattr(coding_autotitle, "SessionLocal", SessionLocal)
    title = asyncio.run(coding_autotitle.maybe_autotitle_thread(
        "Refactor the authentication module to use JWT", "t1", "tester"))
    assert title == "Refactor the authentication module to use"

    db = SessionLocal()
    try:
        thread = db.query(CodingThread).filter(CodingThread.id == "t1").first()
        assert thread.title == "Refactor the authentication module to use"
        assert json.loads(thread.metadata_json).get("title_autoset") is True
    finally:
        db.close()


def test_autotitle_uses_llm_when_available(db_store, monkeypatch):
    from src import coding_autotitle

    SessionLocal, _ = db_store
    monkeypatch.setattr(coding_autotitle, "_llm_title", _async_return("Add Dark Mode"))
    monkeypatch.setattr(coding_autotitle, "SessionLocal", SessionLocal)
    db = SessionLocal()
    try:
        _seed_project_thread(db, title="Untitled Thread")
    finally:
        db.close()
    title = asyncio.run(coding_autotitle.maybe_autotitle_thread("please add a dark mode toggle", "t1", "tester"))
    assert title == "Add Dark Mode"


def test_autotitle_noop_when_title_already_custom(db_store, monkeypatch):
    from src import coding_autotitle

    SessionLocal, _ = db_store
    monkeypatch.setattr(coding_autotitle, "_llm_title", _async_return("Should Not Be Used"))
    monkeypatch.setattr(coding_autotitle, "SessionLocal", SessionLocal)
    db = SessionLocal()
    try:
        _seed_project_thread(db, title="My Custom Title")
    finally:
        db.close()
    title = asyncio.run(coding_autotitle.maybe_autotitle_thread("some task text here", "t1", "tester"))
    assert title is None
    db = SessionLocal()
    try:
        assert db.query(CodingThread).filter(CodingThread.id == "t1").first().title == "My Custom Title"
    finally:
        db.close()


def _async_return(value):
    async def _fn(*args, **kwargs):
        return value
    return _fn


# --------------------------------------------------------------------------- #
# Subscription-login security: token scrubbing + credential-file permissions
# --------------------------------------------------------------------------- #

import os
import stat


def test_owner_credential_isolation(monkeypatch, tmp_path):
    from src import coding_auth_service as a
    monkeypatch.setattr(a, "DATA_DIR", str(tmp_path))
    d1 = a.auth_config_dir("alice@example.com", "claude")
    d2 = a.auth_config_dir("bob@example.com", "claude")
    assert d1 != d2                         # per-owner isolation
    assert "alice" not in str(d1)           # owner key (email) is hashed, never raw
    assert oct(stat.S_IMODE(os.stat(d1).st_mode)) == "0o700"


def test_claude_finalize_detects_credentials_file(monkeypatch, tmp_path):
    # `claude auth login` writes .credentials.json into the isolated config dir; finalize
    # detects THAT (no PTY token scraping) and marks logged_in + locks the file 0o600.
    from src import coding_auth_service as a
    monkeypatch.setattr(a, "DATA_DIR", str(tmp_path))
    svc = a.CodingAuthService()
    config_dir = a.auth_config_dir("tester", "claude")
    creds = config_dir / ".credentials.json"
    creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat01-AAAA", "refreshToken": "rt"}}))
    os.chmod(creds, 0o644)

    captured = {}
    monkeypatch.setattr(svc, "_set_status", lambda owner, provider, status, **kw: captured.update(
        {"owner": owner, "provider": provider, "status": status, **kw}))
    done = asyncio.run(svc._try_finalize("tester", "claude", "sess-x", config_dir / "login.log", config_dir))
    assert done is True
    assert captured["status"] == "logged_in"
    assert "token_encrypted" not in captured  # token is no longer scraped/stored
    assert oct(stat.S_IMODE(os.stat(creds).st_mode)) == "0o600"


def test_codex_finalize_restricts_auth_json_perms(monkeypatch, tmp_path):
    from src import coding_auth_service as a
    monkeypatch.setattr(a, "DATA_DIR", str(tmp_path))
    svc = a.CodingAuthService()

    config_dir = a.auth_config_dir("tester", "codex")
    auth_file = config_dir / "auth.json"
    auth_file.write_text(json.dumps({"tokens": {"access_token": "secret"}, "email": "u@x.com"}))
    os.chmod(auth_file, 0o644)  # simulate the CLI writing it world-readable

    monkeypatch.setattr(svc, "_set_status", lambda *a_, **k_: None)
    done = asyncio.run(svc._try_finalize("tester", "codex", "sess-y", config_dir / "login.log", config_dir))
    assert done is True
    # The CLI's world-readable auth.json is tightened to owner-only.
    assert oct(stat.S_IMODE(os.stat(auth_file).st_mode)) == "0o600"


def test_subscription_login_env_does_not_inherit_parent_secrets(monkeypatch, tmp_path):
    from src import coding_auth_service as a

    monkeypatch.setattr(a, "DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-parent-secret-value")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-parent-secret-value")
    monkeypatch.setenv("ODYSSEUS_INTERNAL_TOKEN", "ody_internal_parent_secret")

    svc = a.CodingAuthService()
    config_dir = a.auth_config_dir("tester", "claude")
    env = svc._login_env("claude", config_dir)

    assert env["CLAUDE_CONFIG_DIR"] == str(config_dir)
    assert env["TERM"] == "xterm-256color"
    assert "OPENAI_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "ODYSSEUS_INTERNAL_TOKEN" not in env


def test_subscription_auth_output_redactor_masks_token_shapes():
    from src.coding_auth_service import _redact_auth_output_text

    text = (
        "access_token=sk-proj-parent-secret-value\n"
        "Authorization: Bearer hf_abcdefghijklmnopqrstuvwxyz\n"
        "id_token=eyJabcdefghijklmnopqrstuv.abcdefghijklmnop.abcdefghijklmnop\n"
    )

    redacted = _redact_auth_output_text(text)

    assert "sk-proj-parent-secret-value" not in redacted
    assert "hf_abcdefghijklmnopqrstuvwxyz" not in redacted
    assert "eyJabcdefghijklmnopqrstuv.abcdefghijklmnop.abcdefghijklmnop" not in redacted
    assert redacted.count("[redacted]") >= 3


def test_claude_logout_clears_metadata_and_keychain(monkeypatch, tmp_path):
    from src import coding_auth_service as a
    from src import coding_provider_oauth as oauth

    monkeypatch.setattr(a, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(a, "_is_macos", lambda: True)

    keychain: dict[str, object] = {
        "claudeAiOauth": {
            "accessToken": "sk-ant-oat01-LOGOUT",
            "refreshToken": "refresh-token",
        }
    }

    class _Proc:
        def __init__(self, returncode: int = 0, stdout: str = ""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = ""

    def _security(args, *_, **__):
        if "find-generic-password" in args:
            if not keychain:
                return _Proc(44, "")
            return _Proc(0, json.dumps(keychain))
        if "delete-generic-password" in args:
            keychain.clear()
            return _Proc(0, "")
        raise AssertionError(f"unexpected security command: {args!r}")

    monkeypatch.setattr(a.subprocess, "run", _security)
    config_dir = a.auth_config_dir("tester", "claude")
    claude_json = config_dir / ".claude.json"
    claude_json.write_text(
        json.dumps(
            {
                "oauthAccount": {"emailAddress": "x@y.z"},
                "hasCompletedOnboarding": True,
                "projects": {"/repo": {"hasTrustDialogAccepted": True}},
            }
        )
    )

    assert a.subscription_logged_in(None, "tester", "claude") is True
    assert asyncio.run(oauth.resolve_subscription_credential("tester", "claude")) is not None

    svc = a.CodingAuthService()

    async def _noop_teardown(_name):
        return None

    monkeypatch.setattr(svc, "_teardown_session", _noop_teardown)
    monkeypatch.setattr(svc, "_set_status", lambda *_, **__: None)

    result = asyncio.run(svc.logout("tester", "claude"))

    assert result["logged_in"] is False
    assert keychain == {}
    assert a.subscription_logged_in(None, "tester", "claude") is False
    assert asyncio.run(oauth.resolve_subscription_credential("tester", "claude")) is None
    if claude_json.exists():
        assert "oauthAccount" not in json.loads(claude_json.read_text())


def test_subscription_login_env_omitted_for_non_subscription_harness():
    from src.coding_auth_service import subscription_launch_env
    # Pi/generic/etc. have no subscription concept -> no auth env injected.
    assert subscription_launch_env(None, "tester", "pi") == {}
    assert subscription_launch_env(None, "tester", "generic") == {}
