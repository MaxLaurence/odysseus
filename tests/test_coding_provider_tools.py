from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import bcrypt
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

_MODULE_DATA_DIR = Path(tempfile.mkdtemp(prefix="odysseus-coding-provider-tests-")) / "data"
_MODULE_DATA_DIR.mkdir(parents=True, exist_ok=True)
os.environ["DATA_DIR"] = str(_MODULE_DATA_DIR)
os.environ["DATABASE_URL"] = f"sqlite:///{_MODULE_DATA_DIR / 'app.sqlite'}"

from core.database import (  # noqa: E402
    ApiToken,
    Base,
    CodingModelConfigSnapshot,
    CodingProject,
    CodingProviderToken,
    CodingRun,
    CodingThread,
    CodingThreadEvent,
    ModelEndpoint,
    Session,
)


@dataclass
class ProviderCodingStore:
    data_dir: Path
    db_path: Path
    engine: object
    SessionLocal: object
    workspace: Path


class _ProviderAuth:
    is_configured = True

    def is_admin(self, username):
        return username == "tester"


@pytest.fixture
def isolated_provider_coding_store(monkeypatch, tmp_path):
    import core.database as database
    import routes.coding_routes as coding_routes
    import src.coding_provider as coding_provider
    import src.coding_runtime as coding_runtime

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_path = data_dir / "provider-coding.sqlite"
    database_url = f"sqlite:///{db_path}"

    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("AUTH_ENABLED", "true")

    engine = create_engine(database_url, connect_args={"check_same_thread": False})
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Session.__table__,
            ModelEndpoint.__table__,
            ApiToken.__table__,
            CodingProject.__table__,
            CodingThread.__table__,
            CodingRun.__table__,
            CodingThreadEvent.__table__,
            CodingModelConfigSnapshot.__table__,
            CodingProviderToken.__table__,
        ],
    )

    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(coding_routes, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(coding_provider, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(coding_runtime, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(coding_runtime, "RUN_ROOT", data_dir / "coding_runs")

    try:
        yield ProviderCodingStore(
            data_dir=data_dir,
            db_path=db_path,
            engine=engine,
            SessionLocal=TestingSessionLocal,
            workspace=workspace,
        )
    finally:
        engine.dispose()


@pytest.fixture
def provider_coding_client(monkeypatch, isolated_provider_coding_store):
    import routes.coding_routes as coding_routes
    from routes.api_token_routes import setup_api_token_routes
    from routes.coding_provider_routes import setup_coding_provider_routes
    from src.coding_runtime import CodingRuntimeService

    runtime = CodingRuntimeService()
    monkeypatch.setattr(coding_routes, "get_coding_runtime_service", lambda: runtime)

    app = FastAPI()
    app.state.auth_manager = _ProviderAuth()

    @app.middleware("http")
    async def stamp_provider_or_test_user(request, call_next):
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            raw_token = auth_header[7:]
            if request.url.path.startswith("/api/coding/provider/") and raw_token.startswith("ody_cp_"):
                request.state.current_user = "api"
                request.state.api_token = False
                request.state.api_token_owner = "tester"
                return await call_next(request)
            db = isolated_provider_coding_store.SessionLocal()
            try:
                token = None
                for candidate in (
                    db.query(ApiToken)
                    .filter(ApiToken.token_prefix == raw_token[:8], ApiToken.is_active == True)  # noqa: E712
                    .all()
                ):
                    if bcrypt.checkpw(raw_token.encode(), candidate.token_hash.encode()):
                        token = candidate
                        break
                if token is None:
                    return JSONResponse({"error": "Invalid API token"}, status_code=401)
                request.state.current_user = "api"
                request.state.api_token = True
                request.state.api_token_id = token.id
                request.state.api_token_owner = token.owner
                request.state.api_token_scopes = [
                    scope.strip()
                    for scope in (token.scopes or "chat").split(",")
                    if scope.strip()
                ]
            finally:
                db.close()
        else:
            request.state.current_user = "tester"
            request.state.api_token = False
        return await call_next(request)

    app.include_router(setup_api_token_routes())
    app.include_router(coding_routes.setup_coding_routes())
    app.include_router(setup_coding_provider_routes(memory_manager=object(), session_manager=object()))
    with TestClient(app) as client:
        yield client, runtime


def _seed_project_and_thread(
    store: ProviderCodingStore,
    *,
    owner: str = "tester",
    thread_id: str | None = None,
) -> tuple[str, str]:
    project_id = f"project-{uuid.uuid4()}"
    thread_id = thread_id or f"thread-{uuid.uuid4()}"
    db = store.SessionLocal()
    try:
        project = CodingProject(
            id=project_id,
            owner=owner,
            name=f"{owner} Project",
            root_path=str(store.workspace),
            default_harness="generic",
        )
        thread = CodingThread(
            id=thread_id,
            project_id=project_id,
            owner=owner,
            title=f"{owner} Thread",
            cwd=str(store.workspace),
            harness_id="generic",
            status="idle",
            metadata_json="{}",
        )
        db.add_all([project, thread])
        db.commit()
        return project_id, thread_id
    finally:
        db.close()


def _seed_run(
    store: ProviderCodingStore,
    *,
    thread_id: str,
    owner: str = "tester",
    status: str = "running",
    run_id: str | None = None,
) -> str:
    run_id = run_id or f"run-{uuid.uuid4()}"
    db = store.SessionLocal()
    try:
        run = CodingRun(
            id=run_id,
            thread_id=thread_id,
            owner=owner,
            harness_id="generic",
            status=status,
            command="echo provider",
            cwd=str(store.workspace),
            metadata_json="{}",
        )
        thread = db.query(CodingThread).filter(CodingThread.id == thread_id).first()
        if thread:
            thread.last_run_id = run_id
            thread.status = status
        db.add(run)
        db.commit()
        return run_id
    finally:
        db.close()


def _set_run_status(store: ProviderCodingStore, run_id: str, status: str) -> None:
    db = store.SessionLocal()
    try:
        run = db.query(CodingRun).filter(CodingRun.id == run_id).first()
        assert run is not None
        run.status = status
        db.commit()
    finally:
        db.close()


def _delete_run(store: ProviderCodingStore, run_id: str) -> None:
    db = store.SessionLocal()
    try:
        db.query(CodingRun).filter(CodingRun.id == run_id).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _delete_thread(store: ProviderCodingStore, thread_id: str) -> None:
    db = store.SessionLocal()
    try:
        db.query(CodingThread).filter(CodingThread.id == thread_id).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _run_auth(token: str, *, project_id: str, thread_id: str, run_id: str) -> dict[str, str]:
    return {
        **_auth(token),
        "X-Odysseus-Project-ID": project_id,
        "X-Odysseus-Thread-ID": thread_id,
        "X-Odysseus-Run-ID": run_id,
    }


def test_provider_tool_catalog_names_match_registered_handlers():
    from src.coding_provider import PROVIDER_TOOL_CATALOG, TOOL_HANDLERS

    assert {item["tool"] for item in PROVIDER_TOOL_CATALOG} == set(TOOL_HANDLERS)


@pytest.mark.parametrize(
    "key",
    [
        "OPENAI_API_KEY",
        "access_token",
        "apiKey",
        "bearerToken",
        "Authorization",
        "authorizationHeader",
        "headers",
        "password",
        "token_hash",
        "state_path",
        "file_path",
        "logPath",
        "run_dir",
        "tmuxSession",
        "session_id",
    ],
)
def test_provider_result_sensitive_key_predicate_catches_variants(key):
    from src.coding_provider import is_sensitive_result_key

    assert is_sensitive_result_key(key) is True


@pytest.mark.parametrize(
    "key",
    ["thread_id", "project_id", "run_id", "status", "name", "title", "token_capabilities", "count", "message"],
)
def test_provider_result_sensitive_key_predicate_preserves_public_fields(key):
    from src.coding_provider import is_sensitive_result_key

    assert is_sensitive_result_key(key) is False


def test_provider_result_sanitizer_recurses_and_preserves_public_ids():
    from src.coding_provider import sanitize_result

    result = sanitize_result(
        {
            "thread_id": "thread-a",
            "project_id": "project-a",
            "run_id": "run-a",
            "status": "running",
            "name": "Provider",
            "title": "Current Thread",
            "OPENAI_API_KEY": "secret",
            "access_token": "secret",
            "apiKey": "secret",
            "bearerToken": "secret",
            "state_path": "/tmp/state.json",
            "nested": {
                "file_path": "/tmp/output.txt",
                "Authorization": "Bearer secret",
                "safe": "value",
                "items": [
                    {"token_hash": "secret", "message": "kept"},
                    {"logPath": "/tmp/run.log", "count": 2},
                ],
            },
        }
    )

    assert result == {
        "thread_id": "thread-a",
        "project_id": "project-a",
        "run_id": "run-a",
        "status": "running",
        "name": "Provider",
        "title": "Current Thread",
        "nested": {
            "safe": "value",
            "items": [
                {"message": "kept"},
                {"count": 2},
            ],
        },
    }


def test_provider_facade_reexports_decomposed_handler_helpers():
    import src.coding_provider as coding_provider
    from src import coding_provider_coding
    from src import coding_provider_memory
    from src import coding_provider_terminal
    from src import coding_provider_thread_messages
    from src import coding_provider_tool_common

    assert coding_provider.call_coding_tool is coding_provider_coding.call_coding_tool
    assert coding_provider.call_model_config_tool is coding_provider_coding.call_model_config_tool
    assert coding_provider.call_memory_tool is coding_provider_memory.call_memory_tool
    assert coding_provider.call_terminal_tool is coding_provider_terminal.call_terminal_tool
    assert coding_provider.run_id_for_terminal_action is coding_provider_terminal.run_id_for_terminal_action
    assert coding_provider.call_thread_messages_tool is coding_provider_thread_messages.call_thread_messages_tool
    assert coding_provider.thread_session_id is coding_provider_thread_messages.thread_session_id
    assert coding_provider.message_dict is coding_provider_thread_messages.message_dict
    assert coding_provider.is_sensitive_result_key is coding_provider_tool_common.is_sensitive_result_key
    assert coding_provider.sanitize_result is coding_provider_tool_common.sanitize_result


@pytest.mark.asyncio
async def test_provider_tool_envelope_aliases_and_sanitizes_results(monkeypatch):
    import src.coding_provider_tools as provider_tools
    from src.coding_provider import ProviderContext, call_provider_tool

    async def fake_handler(context, action, args, services):
        del services
        assert context.thread_id == "thread-a"
        assert action == "some_action"
        assert args == {"visible": "yes"}
        return {
            "keep": "ok",
            "token": "secret",
            "nested": {
                "Authorization": "Bearer secret",
                "log_path": "/tmp/secret.log",
                "safe": "value",
            },
            "items": [{"api_key": "secret", "safe": "item"}],
            "tmux_session": "secret-session",
        }

    monkeypatch.setitem(provider_tools.TOOL_HANDLERS, "fake_tool", fake_handler)
    context = ProviderContext(
        token_id="token-a",
        thread_id="thread-a",
        project_id="project-a",
        owner="tester",
        session_id=None,
        capabilities=frozenset(),
    )

    response = await call_provider_tool(
        context,
        {"action": "fake-tool.some-action", "arguments": {"visible": "yes"}},
        memory_manager=object(),
        session_manager=object(),
    )

    assert response == {
        "ok": True,
        "thread_id": "thread-a",
        "tool": "fake_tool",
        "action": "some_action",
        "result": {
            "keep": "ok",
            "nested": {"safe": "value"},
            "items": [{"safe": "item"}],
        },
    }


def test_provider_token_can_discover_tools_only_through_provider_tool(
    provider_coding_client,
    isolated_provider_coding_store,
):
    client, _runtime = provider_coding_client
    _project_id, thread_id = _seed_project_and_thread(isolated_provider_coding_store)

    minted = client.post(
        "/api/coding/provider/tokens",
        json={"thread_id": thread_id, "capabilities": ["coding.read"]},
    )
    assert minted.status_code == 200
    raw_token = minted.json()["token"]
    assert raw_token.startswith("ody_cp_")

    admin_capabilities = client.get("/api/coding/provider/capabilities", headers=_auth(raw_token))
    assert admin_capabilities.status_code in {401, 403}

    discovered = client.post(
        "/api/coding/provider/tool",
        headers=_auth(raw_token),
        json={"tool": "provider", "action": "list"},
    )
    assert discovered.status_code == 200
    result = discovered.json()["result"]
    assert result["token_capabilities"] == ["coding.read"]
    assert "coding.read" in result["capabilities"]
    tools = {tool["tool"]: tool for tool in result["tools"]}
    assert tools["provider"]["enabled"] is True
    assert tools["coding"]["enabled"] is True
    assert tools["memory"]["enabled"] is False


def test_provider_token_mint_verify_revoke_scopes_coding_reads(
    provider_coding_client,
    isolated_provider_coding_store,
):
    client, _runtime = provider_coding_client
    tester_project_id, _thread_id = _seed_project_and_thread(isolated_provider_coding_store)
    _seed_project_and_thread(isolated_provider_coding_store, owner="other")

    minted = client.post("/api/tokens", data={"name": "Provider token"})
    assert minted.status_code == 200
    body = minted.json()
    raw_token = body["token"]
    assert raw_token.startswith("ody_")
    assert body["owner"] == "tester"
    assert body["scopes"] == ["chat"]

    db = isolated_provider_coding_store.SessionLocal()
    try:
        row = db.query(ApiToken).filter(ApiToken.id == body["id"]).one()
        assert row.token_prefix == raw_token[:8]
        assert row.token_hash != raw_token
        assert bcrypt.checkpw(raw_token.encode(), row.token_hash.encode())
    finally:
        db.close()

    listed = client.get("/api/tokens")
    assert listed.status_code == 200
    assert set(listed.json()[0]) >= {
        "id",
        "name",
        "owner",
        "token_prefix",
        "scopes",
        "is_active",
        "last_used_at",
        "created_at",
    }

    projects = client.get("/api/coding/projects", headers=_auth(raw_token))
    assert projects.status_code == 200
    assert [project["id"] for project in projects.json()["projects"]] == [tester_project_id]

    revoked = client.delete(f"/api/tokens/{body['id']}")
    assert revoked.status_code == 200
    assert revoked.json() == {"status": "deleted"}

    rejected = client.get("/api/coding/projects", headers=_auth(raw_token))
    assert rejected.status_code == 401
    assert rejected.json()["error"] == "Invalid API token"


def test_provider_bearer_token_denies_admin_coding_capabilities(
    provider_coding_client,
    isolated_provider_coding_store,
):
    client, _runtime = provider_coding_client
    _project_id, thread_id = _seed_project_and_thread(isolated_provider_coding_store)
    raw_token = client.post("/api/tokens", data={"name": "Read-only provider"}).json()["token"]

    readable = client.get(f"/api/coding/threads/{thread_id}/events", headers=_auth(raw_token))
    assert readable.status_code == 200
    assert readable.json() == {"events": []}

    denied_run = client.post(
        f"/api/coding/threads/{thread_id}/run",
        headers=_auth(raw_token),
        json={"command": "echo should-not-run"},
    )
    assert denied_run.status_code == 403
    assert denied_run.json()["detail"] == "Admin only"

    denied_delete = client.delete(f"/api/coding/threads/{thread_id}", headers=_auth(raw_token))
    assert denied_delete.status_code == 403
    assert denied_delete.json()["detail"] == "Admin only"


@pytest.mark.asyncio
async def test_provider_manage_coding_tool_read_write_and_thread_messages(
    monkeypatch,
    isolated_provider_coding_store,
):
    import src.coding_runtime as coding_runtime
    from src.coding_runtime import CodingRuntimeService
    from src.tool_implementations import do_manage_coding

    runtime = CodingRuntimeService()
    monkeypatch.setattr(coding_runtime, "get_coding_runtime_service", lambda: runtime)

    created_project = await do_manage_coding(
        json.dumps(
            {
                "action": "create_project",
                "name": "Provider Managed",
                "root_path": str(isolated_provider_coding_store.workspace),
            }
        ),
        owner="provider-user",
    )
    assert created_project["exit_code"] == 0

    created_thread = await do_manage_coding(
        json.dumps(
            {
                "action": "create_thread",
                "project_id": created_project["project"]["id"],
                "title": "Initial title",
                "metadata": {"source": "provider"},
            }
        ),
        owner="provider-user",
    )
    thread_id = created_thread["thread"]["id"]

    updated_thread = await do_manage_coding(
        json.dumps(
            {
                "action": "update_thread",
                "thread_id": thread_id,
                "title": "Provider renamed",
                "metadata": {"source": "provider", "phase": "write"},
            }
        ),
        owner="provider-user",
    )
    assert updated_thread["exit_code"] == 0
    assert updated_thread["thread"]["title"] == "Provider renamed"
    assert updated_thread["thread"]["metadata"] == {"source": "provider", "phase": "write"}

    await runtime.append_event(
        thread_id,
        None,
        "message",
        {"role": "user", "content": "provider message"},
    )

    read_thread = await do_manage_coding(
        json.dumps({"action": "read_thread", "thread_id": thread_id, "include_events": True}),
        owner="provider-user",
    )
    assert read_thread["exit_code"] == 0
    assert read_thread["thread"]["title"] == "Provider renamed"
    assert [(event["kind"], event["payload"]) for event in read_thread["events"]] == [
        ("message", {"role": "user", "content": "provider message"})
    ]

    other_owner = await do_manage_coding(
        json.dumps({"action": "read_thread", "thread_id": thread_id}),
        owner="other-user",
    )
    assert other_owner == {"error": "Thread not found", "exit_code": 1, "status_code": 404}


def test_provider_runtime_launch_env_injects_secrets_without_cross_owner_leak(
    isolated_provider_coding_store,
):
    from src.coding_model_config import build_launch_env, thread_model_config

    db = isolated_provider_coding_store.SessionLocal()
    try:
        endpoint = ModelEndpoint(
            id="anthropic-endpoint",
            owner="tester",
            name="Anthropic",
            base_url="https://api.anthropic.com/v1",
            api_key="anthropic-secret",
            is_enabled=True,
            cached_models=json.dumps(["claude-3-5-sonnet"]),
        )
        thread = CodingThread(
            id="env-thread",
            project_id="missing-project",
            owner="tester",
            title="Env Thread",
            cwd=str(isolated_provider_coding_store.workspace),
            harness_id="codex",
            model_endpoint_id=endpoint.id,
            model="claude-3-5-sonnet",
            status="idle",
            metadata_json="{}",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add_all([endpoint, thread])
        db.commit()

        safe_config = thread_model_config(db, thread)
        assert safe_config["env"] == {
            "OPENAI_BASE_URL": "https://api.anthropic.com/v1",
            "OPENAI_API_BASE": "https://api.anthropic.com/v1",
            "OPENAI_MODEL": "claude-3-5-sonnet",
        }
        assert "OPENAI_API_KEY" not in safe_config["env"]

        launch_env = build_launch_env(db, "tester", endpoint.id, "claude-3-5-sonnet")
        assert launch_env["OPENAI_BASE_URL"] == "https://api.anthropic.com/v1"
        assert launch_env["OPENAI_API_BASE"] == "https://api.anthropic.com/v1"
        assert launch_env["OPENAI_MODEL"] == "claude-3-5-sonnet"
        assert launch_env["OPENAI_API_KEY"] == "anthropic-secret"
        assert launch_env["CODEX_API_KEY"] == "anthropic-secret"
        assert launch_env["ANTHROPIC_API_KEY"] == "anthropic-secret"

        other_owner_env = build_launch_env(db, "other", endpoint.id, "claude-3-5-sonnet")
        assert other_owner_env == {"OPENAI_MODEL": "claude-3-5-sonnet"}
    finally:
        db.close()


def test_provider_ui_api_shape_for_coding_surfaces(provider_coding_client):
    client, _runtime = provider_coding_client

    harnesses = client.get("/api/coding/harnesses")
    assert harnesses.status_code == 200
    first_harness = harnesses.json()["harnesses"][0]
    assert set(first_harness) >= {
        "id",
        "name",
        "description",
        "default_command",
        "stdin_supported",
        "resize_supported",
    }
    if "provider_tools" in first_harness:
        assert first_harness["provider_tools"] in {"none", "cli", "mcp", "extension"}

    token_list = client.get("/api/tokens")
    assert token_list.status_code == 200
    assert token_list.json() == []


def test_pi_extension_source_uses_current_typebox_api(tmp_path):
    from src.coding_provider_launch import build_coding_agent_launch_plan

    plan = build_coding_agent_launch_plan(
        harness_id="pi",
        base_command="pi",
        model=None,
        run_dir=tmp_path,
        default_harness_command=True,
    )

    extension_path = Path(plan.metadata["provider_tools"]["extension_path"])
    extension_source = extension_path.read_text(encoding="utf-8")
    assert plan.metadata["provider_tools"]["mode"] == "pi-extension"
    assert 'import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";' in extension_source
    assert 'import { Type } from "typebox";' in extension_source
    assert "parameters: Type.Object({})" in extension_source
    assert "Type.String({ description:" in extension_source
    assert "Type.Optional(Type.Record(Type.String(), Type.Unknown()" in extension_source
    assert "@oh-my-pi" not in extension_source
    assert "pi.zod" not in extension_source
    assert "z.object" not in extension_source


def test_endpoint_backed_pi_launch_registers_odysseus_provider(tmp_path):
    from src.coding_provider_launch import build_coding_agent_launch_plan

    plan = build_coding_agent_launch_plan(
        harness_id="pi",
        base_command="pi",
        model='model "with spaces"',
        endpoint_url='http://127.0.0.1:8123/openai/"quoted"',
        run_dir=tmp_path,
        default_harness_command=True,
    )

    assert plan.command.startswith("pi --provider odysseus --model 'model \"with spaces\"' --extension ")
    assert plan.metadata["configured_model"] == 'model "with spaces"'
    assert plan.metadata["model_explicit"] is True
    assert plan.metadata["model_arg"] == "--model"
    assert plan.metadata["pi_provider"] == "odysseus"
    assert plan.metadata["pi_provider_source"] == "odysseus-endpoint"

    extension_path = Path(plan.metadata["provider_tools"]["extension_path"])
    extension_source = extension_path.read_text(encoding="utf-8")
    assert 'pi.registerProvider("odysseus"' in extension_source
    assert 'baseUrl: "http://127.0.0.1:8123/openai/\\"quoted\\""' in extension_source
    assert 'id: "model \\"with spaces\\""' in extension_source
    assert 'name: "model \\"with spaces\\""' in extension_source
    assert 'api: "openai-completions"' in extension_source
    assert 'apiKey: process.env.OPENAI_API_KEY || "test"' in extension_source
    assert "authHeader: true" in extension_source
    assert 'input: ["text", "image"]' in extension_source
    assert "cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }" in extension_source
    assert "contextWindow: 262144" in extension_source
    assert "maxTokens: 32768" in extension_source
    assert 'import { Type } from "typebox";' in extension_source
    assert "@oh-my-pi" not in extension_source
    assert "pi.zod" not in extension_source
    assert "z.object" not in extension_source


def _clear_provider_url_env(monkeypatch):
    import os
    import tempfile

    for key in (
        "ODYSSEUS_TOOL_URL",
        "ODYSSEUS_PUBLIC_BASE_URL",
        "ODYSSEUS_BASE_URL",
        "APP_BASE_URL",
        "PUBLIC_BASE_URL",
        "ODYSSEUS_PORT",
        "APP_PORT",
    ):
        monkeypatch.delenv(key, raising=False)
    # Point the published-URL lookup (read by provider_cli._tool_call_url) at a temp
    # dir with no `ody-tool-url` file, so URL-resolution tests exercise the env-based
    # path deterministically regardless of the host's real data dir. Tests that want
    # a published URL override ODYSSEUS_ODY_SOCKET with their own tmp_path.
    monkeypatch.setenv(
        "ODYSSEUS_ODY_SOCKET",
        os.path.join(tempfile.gettempdir(), "odysseus-test-no-published-url", "ody.sock"),
    )


def test_publish_and_read_provider_tool_url_roundtrip(monkeypatch, tmp_path):
    import src.coding_provider_bridge as provider_bridge

    _clear_provider_url_env(monkeypatch)
    monkeypatch.setenv("ODYSSEUS_ODY_SOCKET", str(tmp_path / "ody.sock"))
    monkeypatch.setenv("ODYSSEUS_PORT", "61703")

    written = provider_bridge.publish_provider_tool_url()
    assert written == "http://localhost:61703/api/coding/provider/tool"
    assert (tmp_path / "ody-tool-url").read_text(encoding="utf-8").strip() == written
    assert provider_bridge.read_published_provider_tool_url() == written

    provider_bridge.unpublish_provider_tool_url()
    assert provider_bridge.read_published_provider_tool_url() == ""


def test_tool_call_url_prefers_published_over_stale_env(monkeypatch, tmp_path):
    """A backend restart changes the dynamic port, so the agent's baked-in
    ODYSSEUS_TOOL_URL goes stale. `odysseus-tool` must re-resolve from the published
    file (current port), not the dead port in its env — the fix for the
    '[Errno 61] Connection refused' a restart caused for already-running agents."""
    import src.coding_provider_cli as provider_cli

    _clear_provider_url_env(monkeypatch)
    monkeypatch.setenv("ODYSSEUS_ODY_SOCKET", str(tmp_path / "ody.sock"))
    # Backend (re)started on a new port and published it next to the ody socket:
    (tmp_path / "ody-tool-url").write_text(
        "http://127.0.0.1:61703/api/coding/provider/tool", encoding="utf-8"
    )
    # The agent still has the OLD (now-dead) port baked into its env from launch:
    monkeypatch.setenv("ODYSSEUS_TOOL_URL", "http://127.0.0.1:55807/api/coding/provider/tool")

    assert provider_cli._tool_call_url() == "http://127.0.0.1:61703/api/coding/provider/tool"


def test_tool_call_url_falls_back_to_env_when_nothing_published(monkeypatch, tmp_path):
    import src.coding_provider_cli as provider_cli

    _clear_provider_url_env(monkeypatch)
    # Stable dir exists but the backend hasn't published (e.g. dev run): use the env.
    monkeypatch.setenv("ODYSSEUS_ODY_SOCKET", str(tmp_path / "ody.sock"))
    monkeypatch.setenv("ODYSSEUS_TOOL_URL", "http://127.0.0.1:55807/api/coding/provider/tool")

    assert provider_cli._tool_call_url() == "http://127.0.0.1:55807/api/coding/provider/tool"


@pytest.mark.parametrize(
    "configured_url",
    [
        "http://example.test/api/coding/provider/tool",
        "http://example.test/api/coding/provider",
        "http://example.test/",
        "http://example.test",
    ],
)
def test_provider_tool_url_normalizes_provider_endpoint_forms(monkeypatch, configured_url):
    import src.coding_provider_bridge as provider_bridge

    _clear_provider_url_env(monkeypatch)
    monkeypatch.setenv("ODYSSEUS_TOOL_URL", configured_url)

    assert provider_bridge.provider_tool_url() == "http://example.test/api/coding/provider/tool"


@pytest.mark.parametrize(
    "env_key",
    [
        "ODYSSEUS_PUBLIC_BASE_URL",
        "ODYSSEUS_BASE_URL",
        "APP_BASE_URL",
        "PUBLIC_BASE_URL",
    ],
)
def test_provider_tool_url_uses_public_base_url_envs(monkeypatch, env_key):
    import src.coding_provider_bridge as provider_bridge

    _clear_provider_url_env(monkeypatch)
    monkeypatch.setenv(env_key, "https://public.example.test/")

    assert provider_bridge.provider_tool_url() == "https://public.example.test/api/coding/provider/tool"


def test_provider_tool_url_prefers_exact_tool_url_over_base_url_envs(monkeypatch):
    import src.coding_provider_bridge as provider_bridge

    _clear_provider_url_env(monkeypatch)
    monkeypatch.setenv("ODYSSEUS_TOOL_URL", "https://tool.example.test/api/coding/provider")
    monkeypatch.setenv("ODYSSEUS_PUBLIC_BASE_URL", "https://public.example.test")

    assert provider_bridge.provider_tool_url() == "https://tool.example.test/api/coding/provider/tool"


def test_provider_tool_url_falls_back_to_app_port_then_default(monkeypatch):
    import src.coding_provider_bridge as provider_bridge

    _clear_provider_url_env(monkeypatch)
    monkeypatch.setenv("APP_PORT", "8123")
    assert provider_bridge.provider_tool_url() == "http://localhost:8123/api/coding/provider/tool"

    monkeypatch.delenv("APP_PORT")
    assert provider_bridge.provider_tool_url() == "http://localhost:7000/api/coding/provider/tool"


def test_provider_tool_url_prefers_odysseus_port_over_default(monkeypatch):
    # Regression: the macOS launcher binds a dynamic free port and exports it as
    # ODYSSEUS_PORT. Defaulting to :7000 points the harness at AirPlay Receiver.
    import src.coding_provider_bridge as provider_bridge

    _clear_provider_url_env(monkeypatch)
    monkeypatch.setenv("ODYSSEUS_PORT", "54321")
    assert provider_bridge.provider_tool_url() == "http://localhost:54321/api/coding/provider/tool"

    # ODYSSEUS_PORT (the real bound port) wins over APP_PORT.
    monkeypatch.setenv("APP_PORT", "8123")
    assert provider_bridge.provider_tool_url() == "http://localhost:54321/api/coding/provider/tool"


@pytest.mark.parametrize(
    "configured_url",
    [
        "http://example.test/api/coding/provider/tool",
        "http://example.test/api/coding/provider",
        "http://example.test/",
        "http://example.test",
    ],
)
def test_provider_cli_tool_call_url_uses_provider_endpoint_without_call_suffix(
    monkeypatch,
    configured_url,
):
    import src.coding_provider_cli as provider_cli

    _clear_provider_url_env(monkeypatch)
    monkeypatch.setenv("ODYSSEUS_TOOL_URL", configured_url)

    assert provider_cli._tool_call_url() == "http://example.test/api/coding/provider/tool"


def test_provider_cli_uses_canonical_odysseus_tool_env(monkeypatch, capsys):
    import src.coding_provider_cli as provider_cli

    monkeypatch.setenv("ODYSSEUS_TOOL_URL", "http://example.test/api/coding/provider/tool")
    monkeypatch.setenv("ODYSSEUS_TOOL_TOKEN", "ody_cp_canonical")
    monkeypatch.setenv("ODYSSEUS_THREAD_ID", "thread-canonical")
    monkeypatch.setenv("ODYSSEUS_PROJECT_ID", "project-canonical")
    monkeypatch.setenv("ODYSSEUS_RUN_ID", "run-canonical")
    monkeypatch.setenv("ODYSSEUS_PROVIDER_URL", "http://wrong.test/provider")
    monkeypatch.setenv("ODYSSEUS_PROVIDER_TOKEN", "ody_cp_wrong")

    captured = {}

    class _FakeResponse:
        headers = {"content-type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"capabilities":["coding.read"]}'

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["authorization"] = request.get_header("Authorization")
        captured["thread_id"] = request.get_header("X-odysseus-thread-id")
        captured["project_id"] = request.get_header("X-odysseus-project-id")
        captured["run_id"] = request.get_header("X-odysseus-run-id")
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr(provider_cli.urllib.request, "urlopen", fake_urlopen)

    provider_cli.cmd_list(SimpleNamespace(pretty=False))

    assert json.loads(capsys.readouterr().out) == {"capabilities": ["coding.read"]}
    assert captured == {
        "url": "http://example.test/api/coding/provider/tool",
        "method": "POST",
        "body": {"tool": "provider", "action": "list", "args": {}},
        "authorization": "Bearer ody_cp_canonical",
        "thread_id": "thread-canonical",
        "project_id": "project-canonical",
        "run_id": "run-canonical",
        "timeout": 60,
    }

    env_status = provider_cli.bridge_env_status()
    assert set(env_status) == {
        "ODYSSEUS_TOOL_URL",
        "ODYSSEUS_TOOL_TOKEN",
        "ODYSSEUS_THREAD_ID",
        "ODYSSEUS_PROJECT_ID",
        "ODYSSEUS_RUN_ID",
        "configured",
    }
    assert env_status["ODYSSEUS_TOOL_TOKEN"] == "***"


@pytest.mark.asyncio
async def test_provider_cli_mcp_server_uses_sdk_stdio_list_tools():
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "scripts" / "odysseus-tool"
    server_params = StdioServerParameters(
        command=sys.executable,
        args=[str(script_path), "mcp-server"],
        cwd=repo_root,
        env={
            **os.environ,
            "PYTHONPATH": str(repo_root),
            "ODYSSEUS_TOOL_URL": "http://localhost/api/coding/provider/tool",
            "ODYSSEUS_TOOL_TOKEN": "ody_cp_sdk_stdio_test",
            "ODYSSEUS_THREAD_ID": "thread-sdk-stdio",
            "ODYSSEUS_PROJECT_ID": "project-sdk-stdio",
            "ODYSSEUS_RUN_ID": "run-sdk-stdio",
        },
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()

    tool_names = {tool.name for tool in listed.tools}
    assert {"odysseus_provider", "odysseus_list_tools"} <= tool_names


def test_provider_bridge_run_env_uses_canonical_names_only(monkeypatch):
    import src.coding_provider_bridge as provider_bridge

    monkeypatch.setenv("ODYSSEUS_TOOL_URL", "http://localhost:7000/api/coding/provider/tool")
    # Pin the ody control-plane socket so the injected ODYSSEUS_ODY_SOCKET is deterministic.
    monkeypatch.setenv("ODYSSEUS_ODY_SOCKET", "/tmp/ody-canonical.sock")
    monkeypatch.setattr(
        provider_bridge,
        "_mint_backend_run_token",
        lambda scope: {"token": "ody_cp_backend", "token_id": "provider-token-id"},
    )
    service = provider_bridge.CodingProviderBridgeService()

    env = service.create_run_env(
        provider_bridge.ProviderBridgeScope(
            owner="tester",
            project_id="project-canonical",
            thread_id="thread-canonical",
            run_id="run-canonical",
        )
    )

    # create_run_env now also injects the ody control-plane socket + owner so an
    # `ody` invoked inside a pane targets the right socket/owner.
    assert env == {
        "ODYSSEUS_TOOL_URL": "http://localhost:7000/api/coding/provider/tool",
        "ODYSSEUS_TOOL_TOKEN": "ody_cp_backend",
        "ODYSSEUS_THREAD_ID": "thread-canonical",
        "ODYSSEUS_PROJECT_ID": "project-canonical",
        "ODYSSEUS_RUN_ID": "run-canonical",
        "ODYSSEUS_ODY_SOCKET": "/tmp/ody-canonical.sock",
        "ODYSSEUS_OWNER": "tester",
    }
    assert not any(key.startswith("ODYSSEUS_PROVIDER_") for key in env)

    launch_env = provider_bridge.with_scripts_on_path({"PATH": f"/usr/bin{os.pathsep}{provider_bridge.scripts_dir()}", **env})
    assert launch_env["PATH"].split(os.pathsep)[0] == str(provider_bridge.scripts_dir())
    # The canonical provider-bridge keys all flow through unchanged.
    assert {key: launch_env[key] for key in provider_bridge.PROVIDER_BRIDGE_ENV_KEYS} == {
        key: env[key] for key in provider_bridge.PROVIDER_BRIDGE_ENV_KEYS
    }


def test_provider_token_discovery_allows_provider_scoped_bearer_without_admin(
    provider_coding_client,
    isolated_provider_coding_store,
):
    from src.coding_provider import mint_provider_token

    client, _runtime = provider_coding_client
    _project_id, thread_id = _seed_project_and_thread(isolated_provider_coding_store)
    minted = mint_provider_token(
        owner="tester",
        thread_id=thread_id,
        capabilities=["coding.read"],
        ttl_seconds=300,
    )

    response = client.post(
        "/api/coding/provider/tool",
        headers=_auth(minted["token"]),
        json={"tool": "provider", "action": "list", "args": {}},
    )

    assert response.status_code == 200
    result = response.json()["result"]
    assert {"coding.read", "thread.messages.read"} <= set(result["capabilities"])
    assert result["token_capabilities"] == ["coding.read"]


def test_public_mint_metadata_cannot_create_run_scoped_behavior(
    provider_coding_client,
    isolated_provider_coding_store,
):
    client, _runtime = provider_coding_client
    _project_id, thread_id = _seed_project_and_thread(isolated_provider_coding_store)

    minted = client.post(
        "/api/coding/provider/tokens",
        json={
            "thread_id": thread_id,
            "capabilities": ["coding.read"],
            "metadata": {
                "run_scoped": True,
                "run_id": "missing-run",
                "credential_class": "run",
                "_odysseus_credential_class": "run",
                "_odysseus_run_id": "missing-run",
            },
        },
    )
    assert minted.status_code == 200
    body = minted.json()
    assert body["provider_token"]["credential_class"] == "explicit"
    assert body["provider_token"]["metadata"] == {}

    response = client.post(
        "/api/coding/provider/tool",
        headers=_auth(body["token"]),
        json={"tool": "provider", "action": "list", "args": {}},
    )

    assert response.status_code == 200
    assert response.json()["result"]["token_capabilities"] == ["coding.read"]


def test_run_provider_credential_requires_matching_scope_headers(
    provider_coding_client,
    isolated_provider_coding_store,
):
    from src.coding_provider_tokens import mint_run_provider_token

    client, _runtime = provider_coding_client
    project_id, thread_id = _seed_project_and_thread(isolated_provider_coding_store)
    run_id = _seed_run(isolated_provider_coding_store, thread_id=thread_id)
    minted = mint_run_provider_token(
        owner="tester",
        project_id=project_id,
        thread_id=thread_id,
        run_id=run_id,
        capabilities=["coding.read"],
    )

    response = client.post(
        "/api/coding/provider/tool",
        headers=_run_auth(minted["token"], project_id=project_id, thread_id=thread_id, run_id=run_id),
        json={"tool": "provider", "action": "list", "args": {}},
    )

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["token_capabilities"] == ["coding.read"]


def test_run_provider_terminal_read_defaults_to_scoped_run_and_explicit_keeps_last_run(
    provider_coding_client,
    isolated_provider_coding_store,
):
    from src.coding_provider_tokens import mint_provider_token, mint_run_provider_token

    client, _runtime = provider_coding_client
    project_id, thread_id = _seed_project_and_thread(isolated_provider_coding_store)
    scoped_run_id = _seed_run(isolated_provider_coding_store, thread_id=thread_id)
    last_run_id = _seed_run(isolated_provider_coding_store, thread_id=thread_id)
    run_minted = mint_run_provider_token(
        owner="tester",
        project_id=project_id,
        thread_id=thread_id,
        run_id=scoped_run_id,
        capabilities=["terminal.read"],
    )
    explicit_minted = mint_provider_token(
        owner="tester",
        thread_id=thread_id,
        capabilities=["terminal.read"],
    )

    run_response = client.post(
        "/api/coding/provider/tool",
        headers=_run_auth(run_minted["token"], project_id=project_id, thread_id=thread_id, run_id=scoped_run_id),
        json={"tool": "terminal", "action": "read", "args": {}},
    )
    explicit_response = client.post(
        "/api/coding/provider/tool",
        headers=_auth(explicit_minted["token"]),
        json={"tool": "terminal", "action": "read", "args": {}},
    )

    assert run_response.status_code == 200
    assert run_response.json()["result"]["run"]["id"] == scoped_run_id
    assert explicit_response.status_code == 200
    assert explicit_response.json()["result"]["run"]["id"] == last_run_id


@pytest.mark.parametrize(
    ("action", "action_args"),
    [
        ("read", {}),
        ("stdin", {"data": "input\n"}),
        ("resize", {"cols": 100, "rows": 30}),
        ("stop", {"reason": "test stop"}),
    ],
)
def test_run_provider_terminal_actions_reject_same_thread_wrong_run(
    provider_coding_client,
    isolated_provider_coding_store,
    action,
    action_args,
):
    from src.coding_provider_tokens import mint_run_provider_token

    client, _runtime = provider_coding_client
    project_id, thread_id = _seed_project_and_thread(isolated_provider_coding_store)
    scoped_run_id = _seed_run(isolated_provider_coding_store, thread_id=thread_id)
    sibling_run_id = _seed_run(isolated_provider_coding_store, thread_id=thread_id)
    minted = mint_run_provider_token(
        owner="tester",
        project_id=project_id,
        thread_id=thread_id,
        run_id=scoped_run_id,
        capabilities=[
            "terminal.read",
            "terminal.stdin",
            "terminal.resize",
            "terminal.stop",
        ],
    )
    args = dict(action_args)
    args["run_id"] = sibling_run_id

    response = client.post(
        "/api/coding/provider/tool",
        headers=_run_auth(minted["token"], project_id=project_id, thread_id=thread_id, run_id=scoped_run_id),
        json={"tool": "terminal", "action": action, "args": args},
    )

    assert response.status_code == 403
    assert "cannot access a different terminal run" in response.json()["detail"]


def test_run_provider_terminal_start_is_rejected(
    provider_coding_client,
    isolated_provider_coding_store,
):
    from src.coding_provider_tokens import mint_run_provider_token

    client, _runtime = provider_coding_client
    project_id, thread_id = _seed_project_and_thread(isolated_provider_coding_store)
    run_id = _seed_run(isolated_provider_coding_store, thread_id=thread_id)
    minted = mint_run_provider_token(
        owner="tester",
        project_id=project_id,
        thread_id=thread_id,
        run_id=run_id,
        capabilities=["terminal.start"],
    )

    response = client.post(
        "/api/coding/provider/tool",
        headers=_run_auth(minted["token"], project_id=project_id, thread_id=thread_id, run_id=run_id),
        json={"tool": "terminal", "action": "start", "args": {"command": "echo should-not-start"}},
    )

    assert response.status_code == 403
    assert "cannot start terminal runs" in response.json()["detail"]
    db = isolated_provider_coding_store.SessionLocal()
    try:
        run_count = db.query(CodingRun).filter(CodingRun.thread_id == thread_id).count()
    finally:
        db.close()
    assert run_count == 1


def test_run_provider_credential_rejects_missing_run_header(
    provider_coding_client,
    isolated_provider_coding_store,
):
    from src.coding_provider_tokens import mint_run_provider_token

    client, _runtime = provider_coding_client
    project_id, thread_id = _seed_project_and_thread(isolated_provider_coding_store)
    run_id = _seed_run(isolated_provider_coding_store, thread_id=thread_id)
    minted = mint_run_provider_token(
        owner="tester",
        project_id=project_id,
        thread_id=thread_id,
        run_id=run_id,
        capabilities=["coding.read"],
    )
    headers = _run_auth(minted["token"], project_id=project_id, thread_id=thread_id, run_id=run_id)
    headers.pop("X-Odysseus-Run-ID")

    response = client.post(
        "/api/coding/provider/tool",
        headers=headers,
        json={"tool": "provider", "action": "list", "args": {}},
    )

    assert response.status_code == 401
    assert "requires run, thread, and project headers" in response.json()["detail"]


@pytest.mark.parametrize(
    ("header_name", "wrong_value"),
    [
        ("X-Odysseus-Run-ID", "wrong-run"),
        ("X-Odysseus-Thread-ID", "wrong-thread"),
        ("X-Odysseus-Project-ID", "wrong-project"),
    ],
)
def test_run_provider_credential_rejects_wrong_scope_headers(
    provider_coding_client,
    isolated_provider_coding_store,
    header_name,
    wrong_value,
):
    from src.coding_provider_tokens import mint_run_provider_token

    client, _runtime = provider_coding_client
    project_id, thread_id = _seed_project_and_thread(isolated_provider_coding_store)
    run_id = _seed_run(isolated_provider_coding_store, thread_id=thread_id)
    minted = mint_run_provider_token(
        owner="tester",
        project_id=project_id,
        thread_id=thread_id,
        run_id=run_id,
        capabilities=["coding.read"],
    )
    headers = _run_auth(minted["token"], project_id=project_id, thread_id=thread_id, run_id=run_id)
    headers[header_name] = wrong_value

    response = client.post(
        "/api/coding/provider/tool",
        headers=headers,
        json={"tool": "provider", "action": "list", "args": {}},
    )

    assert response.status_code == 401
    assert "scope mismatch" in response.json()["detail"]


@pytest.mark.parametrize("status", ["stopping", "exited", "completed", "cancelled", "failed", "deleted"])
def test_run_provider_credential_rejects_inactive_run_statuses(
    provider_coding_client,
    isolated_provider_coding_store,
    status,
):
    from src.coding_provider_tokens import mint_run_provider_token

    client, _runtime = provider_coding_client
    project_id, thread_id = _seed_project_and_thread(isolated_provider_coding_store)
    run_id = _seed_run(isolated_provider_coding_store, thread_id=thread_id, status="running")
    minted = mint_run_provider_token(
        owner="tester",
        project_id=project_id,
        thread_id=thread_id,
        run_id=run_id,
        capabilities=["coding.read"],
    )
    _set_run_status(isolated_provider_coding_store, run_id, status)

    response = client.post(
        "/api/coding/provider/tool",
        headers=_run_auth(minted["token"], project_id=project_id, thread_id=thread_id, run_id=run_id),
        json={"tool": "provider", "action": "list", "args": {}},
    )

    assert response.status_code == 401
    assert "run is not active" in response.json()["detail"]


def test_run_provider_credential_rejects_deleted_run(
    provider_coding_client,
    isolated_provider_coding_store,
):
    from src.coding_provider_tokens import mint_run_provider_token

    client, _runtime = provider_coding_client
    project_id, thread_id = _seed_project_and_thread(isolated_provider_coding_store)
    run_id = _seed_run(isolated_provider_coding_store, thread_id=thread_id)
    minted = mint_run_provider_token(
        owner="tester",
        project_id=project_id,
        thread_id=thread_id,
        run_id=run_id,
        capabilities=["coding.read"],
    )
    _delete_run(isolated_provider_coding_store, run_id)

    response = client.post(
        "/api/coding/provider/tool",
        headers=_run_auth(minted["token"], project_id=project_id, thread_id=thread_id, run_id=run_id),
        json={"tool": "provider", "action": "list", "args": {}},
    )

    assert response.status_code == 401
    assert "no longer has an active run" in response.json()["detail"]


def test_run_provider_credential_rejects_deleted_thread(
    provider_coding_client,
    isolated_provider_coding_store,
):
    from src.coding_provider_tokens import mint_run_provider_token

    client, _runtime = provider_coding_client
    project_id, thread_id = _seed_project_and_thread(isolated_provider_coding_store)
    run_id = _seed_run(isolated_provider_coding_store, thread_id=thread_id)
    minted = mint_run_provider_token(
        owner="tester",
        project_id=project_id,
        thread_id=thread_id,
        run_id=run_id,
        capabilities=["coding.read"],
    )
    _delete_thread(isolated_provider_coding_store, thread_id)

    response = client.post(
        "/api/coding/provider/tool",
        headers=_run_auth(minted["token"], project_id=project_id, thread_id=thread_id, run_id=run_id),
        json={"tool": "provider", "action": "list", "args": {}},
    )

    assert response.status_code == 401
    assert "no longer has a thread" in response.json()["detail"]


def test_backend_mint_failure_does_not_inject_unusable_odyt_http_token(monkeypatch):
    import src.coding_provider_bridge as provider_bridge

    monkeypatch.setenv("ODYSSEUS_TOOL_URL", "http://localhost:7000/api/coding/provider/tool")
    monkeypatch.setattr(provider_bridge, "_mint_backend_run_token", lambda scope: None)
    service = provider_bridge.CodingProviderBridgeService()

    try:
        env = service.create_run_env(
            provider_bridge.ProviderBridgeScope(
                owner="tester",
                project_id="project-mint-failure",
                thread_id="thread-mint-failure",
                run_id="run-mint-failure",
            )
        )
    except Exception as exc:
        assert exc
        return

    token = env.get("ODYSSEUS_TOOL_TOKEN", "")
    assert not token.startswith("odyt_")
    if token:
        assert token.startswith("ody_cp_")


def test_run_provider_task_acquire_and_release(
    provider_coding_client,
    isolated_provider_coding_store,
):
    from src.coding_provider_tokens import mint_run_provider_token
    from src.coding_task_slots import get_task_slot_service

    # Isolate the process-singleton slot service from any other test's state.
    svc = get_task_slot_service()
    svc._holders.clear()
    svc._waiters.clear()
    svc._limits.clear()

    client, _runtime = provider_coding_client
    project_id, thread_id = _seed_project_and_thread(isolated_provider_coding_store)
    run_id = _seed_run(isolated_provider_coding_store, thread_id=thread_id)
    minted = mint_run_provider_token(
        owner="tester",
        project_id=project_id,
        thread_id=thread_id,
        run_id=run_id,
        capabilities=["task.acquire", "task.release", "task.heartbeat"],
    )
    headers = _run_auth(minted["token"], project_id=project_id, thread_id=thread_id, run_id=run_id)

    acquired = client.post(
        "/api/coding/provider/tool",
        headers=headers,
        json={"tool": "task", "action": "acquire", "args": {"wait_seconds": 2}},
    )
    assert acquired.status_code == 200
    result = acquired.json()["result"]
    assert result["granted"] is True
    slot_id = result["slot_id"]
    assert slot_id

    released = client.post(
        "/api/coding/provider/tool",
        headers=headers,
        json={"tool": "task", "action": "release", "args": {"slot_id": slot_id}},
    )
    assert released.status_code == 200
    assert released.json()["result"]["released"] is True

    svc._holders.clear()
    svc._waiters.clear()


def test_run_provider_task_acquire_requires_capability(
    provider_coding_client,
    isolated_provider_coding_store,
):
    from src.coding_provider_tokens import mint_run_provider_token

    client, _runtime = provider_coding_client
    project_id, thread_id = _seed_project_and_thread(isolated_provider_coding_store)
    run_id = _seed_run(isolated_provider_coding_store, thread_id=thread_id)
    minted = mint_run_provider_token(
        owner="tester",
        project_id=project_id,
        thread_id=thread_id,
        run_id=run_id,
        capabilities=["terminal.read"],  # no task.* capability
    )

    response = client.post(
        "/api/coding/provider/tool",
        headers=_run_auth(minted["token"], project_id=project_id, thread_id=thread_id, run_id=run_id),
        json={"tool": "task", "action": "acquire", "args": {}},
    )
    assert response.status_code == 403
    assert "task.acquire" in response.json()["detail"]
