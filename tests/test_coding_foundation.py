from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shlex
import sqlite3
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

_MODULE_DATA_DIR = Path(tempfile.mkdtemp(prefix="odysseus-coding-tests-")) / "data"
_MODULE_DATA_DIR.mkdir(parents=True, exist_ok=True)
os.environ["DATA_DIR"] = str(_MODULE_DATA_DIR)
os.environ["DATABASE_URL"] = f"sqlite:///{_MODULE_DATA_DIR / 'app.sqlite'}"

from core.database import (  # noqa: E402
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
from src.coding_harnesses import build_harness_command, list_harnesses  # noqa: E402
from src.coding_model_config import build_safe_cli_env  # noqa: E402
from src.coding_provider_launch import build_coding_agent_launch_plan, codex_mcp_config_arg  # noqa: E402


CODING_TABLES = {
    "coding_projects",
    "coding_threads",
    "coding_runs",
    "coding_thread_events",
    "coding_model_config_snapshots",
    "coding_provider_tokens",
}


@dataclass
class IsolatedCodingStore:
    data_dir: Path
    db_path: Path
    engine: object
    SessionLocal: object
    workspace: Path


@pytest.fixture
def isolated_coding_store(monkeypatch, tmp_path):
    import core.database as database
    import routes.coding_routes as coding_routes
    import src.coding_provider as coding_provider
    import src.coding_runtime as coding_runtime

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_path = data_dir / "coding.sqlite"
    database_url = f"sqlite:///{db_path}"

    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("AUTH_ENABLED", "false")

    engine = create_engine(database_url, connect_args={"check_same_thread": False})
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Session.__table__,
            ModelEndpoint.__table__,
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
    monkeypatch.setattr(
        coding_routes,
        "derive_odysseus_model_config",
        lambda _db, _owner: {
            "endpoint_id": "",
            "endpoint_url": "",
            "model": "",
            "env": {},
            "source": "test",
        },
    )
    monkeypatch.setattr(
        coding_routes,
        "thread_model_config",
        lambda _db, _thread: {
            "endpoint_id": "",
            "endpoint_url": "",
            "model": "",
            "env": {},
            "source": "test",
        },
    )

    try:
        yield IsolatedCodingStore(
            data_dir=data_dir,
            db_path=db_path,
            engine=engine,
            SessionLocal=TestingSessionLocal,
            workspace=workspace,
        )
    finally:
        engine.dispose()


@pytest.fixture
def coding_client(monkeypatch, isolated_coding_store):
    import routes.coding_routes as coding_routes
    from src.coding_runtime import CodingRuntimeService

    runtime = CodingRuntimeService()
    monkeypatch.setattr(coding_routes, "get_coding_runtime_service", lambda: runtime)

    app = FastAPI()

    @app.middleware("http")
    async def stamp_test_user(request, call_next):
        request.state.current_user = "tester"
        return await call_next(request)

    app.include_router(coding_routes.setup_coding_routes())
    with TestClient(app) as client:
        yield client, runtime


def _seed_project_and_thread(
    store: IsolatedCodingStore,
    *,
    owner: str = "tester",
    project_id: str | None = None,
    thread_id: str | None = None,
    title: str = "Coding Thread",
    pinned: bool = False,
) -> tuple[str, str]:
    create_project = project_id is None
    project_id = project_id or f"project-{uuid.uuid4()}"
    thread_id = thread_id or f"thread-{uuid.uuid4()}"
    db = store.SessionLocal()
    try:
        rows = []
        if create_project:
            rows.append(
                CodingProject(
                    id=project_id,
                    owner=owner,
                    name="Test Project",
                    root_path=str(store.workspace),
                    default_harness="generic",
                )
            )
        thread = CodingThread(
            id=thread_id,
            project_id=project_id,
            owner=owner,
            title=title,
            cwd=str(store.workspace),
            harness_id="generic",
            pinned_at=datetime.utcnow() if pinned else None,
            status="idle",
            metadata_json="{}",
        )
        rows.append(thread)
        db.add_all(rows)
        db.commit()
        return project_id, thread_id
    finally:
        db.close()


def _run_statuses(store: IsolatedCodingStore, *run_ids: str) -> dict[str, str]:
    db = store.SessionLocal()
    try:
        rows = db.query(CodingRun).filter(CodingRun.id.in_(run_ids)).all()
        return {row.id: row.status for row in rows}
    finally:
        db.close()


def _load_run(store: IsolatedCodingStore, run_id: str) -> CodingRun:
    db = store.SessionLocal()
    try:
        run = db.query(CodingRun).filter(CodingRun.id == run_id).first()
        assert run is not None
        for attr in ("status", "started_at", "finished_at", "exit_code"):
            getattr(run, attr)
        db.expunge(run)
        return run
    finally:
        db.close()


async def _wait_for(predicate, timeout: float = 5.0, interval: float = 0.03):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("Timed out waiting for coding runtime condition")
        await asyncio.sleep(interval)


def test_required_coding_harnesses_are_registered_with_stable_ids():
    harnesses = list_harnesses()
    ids = [item["id"] for item in harnesses]

    assert ids == ["generic", "pi", "codex", "claude", "opencode", "omp", "hermes", "custom"]
    assert all(item["name"] for item in harnesses)
    assert all("stdin_supported" in item and "resize_supported" in item for item in harnesses)


def test_harness_command_uses_overrides_defaults_and_rejects_unknown_ids():
    assert build_harness_command("generic", "pwd", {}) == "pwd"
    assert build_harness_command("codex", "", {}) == "codex"
    assert build_harness_command("custom", "", {"command": "npm test"}) == "npm test"
    assert build_harness_command("pi", "", {}) == "pi"
    assert build_harness_command("pi", "pi --version", {}) == "pi --version"
    assert build_harness_command("omp", "", {}) == "omp"
    assert "odysseus-tool run --" not in build_harness_command("custom", "", {"command": "npm test"})

    with pytest.raises(ValueError, match="Unknown coding harness"):
        build_harness_command("missing", "", {})
    with pytest.raises(ValueError, match="requires a command"):
        build_harness_command("custom", "", {})


def test_agent_launch_plan_injects_explicit_models_and_odysseus_tools(tmp_path, monkeypatch):
    pi_plan = build_coding_agent_launch_plan(
        harness_id="pi",
        base_command="pi",
        model="gpt-5.1-codex",
        run_dir=tmp_path,
        default_harness_command=True,
    )
    assert pi_plan.command.startswith("pi --model gpt-5.1-codex --extension ")
    assert pi_plan.metadata["model_explicit"] is True
    assert pi_plan.metadata["provider_tools"]["mode"] == "pi-extension"
    assert "pi_provider" not in pi_plan.metadata
    extension_path = Path(pi_plan.metadata["provider_tools"]["extension_path"])
    assert extension_path.exists()
    extension_source = extension_path.read_text(encoding="utf-8")
    assert 'import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";' in extension_source
    assert 'import { Type } from "typebox";' in extension_source
    assert "@oh-my-pi" not in extension_source
    assert "pi.zod" not in extension_source
    assert "parameters: Type.Object({})" in extension_source
    assert "Type.Optional(Type.Record(Type.String(), Type.Unknown()" in extension_source
    assert 'name: "odysseus_list_tools"' in extension_source
    assert 'name: "odysseus_provider"' in extension_source
    assert "pi.registerProvider" not in extension_source

    endpoint_pi_plan = build_coding_agent_launch_plan(
        harness_id="pi",
        base_command="pi",
        model="Qwopus3.6-35B-A3B-v1-oQ6_8-vlm",
        endpoint_url="http://127.0.0.1:11434/v1",
        run_dir=tmp_path,
        default_harness_command=True,
    )
    assert endpoint_pi_plan.command.startswith(
        "pi --provider odysseus --model Qwopus3.6-35B-A3B-v1-oQ6_8-vlm --extension "
    )
    assert endpoint_pi_plan.metadata["model_explicit"] is True
    assert endpoint_pi_plan.metadata["model_arg"] == "--model"
    assert endpoint_pi_plan.metadata["configured_model"] == "Qwopus3.6-35B-A3B-v1-oQ6_8-vlm"
    assert endpoint_pi_plan.metadata["pi_provider"] == "odysseus"
    assert endpoint_pi_plan.metadata["pi_provider_source"] == "odysseus-endpoint"
    endpoint_extension_path = Path(endpoint_pi_plan.metadata["provider_tools"]["extension_path"])
    endpoint_extension_source = endpoint_extension_path.read_text(encoding="utf-8")
    assert 'pi.registerProvider("odysseus"' in endpoint_extension_source
    assert 'baseUrl: "http://127.0.0.1:11434/v1"' in endpoint_extension_source
    assert 'id: "Qwopus3.6-35B-A3B-v1-oQ6_8-vlm"' in endpoint_extension_source
    assert 'name: "Qwopus3.6-35B-A3B-v1-oQ6_8-vlm"' in endpoint_extension_source
    assert 'apiKey: process.env.OPENAI_API_KEY || "test"' in endpoint_extension_source
    assert "cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }" in endpoint_extension_source
    assert 'import { Type } from "typebox";' in endpoint_extension_source
    assert "@oh-my-pi" not in endpoint_extension_source
    assert "pi.zod" not in endpoint_extension_source

    codex_plan = build_coding_agent_launch_plan(
        harness_id="codex",
        base_command="codex",
        model="gpt-5.1-codex",
        endpoint_url="https://proxy.example.test/v1",
        run_dir=tmp_path,
        default_harness_command=True,
    )
    assert "codex --model gpt-5.1-codex" in codex_plan.command
    assert "--config" in codex_plan.command
    assert "openai_base_url=" in codex_plan.command
    source_tool_path = str(Path(__file__).resolve().parents[1] / "scripts" / "odysseus-tool")
    source_mcp_config = codex_mcp_config_arg()
    assert codex_mcp_config_arg() in codex_plan.metadata["provider_tools"]["config_arg"]
    assert sys.executable in codex_plan.metadata["provider_tools"]["config_arg"]
    assert f"args=[{json.dumps(source_tool_path)},{json.dumps('mcp-server')}]" in source_mcp_config
    assert codex_plan.metadata["provider_tools"]["command"] == shlex.join(
        [sys.executable, source_tool_path, "mcp-server"]
    )
    assert codex_plan.metadata["provider_tools"]["mode"] == "codex-mcp"

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        sys,
        "executable",
        "/Applications/Odysseus.app/Contents/Resources/server/odysseus_backend",
    )
    frozen_codex_plan = build_coding_agent_launch_plan(
        harness_id="codex",
        base_command="codex",
        model="gpt-5.1-codex",
        endpoint_url="https://proxy.example.test/v1",
        run_dir=tmp_path,
        default_harness_command=True,
    )
    frozen_mcp_config = frozen_codex_plan.metadata["provider_tools"]["config_arg"]
    assert "--odysseus-tool" in frozen_codex_plan.command
    assert "scripts/odysseus-tool" not in frozen_mcp_config
    assert 'args=["--odysseus-tool","mcp-server"]' in frozen_mcp_config
    assert frozen_codex_plan.metadata["provider_tools"]["command"] == (
        "/Applications/Odysseus.app/Contents/Resources/server/odysseus_backend "
        "--odysseus-tool mcp-server"
    )

    override_plan = build_coding_agent_launch_plan(
        harness_id="codex",
        base_command="codex --version",
        model="gpt-5.1-codex",
        run_dir=tmp_path,
        default_harness_command=False,
    )
    assert override_plan.command == "codex --version"
    assert override_plan.metadata["model_explicit"] is False


def test_backend_launcher_detects_provider_cli_dispatch_args():
    module_path = Path(__file__).resolve().parents[1] / "macos" / "backend_launcher.py"
    spec = importlib.util.spec_from_file_location("odysseus_backend_launcher_test", module_path)
    assert spec is not None
    assert spec.loader is not None
    backend_launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backend_launcher)

    assert backend_launcher._provider_cli_args(["odysseus_backend", "--odysseus-tool", "env"]) == ["env"]
    assert backend_launcher._provider_cli_args(
        [
            "odysseus_backend",
            "/Applications/Odysseus.app/Contents/Resources/server/_internal/scripts/odysseus-tool",
            "mcp-server",
        ]
    ) == ["mcp-server"]
    assert backend_launcher._provider_cli_args(["odysseus_backend"]) is None


@pytest.mark.asyncio
async def test_enqueue_run_persists_model_and_tool_launch_metadata(
    monkeypatch,
    isolated_coding_store,
):
    from src.coding_runtime import CodingRuntimeService

    _project_id, thread_id = _seed_project_and_thread(isolated_coding_store)
    db = isolated_coding_store.SessionLocal()
    try:
        thread = db.query(CodingThread).filter(CodingThread.id == thread_id).first()
        assert thread is not None
        thread.harness_id = "codex"
        thread.model = "gpt-5.1-codex"
        db.commit()
    finally:
        db.close()

    service = CodingRuntimeService()

    async def no_pump(**_kwargs):
        return None

    monkeypatch.setattr(service, "pump_queue", no_pump)
    run = await service.enqueue_run(thread_id=thread_id, owner="tester")

    assert "codex --model gpt-5.1-codex" in run.command
    assert "mcp_servers.odysseus" in run.command

    db = isolated_coding_store.SessionLocal()
    try:
        persisted = db.query(CodingRun).filter(CodingRun.id == run.id).first()
        assert persisted is not None
        metadata = json.loads(persisted.metadata_json)
        assert metadata["agent_launch"]["configured_model"] == "gpt-5.1-codex"
        assert metadata["agent_launch"]["model_explicit"] is True
        assert metadata["agent_launch"]["provider_tools"]["mode"] == "codex-mcp"
        queued_event = (
            db.query(CodingThreadEvent)
            .filter(CodingThreadEvent.run_id == run.id, CodingThreadEvent.kind == "queued")
            .first()
        )
        assert queued_event is not None
        payload = json.loads(queued_event.payload_json)
        assert payload["model"] == "gpt-5.1-codex"
        assert payload["agent_launch"]["provider_tools"]["mode"] == "codex-mcp"
    finally:
        db.close()


def test_provider_bridge_service_issues_run_env_and_revokes_token(monkeypatch):
    import src.coding_provider_bridge as provider_bridge
    from src.coding_provider_bridge import ProviderBridgeScope, get_provider_bridge_service

    monkeypatch.setattr(
        provider_bridge,
        "_mint_backend_run_token",
        lambda scope: {"token": f"ody_cp_{scope.run_id}_test", "token_id": f"token-{scope.run_id}"},
    )
    monkeypatch.setattr(provider_bridge, "_revoke_backend_run_token", lambda record: None)

    monkeypatch.setenv("ODYSSEUS_TOOL_URL", "http://localhost:9999/provider")
    service = get_provider_bridge_service()
    service.revoke_run_token(run_id="run-env-smoke")

    scope = ProviderBridgeScope(
        owner="tester",
        project_id="project-env-smoke",
        thread_id="thread-env-smoke",
        run_id="run-env-smoke",
    )
    env = service.create_run_env(scope)

    assert env["ODYSSEUS_TOOL_URL"] == "http://localhost:9999/provider"
    assert env["ODYSSEUS_THREAD_ID"] == "thread-env-smoke"
    assert env["ODYSSEUS_PROJECT_ID"] == "project-env-smoke"
    assert env["ODYSSEUS_RUN_ID"] == "run-env-smoke"
    assert env["ODYSSEUS_TOOL_TOKEN"].startswith("ody_cp_")

    validated = service.validate_token(env["ODYSSEUS_TOOL_TOKEN"])
    assert validated is not None
    assert validated.run_id == "run-env-smoke"

    assert service.revoke_run_token(run_id="run-env-smoke") is True
    assert service.validate_token(env["ODYSSEUS_TOOL_TOKEN"]) is None


def test_provider_bridge_service_rejects_unusable_backend_token(monkeypatch):
    import src.coding_provider_bridge as provider_bridge
    from src.coding_provider_bridge import ProviderBridgeScope, get_provider_bridge_service

    monkeypatch.setattr(
        provider_bridge,
        "_mint_backend_run_token",
        lambda scope: {"token": f"odyt_{scope.run_id}_test", "token_id": f"token-{scope.run_id}"},
    )
    service = get_provider_bridge_service()
    service.revoke_run_token(run_id="bad-run-token")

    scope = ProviderBridgeScope(
        owner="tester",
        project_id="project-bad-token",
        thread_id="thread-bad-token",
        run_id="bad-run-token",
    )
    with pytest.raises(RuntimeError, match="usable provider HTTP token"):
        service.create_run_env(scope)


def test_launch_environment_strips_private_odysseus_env_and_preserves_provider_allowlist(
    monkeypatch,
    isolated_coding_store,
):
    import src.coding_runtime as coding_runtime
    from src.coding_provider_bridge import scripts_dir
    from src.coding_runtime import CodingRuntimeService

    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("OPENAI_ORG_ID", "org-from-server")
    monkeypatch.setenv("ODYSSEUS_INTERNAL_TOKEN", "server-admin-token")
    monkeypatch.setenv("ODYSSEUS_PRIVATE_FLAG", "server-private")
    monkeypatch.setattr(
        coding_runtime,
        "build_launch_env",
        lambda _db, _owner, _endpoint_id, _model: {
            "OPENAI_API_KEY": "model-token",
            "OPENAI_MODEL": "model-from-thread",
            "ODYSSEUS_INTERNAL_TOKEN": "model-should-not-pass",
        },
    )

    service = CodingRuntimeService()
    monkeypatch.setattr(
        service,
        "_issue_provider_bridge_env",
        lambda _run, _thread: {
            "ODYSSEUS_TOOL_URL": "http://localhost:7000/api/coding/provider/tool",
            "ODYSSEUS_TOOL_TOKEN": "ody_cp_run_token",
            "ODYSSEUS_THREAD_ID": "thread-env-sanitize",
            "ODYSSEUS_PROJECT_ID": "project-env-sanitize",
            "ODYSSEUS_RUN_ID": "run-env-sanitize",
        },
    )

    _project_id, thread_id = _seed_project_and_thread(
        isolated_coding_store,
        thread_id="thread-env-sanitize",
    )
    db = isolated_coding_store.SessionLocal()
    try:
        thread = db.query(CodingThread).filter(CodingThread.id == thread_id).first()
        assert thread is not None
        run = CodingRun(
            id="run-env-sanitize",
            thread_id=thread_id,
            owner="tester",
            harness_id="generic",
            status="starting",
        )
        env, metadata = service._build_launch_environment(db, run, thread)
    finally:
        db.close()

    assert env["OPENAI_ORG_ID"] == "org-from-server"
    assert env["OPENAI_API_KEY"] == "model-token"
    assert env["OPENAI_MODEL"] == "model-from-thread"
    assert env["ODYSSEUS_TOOL_URL"].endswith("/api/coding/provider/tool")
    assert env["ODYSSEUS_TOOL_TOKEN"] == "ody_cp_run_token"
    assert env["ODYSSEUS_THREAD_ID"] == "thread-env-sanitize"
    assert env["ODYSSEUS_PROJECT_ID"] == "project-env-sanitize"
    assert env["ODYSSEUS_RUN_ID"] == "run-env-sanitize"
    assert "ODYSSEUS_INTERNAL_TOKEN" not in env
    assert "ODYSSEUS_PRIVATE_FLAG" not in env
    assert env["PATH"].startswith(f"{scripts_dir()}{os.pathsep}")
    assert metadata["token_issued"] is True
    # The full (already-sanitized) launch env is handed to dtach directly; private
    # ODYSSEUS_* vars are dropped by _build_launch_environment (asserted above), so the
    # agent never sees server-internal tokens.


def test_provider_cli_list_uses_provider_authenticated_tool_action(monkeypatch, capsys):
    import argparse
    import src.coding_provider_cli as provider_cli

    calls = []

    def fake_request(url, payload=None, method="POST"):
        calls.append((url, payload, method))
        return {"ok": True, "result": {"tools": []}}

    monkeypatch.setattr(provider_cli, "_tool_call_url", lambda: "http://localhost/api/coding/provider/tool")
    monkeypatch.setattr(provider_cli, "_request", fake_request)

    provider_cli.cmd_list(argparse.Namespace(pretty=False))

    assert calls == [
        (
            "http://localhost/api/coding/provider/tool",
            {"tool": "provider", "action": "list", "args": {}},
            "POST",
        )
    ]
    assert json.loads(capsys.readouterr().out) == {"ok": True, "result": {"tools": []}}


def test_manage_coding_is_registered_for_agent_function_calls():
    from src.agent_tools import TOOL_TAGS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block

    schema = next(
        item
        for item in FUNCTION_TOOL_SCHEMAS
        if item.get("function", {}).get("name") == "manage_coding"
    )
    actions = schema["function"]["parameters"]["properties"]["action"]["enum"]

    assert "manage_coding" in TOOL_TAGS
    assert {"create_project", "create_thread", "run_thread", "read_run"} <= set(actions)

    block = function_call_to_tool_block("manage_coding", json.dumps({"action": "harnesses"}))
    assert block is not None
    assert block.tool_type == "manage_coding"
    assert json.loads(block.content) == {"action": "harnesses"}


def test_safe_cli_env_omits_secrets_and_sets_ollama_host(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "real-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "real-anthropic-key")

    env = build_safe_cli_env("http://127.0.0.1:11434/v1", "llama3.2")

    assert env == {
        "OPENAI_BASE_URL": "http://127.0.0.1:11434/v1",
        "OPENAI_API_BASE": "http://127.0.0.1:11434/v1",
        "OLLAMA_HOST": "http://127.0.0.1:11434",
        "OPENAI_MODEL": "llama3.2",
    }
    assert "OPENAI_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env


def test_temp_sqlite_store_creates_only_isolated_coding_data(isolated_coding_store):
    table_names = set(inspect(isolated_coding_store.engine).get_table_names())

    assert CODING_TABLES <= table_names
    assert isolated_coding_store.db_path.exists()
    assert str(isolated_coding_store.db_path).startswith(str(isolated_coding_store.data_dir))
    with sqlite3.connect(isolated_coding_store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM coding_projects").fetchone()[0] == 0


def test_pin_and_unpin_persist_through_routes(coding_client, isolated_coding_store):
    client, _runtime = coding_client
    _project_id, thread_id = _seed_project_and_thread(isolated_coding_store)

    response = client.post(f"/api/coding/threads/{thread_id}/pin")
    assert response.status_code == 200
    assert response.json()["thread"]["pinned_at"]

    db = isolated_coding_store.SessionLocal()
    try:
        persisted = db.query(CodingThread).filter(CodingThread.id == thread_id).first()
        assert persisted is not None
        assert persisted.pinned_at is not None
    finally:
        db.close()

    response = client.delete(f"/api/coding/threads/{thread_id}/pin")
    assert response.status_code == 200
    assert response.json()["thread"]["pinned_at"] is None

    db = isolated_coding_store.SessionLocal()
    try:
        persisted = db.query(CodingThread).filter(CodingThread.id == thread_id).first()
        assert persisted is not None
        assert persisted.pinned_at is None
    finally:
        db.close()


def test_coding_settings_persist_max_concurrent_tasks(
    monkeypatch,
    coding_client,
    isolated_coding_store,
):
    import src.settings as settings
    from src.coding_task_slots import get_task_slot_service

    settings_path = isolated_coding_store.data_dir / "settings.json"
    monkeypatch.setattr(settings, "SETTINGS_FILE", str(settings_path))
    settings._invalidate_caches()

    client, _runtime = coding_client

    initial = client.get("/api/coding/settings")
    assert initial.status_code == 200
    assert initial.json()["settings"]["max_concurrent_tasks"] == 2

    # The current UI still sends the legacy alias; it maps to the task limit.
    updated = client.patch("/api/coding/settings", json={"max_concurrent_agents": 1})
    assert updated.status_code == 200
    body = updated.json()["settings"]
    assert body["max_concurrent_tasks"] == 1
    assert body["coding_max_concurrent_tasks"] == 1
    assert body["max_concurrent_agents"] == 1  # legacy alias mirrors the value
    assert json.loads(settings_path.read_text(encoding="utf-8"))["coding_max_concurrent_tasks"] == 1

    # The canonical key is accepted too, and drives the per-endpoint slot limit.
    updated2 = client.patch("/api/coding/settings", json={"max_concurrent_tasks": 3})
    assert updated2.status_code == 200
    assert updated2.json()["settings"]["max_concurrent_tasks"] == 3
    settings._invalidate_caches()
    assert get_task_slot_service().default_limit() == 3
    settings._invalidate_caches()


def test_queue_poll_reconciles_dead_tmux_run_and_starts_next(
    monkeypatch,
    coding_client,
    isolated_coding_store,
):
    import src.coding_runtime as coding_runtime

    client, runtime = coding_client
    monkeypatch.setattr(
        coding_runtime,
        "get_setting",
        lambda key, default=None: 1 if key == "coding_max_concurrent_threads" else default,
    )
    monkeypatch.setattr(coding_runtime.CodingRuntimeService, "dtach_available", lambda _self: True)

    async def missing_session(_self, _name):
        return False

    async def no_launch(_self, _run_id):
        return None

    monkeypatch.setattr(coding_runtime.CodingPtyBridge, "has_session", missing_session)
    monkeypatch.setattr(coding_runtime.CodingRuntimeService, "_launch_run", no_launch)

    _project_id, stale_thread_id = _seed_project_and_thread(isolated_coding_store)
    _project_id, queued_thread_id = _seed_project_and_thread(
        isolated_coding_store,
        title="Queued Thread",
    )
    stale_run_id = f"run-{uuid.uuid4()}"
    queued_run_id = f"run-{uuid.uuid4()}"
    db = isolated_coding_store.SessionLocal()
    try:
        stale_thread = db.query(CodingThread).filter(CodingThread.id == stale_thread_id).first()
        queued_thread = db.query(CodingThread).filter(CodingThread.id == queued_thread_id).first()
        assert stale_thread is not None
        assert queued_thread is not None
        db.add_all(
            [
                CodingRun(
                    id=stale_run_id,
                    thread_id=stale_thread_id,
                    owner="tester",
                    harness_id="generic",
                    status="running",
                    command="echo stale",
                    cwd=str(isolated_coding_store.workspace),
                    tmux_session="missing-session",
                    metadata_json="{}",
                    started_at=datetime.utcnow(),
                ),
                CodingRun(
                    id=queued_run_id,
                    thread_id=queued_thread_id,
                    owner="tester",
                    harness_id="generic",
                    status="queued",
                    command="echo next",
                    cwd=str(isolated_coding_store.workspace),
                    metadata_json="{}",
                ),
            ]
        )
        stale_thread.status = "running"
        stale_thread.last_run_id = stale_run_id
        queued_thread.status = "queued"
        queued_thread.last_run_id = queued_run_id
        db.commit()
    finally:
        db.close()

    response = client.get("/api/coding/queue")

    assert response.status_code == 200
    queue = response.json()["queue"]
    assert queue["max_concurrent"] == 1
    assert queue["queued_count"] == 0
    assert queue["active_count"] == 1
    assert queue["active"] == [queued_run_id]
    assert [run["id"] for run in queue["active_runs"]] == [queued_run_id]

    db = isolated_coding_store.SessionLocal()
    try:
        stale_run = db.query(CodingRun).filter(CodingRun.id == stale_run_id).first()
        queued_run = db.query(CodingRun).filter(CodingRun.id == queued_run_id).first()
        stale_thread = db.query(CodingThread).filter(CodingThread.id == stale_thread_id).first()
        queued_thread = db.query(CodingThread).filter(CodingThread.id == queued_thread_id).first()
        assert stale_run is not None
        assert queued_run is not None
        assert stale_run.status == "failed"
        assert "tmux session missing-session no longer exists" in stale_run.error
        assert stale_thread is not None
        assert stale_thread.status == "idle"
        assert queued_run.status == "starting"
        assert queued_thread is not None
        assert queued_thread.status == "starting"
    finally:
        db.close()


def test_queue_is_owner_global_with_project_attribution(
    coding_client,
    isolated_coding_store,
):
    # Concurrency is one pool shared across all of an owner's projects, so the queue
    # count is owner-global regardless of which project is in view; the per-project
    # breakdown (and per-run project_id) is what makes that usage attributable.
    client, runtime = coding_client
    project_a_id, thread_a_id = _seed_project_and_thread(
        isolated_coding_store,
        title="Project A Thread",
    )
    project_b_id, thread_b_id = _seed_project_and_thread(
        isolated_coding_store,
        title="Project B Thread",
    )
    run_a_id = f"run-{uuid.uuid4()}"
    run_b_id = f"run-{uuid.uuid4()}"
    db = isolated_coding_store.SessionLocal()
    try:
        thread_a = db.query(CodingThread).filter(CodingThread.id == thread_a_id).first()
        thread_b = db.query(CodingThread).filter(CodingThread.id == thread_b_id).first()
        assert thread_a is not None
        assert thread_b is not None
        db.add_all(
            [
                CodingRun(
                    id=run_a_id,
                    thread_id=thread_a_id,
                    owner="tester",
                    harness_id="generic",
                    status="running",
                    command="echo project-a",
                    cwd=str(isolated_coding_store.workspace),
                    metadata_json="{}",
                    started_at=datetime.utcnow(),
                ),
                CodingRun(
                    id=run_b_id,
                    thread_id=thread_b_id,
                    owner="tester",
                    harness_id="generic",
                    status="running",
                    command="echo project-b",
                    cwd=str(isolated_coding_store.workspace),
                    metadata_json="{}",
                    started_at=datetime.utcnow(),
                ),
            ]
        )
        thread_a.status = "running"
        thread_a.last_run_id = run_a_id
        thread_b.status = "running"
        thread_b.last_run_id = run_b_id
        db.commit()
    finally:
        db.close()

    runtime._active_tasks[run_a_id] = object()
    runtime._active_tasks[run_b_id] = object()
    try:
        # Selecting a project does NOT narrow the pool count — both runs still count.
        scoped = client.get(f"/api/coding/queue?project_id={project_a_id}")
        assert scoped.status_code == 200
        scoped_queue = scoped.json()["queue"]
        assert scoped_queue["project_id"] == project_a_id
        assert scoped_queue["active_count"] == 2
        assert set(scoped_queue["active"]) == {run_a_id, run_b_id}
        scoped_by_project = {b["project_id"]: b for b in scoped_queue["by_project"]}
        assert scoped_by_project[project_a_id]["active"] == 1
        assert scoped_by_project[project_b_id]["active"] == 1

        unscoped = client.get("/api/coding/queue")
        assert unscoped.status_code == 200
        unscoped_queue = unscoped.json()["queue"]
        assert unscoped_queue["project_id"] is None
        assert unscoped_queue["active_count"] == 2
        assert set(unscoped_queue["active"]) == {run_a_id, run_b_id}
        assert {run["id"] for run in unscoped_queue["active_runs"]} == {run_a_id, run_b_id}
        # Each run carries its project so the UI can show where threads are consumed.
        run_projects = {run["id"]: run["project_id"] for run in unscoped_queue["active_runs"]}
        assert run_projects == {run_a_id: project_a_id, run_b_id: project_b_id}
        by_project = {b["project_id"]: b for b in unscoped_queue["by_project"]}
        assert by_project[project_a_id]["active"] == 1
        assert by_project[project_b_id]["active"] == 1
    finally:
        runtime._active_tasks.pop(run_a_id, None)
        runtime._active_tasks.pop(run_b_id, None)


@pytest.mark.asyncio
async def test_terminals_are_not_capped_runs_launch_immediately(monkeypatch, isolated_coding_store):
    # Concurrency now lives at the LLM-call (task-slot) layer, not the terminal
    # layer: pump_queue launches every queued run immediately, even with the
    # legacy cap pinned to 1.
    import src.coding_runtime as coding_runtime
    from src.coding_runtime import CodingRuntimeService

    monkeypatch.setattr(
        coding_runtime,
        "get_setting",
        lambda key, default=None: 1 if key in ("coding_max_concurrent_threads", "coding_max_concurrent_tasks") else default,
    )
    service = CodingRuntimeService()

    async def no_launch(_run_id):
        return None

    monkeypatch.setattr(service, "_launch_run", no_launch)
    project_a_id, thread_a_id = _seed_project_and_thread(
        isolated_coding_store,
        title="Project A Thread",
    )
    project_b_id, thread_b_id = _seed_project_and_thread(
        isolated_coding_store,
        title="Project B Thread",
    )
    run_a_id = f"run-{uuid.uuid4()}"
    run_b_id = f"run-{uuid.uuid4()}"
    db = isolated_coding_store.SessionLocal()
    try:
        thread_a = db.query(CodingThread).filter(CodingThread.id == thread_a_id).first()
        thread_b = db.query(CodingThread).filter(CodingThread.id == thread_b_id).first()
        assert thread_a is not None
        assert thread_b is not None
        db.add_all(
            [
                CodingRun(
                    id=run_a_id,
                    thread_id=thread_a_id,
                    owner="tester",
                    harness_id="generic",
                    status="running",
                    command="echo project-a",
                    cwd=str(isolated_coding_store.workspace),
                    metadata_json="{}",
                    started_at=datetime.utcnow(),
                ),
                CodingRun(
                    id=run_b_id,
                    thread_id=thread_b_id,
                    owner="tester",
                    harness_id="generic",
                    status="queued",
                    command="echo project-b",
                    cwd=str(isolated_coding_store.workspace),
                    metadata_json="{}",
                ),
            ]
        )
        thread_a.status = "running"
        thread_a.last_run_id = run_a_id
        thread_b.status = "queued"
        thread_b.last_run_id = run_b_id
        db.commit()
    finally:
        db.close()

    service._active_tasks[run_a_id] = object()
    try:
        await service.pump_queue(owner="tester")

        statuses = _run_statuses(isolated_coding_store, run_a_id, run_b_id)
        # No terminal cap: the queued run in project B starts immediately even though
        # run_a is already running (and the legacy cap is 1).
        assert statuses == {run_a_id: "running", run_b_id: "starting"}
        snapshot = service.queue_snapshot("tester")
        assert snapshot["active_count"] == 2
        assert snapshot["queued_count"] == 0
        assert set(snapshot["active"]) == {run_a_id, run_b_id}
    finally:
        service._active_tasks.pop(run_a_id, None)


@pytest.mark.asyncio
async def test_stale_run_reconcile_keeps_actively_tracked_run(isolated_coding_store):
    from src.coding_runtime import CodingRuntimeService

    service = CodingRuntimeService()
    _project_id, thread_id = _seed_project_and_thread(isolated_coding_store)
    run_id = f"run-{uuid.uuid4()}"
    db = isolated_coding_store.SessionLocal()
    try:
        thread = db.query(CodingThread).filter(CodingThread.id == thread_id).first()
        assert thread is not None
        db.add(
            CodingRun(
                id=run_id,
                thread_id=thread_id,
                owner="tester",
                harness_id="generic",
                status="running",
                command="echo active",
                cwd=str(isolated_coding_store.workspace),
                tmux_session="missing-session",
                metadata_json="{}",
                started_at=datetime.utcnow(),
            )
        )
        thread.status = "running"
        thread.last_run_id = run_id
        db.commit()
    finally:
        db.close()

    service._active_tasks[run_id] = object()
    try:
        assert await service.reconcile_stale_active_runs("tester") == []
    finally:
        service._active_tasks.pop(run_id, None)

    db = isolated_coding_store.SessionLocal()
    try:
        run = db.query(CodingRun).filter(CodingRun.id == run_id).first()
        assert run is not None
        assert run.status == "running"
        assert run.finished_at is None
    finally:
        db.close()


def test_delete_thread_cancels_queued_runs_and_deletes_thread(coding_client, isolated_coding_store):
    client, _runtime = coding_client
    _project_id, thread_id = _seed_project_and_thread(isolated_coding_store)
    run_id = f"run-{uuid.uuid4()}"
    db = isolated_coding_store.SessionLocal()
    try:
        thread = db.query(CodingThread).filter(CodingThread.id == thread_id).first()
        assert thread is not None
        db.add(
            CodingRun(
                id=run_id,
                thread_id=thread_id,
                owner="tester",
                harness_id="generic",
                status="queued",
                command="echo queued",
                cwd=str(isolated_coding_store.workspace),
                metadata_json="{}",
            )
        )
        thread.status = "queued"
        thread.last_run_id = run_id
        db.commit()
    finally:
        db.close()

    response = client.delete(f"/api/coding/threads/{thread_id}")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "deleted": thread_id}
    db = isolated_coding_store.SessionLocal()
    try:
        assert db.query(CodingThread).filter(CodingThread.id == thread_id).first() is None
        assert db.query(CodingRun).filter(CodingRun.id == run_id).first() is None
    finally:
        db.close()


def test_delete_thread_propagates_cleanup_failure_without_deleting(
    monkeypatch,
    coding_client,
    isolated_coding_store,
):
    from src.coding_runtime import CodingRuntimeError

    client, runtime = coding_client
    _project_id, thread_id = _seed_project_and_thread(isolated_coding_store)
    run_id = f"run-{uuid.uuid4()}"
    db = isolated_coding_store.SessionLocal()
    try:
        thread = db.query(CodingThread).filter(CodingThread.id == thread_id).first()
        assert thread is not None
        db.add(
            CodingRun(
                id=run_id,
                thread_id=thread_id,
                owner="tester",
                harness_id="generic",
                status="running",
                command="echo running",
                cwd=str(isolated_coding_store.workspace),
                tmux_session="missing-session",
                metadata_json="{}",
            )
        )
        thread.status = "running"
        thread.last_run_id = run_id
        db.commit()
    finally:
        db.close()

    async def fail_cleanup(_run, *, strict=False, include_unpersisted_tmux=False):
        assert strict is True
        assert include_unpersisted_tmux is True
        raise CodingRuntimeError(500, "cleanup failed")

    monkeypatch.setattr(runtime, "_signal_stop", fail_cleanup)

    response = client.delete(f"/api/coding/threads/{thread_id}")

    assert response.status_code == 500
    assert response.json()["detail"] == "cleanup failed"
    db = isolated_coding_store.SessionLocal()
    try:
        thread = db.query(CodingThread).filter(CodingThread.id == thread_id).first()
        run = db.query(CodingRun).filter(CodingRun.id == run_id).first()
        assert thread is not None
        assert run is not None
        assert run.status == "running"
    finally:
        db.close()


def test_delete_thread_returns_409_while_run_launch_is_tracked(coding_client, isolated_coding_store):
    client, runtime = coding_client
    _project_id, thread_id = _seed_project_and_thread(isolated_coding_store)
    runtime._launching_runs["run-launching"] = ("tester", thread_id)
    try:
        response = client.delete(f"/api/coding/threads/{thread_id}")
    finally:
        runtime._launching_runs.pop("run-launching", None)

    assert response.status_code == 409
    assert response.json()["detail"] == "Thread has a run launching; retry deletion"
    db = isolated_coding_store.SessionLocal()
    try:
        assert db.query(CodingThread).filter(CodingThread.id == thread_id).first() is not None
    finally:
        db.close()


def test_delete_thread_stops_tracked_subprocess_before_deleting(coding_client, isolated_coding_store):
    client, runtime = coding_client
    _project_id, thread_id = _seed_project_and_thread(isolated_coding_store)
    run_id = f"run-{uuid.uuid4()}"
    db = isolated_coding_store.SessionLocal()
    try:
        thread = db.query(CodingThread).filter(CodingThread.id == thread_id).first()
        assert thread is not None
        db.add(
            CodingRun(
                id=run_id,
                thread_id=thread_id,
                owner="tester",
                harness_id="generic",
                status="running",
                command="sleep 10",
                cwd=str(isolated_coding_store.workspace),
                metadata_json="{}",
            )
        )
        thread.status = "running"
        thread.last_run_id = run_id
        db.commit()
    finally:
        db.close()

    class FakeProcess:
        def __init__(self):
            self.returncode = None
            self.terminated = False
            self.killed = False

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            return self.returncode

    proc = FakeProcess()
    runtime._active_processes[run_id] = proc

    response = client.delete(f"/api/coding/threads/{thread_id}")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "deleted": thread_id}
    assert proc.terminated is True
    assert proc.killed is False
    assert run_id not in runtime._active_processes
    db = isolated_coding_store.SessionLocal()
    try:
        assert db.query(CodingThread).filter(CodingThread.id == thread_id).first() is None
        assert db.query(CodingRun).filter(CodingRun.id == run_id).first() is None
    finally:
        db.close()


def test_run_pty_websocket_rejects_non_admin_before_attach(monkeypatch, coding_client):
    from routes.auth_routes import SESSION_COOKIE
    from starlette.websockets import WebSocketDisconnect

    client, runtime = coding_client

    class NonAdminAuth:
        is_configured = True

        def validate_token(self, token):
            return token == "viewer-token"

        def get_username_for_token(self, token):
            return "viewer" if token == "viewer-token" else ""

        def is_admin(self, username):
            return False

    async def fail_attach(*_args, **_kwargs):
        raise AssertionError("PTY attach should not run for a non-admin websocket")

    monkeypatch.setenv("AUTH_ENABLED", "true")
    client.app.state.auth_manager = NonAdminAuth()
    monkeypatch.setattr(runtime, "attach_pty", fail_attach)
    client.cookies.set(SESSION_COOKIE, "viewer-token")

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/api/coding/runs/run-websocket-denied/pty"):
            pass

    assert exc.value.code == 1008


@pytest.mark.asyncio
async def test_enqueue_run_rejects_threads_with_deletion_in_progress(isolated_coding_store):
    from src.coding_runtime import CodingRuntimeError, CodingRuntimeService

    service = CodingRuntimeService()
    _project_id, thread_id = _seed_project_and_thread(isolated_coding_store)
    service._deleting_threads.add(("tester", thread_id))
    try:
        with pytest.raises(CodingRuntimeError) as exc:
            await service.enqueue_run(
                thread_id=thread_id,
                owner="tester",
                command="echo blocked",
                cwd=str(isolated_coding_store.workspace),
            )
    finally:
        service._deleting_threads.discard(("tester", thread_id))

    assert exc.value.status_code == 409
    assert exc.value.detail == "Thread deletion is in progress"


@pytest.mark.asyncio
async def test_event_persistence_replays_from_db_and_mirrors_run_dir(isolated_coding_store):
    from src.coding_runtime import CodingRuntimeService

    _project_id, thread_id = _seed_project_and_thread(isolated_coding_store)
    run_id = f"run-{uuid.uuid4()}"
    run_dir = isolated_coding_store.data_dir / "manual-run"
    db = isolated_coding_store.SessionLocal()
    try:
        db.add(
            CodingRun(
                id=run_id,
                thread_id=thread_id,
                owner="tester",
                harness_id="generic",
                status="running",
                command="echo hello",
                cwd=str(isolated_coding_store.workspace),
                run_dir=str(run_dir),
                log_path=str(run_dir / "raw.log"),
                metadata_json="{}",
            )
        )
        db.commit()
    finally:
        db.close()

    service = CodingRuntimeService()
    await service.append_event(thread_id, run_id, "output", {"stream": "stdout", "data": "hello\n"})

    replayed = CodingRuntimeService().get_events(thread_id, "tester", after_seq=0)
    assert [(event.seq, event.kind, json.loads(event.payload_json)) for event in replayed] == [
        (1, "output", {"stream": "stdout", "data": "hello\n"})
    ]

    event_lines = (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(event_lines) == 1
    mirrored = json.loads(event_lines[0])
    assert mirrored["thread_id"] == thread_id
    assert mirrored["run_id"] == run_id
    assert mirrored["kind"] == "output"
    assert mirrored["payload"]["data"] == "hello\n"


@pytest.mark.asyncio
async def test_terminals_launch_concurrently_without_a_cap(monkeypatch, isolated_coding_store):
    # With the terminal cap removed, a second run launches immediately rather than
    # queueing behind the first — even with the legacy cap pinned to 1.
    import src.coding_runtime as coding_runtime
    from src.coding_runtime import CodingRuntimeService

    monkeypatch.setattr(
        coding_runtime,
        "get_setting",
        lambda key, default=None: 1 if key == "coding_max_concurrent_threads" else default,
    )
    service = CodingRuntimeService()
    monkeypatch.setattr(service, "dtach_available", lambda: False)

    project_id, first_thread_id = _seed_project_and_thread(
        isolated_coding_store,
        title="First thread",
    )
    _project_id, second_thread_id = _seed_project_and_thread(
        isolated_coding_store,
        project_id=project_id,
        title="Second thread",
    )

    slow_script = "import time; print('first', flush=True); time.sleep(0.6)"
    fast_script = "print('second', flush=True)"
    slow_command = f"{shlex.quote(sys.executable)} -c {shlex.quote(slow_script)}"
    fast_command = f"{shlex.quote(sys.executable)} -c {shlex.quote(fast_script)}"

    first_run = await service.enqueue_run(
        thread_id=first_thread_id,
        owner="tester",
        command=slow_command,
        cwd=str(isolated_coding_store.workspace),
    )
    await _wait_for(
        lambda: _run_statuses(isolated_coding_store, first_run.id).get(first_run.id)
        in {"starting", "running"}
    )

    second_run = await service.enqueue_run(
        thread_id=second_thread_id,
        owner="tester",
        command=fast_command,
        cwd=str(isolated_coding_store.workspace),
    )
    # Launches right away (not held in "queued") while the first run is still going.
    await _wait_for(
        lambda: _run_statuses(isolated_coding_store, second_run.id).get(second_run.id)
        in {"starting", "running", "exited"}
    )
    assert _run_statuses(isolated_coding_store, second_run.id)[second_run.id] != "queued"

    await _wait_for(
        lambda: _run_statuses(isolated_coding_store, first_run.id, second_run.id)
        == {first_run.id: "exited", second_run.id: "exited"},
        timeout=8.0,
    )

    snapshot = service.queue_snapshot("tester")
    assert snapshot["queued_count"] == 0
    assert snapshot["active_count"] == 0

    first_reloaded = _load_run(isolated_coding_store, first_run.id)
    second_reloaded = _load_run(isolated_coding_store, second_run.id)
    assert first_reloaded.exit_code == 0
    assert second_reloaded.exit_code == 0
    # Concurrency, not queueing: the second run started before the first finished.
    assert second_reloaded.started_at < first_reloaded.finished_at

    first_events = [event.kind for event in service.get_events(first_thread_id, "tester")]
    second_events = [event.kind for event in service.get_events(second_thread_id, "tester")]
    assert first_events[:3] == ["queued", "starting", "started"]
    assert first_events[-1] == "exited"
    assert second_events[:3] == ["queued", "starting", "started"]
    assert second_events[-1] == "exited"


@pytest.mark.asyncio
async def test_manage_coding_creates_threads_and_enforces_owner_scope(isolated_coding_store):
    from src.tool_implementations import do_manage_coding

    created_project = await do_manage_coding(
        json.dumps(
            {
                "action": "create_project",
                "name": "Managed Project",
                "root_path": str(isolated_coding_store.workspace),
                "default_harness": "codex",
            }
        ),
        owner="tester",
    )

    assert created_project["exit_code"] == 0
    assert created_project["project"]["name"] == "Managed Project"
    assert created_project["project"]["default_harness"] == "codex"

    project_id = created_project["project"]["id"]
    other_owner = await do_manage_coding(
        json.dumps({"action": "get_project", "project_id": project_id}),
        owner="other",
    )
    assert other_owner == {"error": "Project not found", "exit_code": 1, "status_code": 404}

    created_thread = await do_manage_coding(
        json.dumps(
            {
                "action": "create_thread",
                "project_id": project_id,
                "title": "Managed Thread",
                "harness_id": "claude",
                "pinned": True,
                "metadata": {"purpose": "coverage"},
            }
        ),
        owner="tester",
    )
    assert created_thread["exit_code"] == 0
    assert created_thread["thread"]["title"] == "Managed Thread"
    assert created_thread["thread"]["harness_id"] == "claude"
    assert created_thread["thread"]["pinned_at"]
    assert created_thread["thread"]["metadata"] == {"purpose": "coverage"}

    listed_threads = await do_manage_coding(
        json.dumps({"action": "list_threads", "project_id": project_id}),
        owner="tester",
    )
    assert [thread["id"] for thread in listed_threads["threads"]] == [
        created_thread["thread"]["id"]
    ]


@pytest.mark.asyncio
async def test_manage_coding_runs_thread_and_reads_events(monkeypatch, isolated_coding_store):
    import src.coding_runtime as coding_runtime
    from src.coding_runtime import CodingRuntimeService
    from src.tool_implementations import do_manage_coding

    runtime = CodingRuntimeService()
    monkeypatch.setattr(runtime, "dtach_available", lambda: False)
    monkeypatch.setattr(coding_runtime, "get_coding_runtime_service", lambda: runtime)

    project = await do_manage_coding(
        json.dumps(
            {
                "action": "create_project",
                "name": "Runnable Project",
                "root_path": str(isolated_coding_store.workspace),
            }
        ),
        owner="tester",
    )
    thread = await do_manage_coding(
        json.dumps(
            {
                "action": "create_thread",
                "project_id": project["project"]["id"],
                "title": "Runnable Thread",
            }
        ),
        owner="tester",
    )

    script = "print('managed coding run', flush=True)"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
    queued = await do_manage_coding(
        json.dumps(
            {
                "action": "run_thread",
                "thread_id": thread["thread"]["id"],
                "command": command,
            }
        ),
        owner="tester",
    )
    assert queued["exit_code"] == 0
    run_id = queued["run"]["id"]

    await _wait_for(
        lambda: _run_statuses(isolated_coding_store, run_id).get(run_id) == "exited",
        timeout=5.0,
    )

    read_run = await do_manage_coding(
        json.dumps(
            {
                "action": "read_run",
                "run_id": run_id,
                "include_events": True,
                "log_chars": 200,
            }
        ),
        owner="tester",
    )
    assert read_run["exit_code"] == 0
    assert read_run["run"]["status"] == "exited"
    assert read_run["run"]["exit_code"] == 0
    assert "managed coding run" in read_run["log_tail"]

    event_kinds = [event["kind"] for event in read_run["events"]]
    assert event_kinds[:3] == ["queued", "starting", "started"]
    assert "output" in event_kinds
    assert event_kinds[-1] == "exited"

    queue = await do_manage_coding(json.dumps({"action": "queue"}), owner="tester")
    assert queue["queue"]["queued_count"] == 0
    assert queue["queue"]["active_count"] == 0


def test_queue_response_includes_task_slot_snapshot(coding_client, isolated_coding_store):
    from src.coding_task_slots import get_task_slot_service

    svc = get_task_slot_service()  # process singleton — isolate from other tests
    svc._holders.clear()
    svc._waiters.clear()
    svc._limits.clear()

    client, _runtime = coding_client
    response = client.get("/api/coding/queue")
    assert response.status_code == 200
    task_slots = response.json()["queue"]["task_slots"]
    # Shape the UI relies on for the badge/popover (idle owner → empty, zeroed).
    assert task_slots["active_total"] == 0
    assert task_slots["waiting_total"] == 0
    assert task_slots["endpoints"] == []
    assert "default_limit" in task_slots
