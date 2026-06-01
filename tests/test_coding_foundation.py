from __future__ import annotations

import asyncio
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
    CodingRun,
    CodingThread,
    CodingThreadEvent,
    ModelEndpoint,
    Session,
)
from src.coding_harnesses import build_harness_command, list_harnesses  # noqa: E402
from src.coding_model_config import build_safe_cli_env  # noqa: E402


CODING_TABLES = {
    "coding_projects",
    "coding_threads",
    "coding_runs",
    "coding_thread_events",
    "coding_model_config_snapshots",
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
        ],
    )

    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(coding_routes, "SessionLocal", TestingSessionLocal)
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
    thread_id: str | None = None,
    title: str = "Coding Thread",
    pinned: bool = False,
) -> tuple[str, str]:
    project_id = f"project-{uuid.uuid4()}"
    thread_id = thread_id or f"thread-{uuid.uuid4()}"
    db = store.SessionLocal()
    try:
        project = CodingProject(
            id=project_id,
            owner=owner,
            name="Test Project",
            root_path=str(store.workspace),
            default_harness="generic",
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
        db.add_all([project, thread])
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

    with pytest.raises(ValueError, match="Unknown coding harness"):
        build_harness_command("missing", "", {})
    with pytest.raises(ValueError, match="requires a command"):
        build_harness_command("custom", "", {})


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
async def test_runtime_max_concurrency_one_drains_queued_runs(monkeypatch, isolated_coding_store):
    import src.coding_runtime as coding_runtime
    from src.coding_runtime import CodingRuntimeService

    monkeypatch.setattr(
        coding_runtime,
        "get_setting",
        lambda key, default=None: 1 if key == "coding_max_concurrent_threads" else default,
    )
    service = CodingRuntimeService()
    monkeypatch.setattr(service, "tmux_available", lambda: False)

    _project_id, first_thread_id = _seed_project_and_thread(
        isolated_coding_store,
        title="First thread",
    )
    _project_id, second_thread_id = _seed_project_and_thread(
        isolated_coding_store,
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
    assert _run_statuses(isolated_coding_store, second_run.id)[second_run.id] == "queued"
    assert service.queue_snapshot("tester")["max_concurrent"] == 1

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
    assert first_reloaded.finished_at <= second_reloaded.started_at

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
    monkeypatch.setattr(runtime, "tmux_available", lambda: False)
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
