"""Tests for the chat <-> Code Station integration (P0/P1/P2).

Covers:
  - P0.0 awareness: manage_coding is registered in the tool-RAG index,
    keyword hints, and the prompt-section registry.
  - P0.1 session linkage: chat-spawned threads auto-link to the session.
  - P1   wake-on-done: coding_followup pending/mark cycle + deferred contract.
  - P2   thread<->issue link + beads/memory action dispatch guard.

DB-backed tests use an isolated SQLite store with the SQLAlchemy models'
own schema (create_all), so new columns (issue_id) are present without a
migration. do_manage_coding / coding_followup import SessionLocal from
core.database at call time, so monkeypatching the attribute is sufficient.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# A DATA_DIR must exist before core.database import for installs that read it.
_TMP = Path(tempfile.mkdtemp(prefix="ody-coding-chat-"))
os.environ.setdefault("DATA_DIR", str(_TMP / "data"))
(_TMP / "data").mkdir(parents=True, exist_ok=True)

import core.database as database  # noqa: E402
from core.database import Base, CodingProject, CodingRun, CodingThread, Session  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def store(monkeypatch, tmp_path):
    """Isolated SQLite store with SessionLocal monkeypatched onto the modules
    that resolve it at call time (core.database + coding_runtime)."""
    import src.coding_runtime as coding_runtime

    db_path = tmp_path / "coding.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    monkeypatch.setattr(database, "SessionLocal", TestSession)
    monkeypatch.setattr(coding_runtime, "SessionLocal", TestSession)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    try:
        yield TestSession, workspace
    finally:
        engine.dispose()


def _seed_session(SessionLocal, sid, owner="tester") -> str:
    """Coding threads FK session_id -> sessions.id, and the engine runs with
    PRAGMA foreign_keys=ON, so a linked thread needs a real session row."""
    db = SessionLocal()
    try:
        if not db.query(Session).filter(Session.id == sid).first():
            db.add(
                Session(
                    id=sid,
                    name=f"chat-{sid}",
                    endpoint_url="http://test",
                    model="test-model",
                    owner=owner,
                )
            )
            db.commit()
    finally:
        db.close()
    return sid


def _seed_project(SessionLocal, workspace, owner="tester") -> str:
    pid = f"project-{uuid.uuid4()}"
    db = SessionLocal()
    try:
        db.add(
            CodingProject(
                id=pid,
                owner=owner,
                name="Test Project",
                root_path=str(workspace),
                default_harness="generic",
            )
        )
        db.commit()
    finally:
        db.close()
    return pid


def _seed_finished_run(
    SessionLocal,
    workspace,
    *,
    session_id,
    status="exited",
    finished=True,
    reported=False,
    owner="tester",
) -> str:
    if session_id:
        _seed_session(SessionLocal, session_id, owner=owner)
    pid = _seed_project(SessionLocal, workspace, owner=owner)
    tid = f"thread-{uuid.uuid4()}"
    rid = f"run-{uuid.uuid4()}"
    # Wake-on-done keys off the per-run report_to_session marker (set by chat
    # run_thread from the trusted caller), NOT thread.session_id.
    meta = {}
    if session_id:
        meta["report_to_session"] = session_id
    if reported:
        meta["reported_to_chat"] = True
    db = SessionLocal()
    try:
        db.add(
            CodingThread(
                id=tid,
                project_id=pid,
                owner=owner,
                session_id=session_id,
                title="Linked Thread",
                cwd=str(workspace),
                harness_id="generic",
                status="idle",
                metadata_json="{}",
            )
        )
        db.add(
            CodingRun(
                id=rid,
                thread_id=tid,
                owner=owner,
                harness_id="generic",
                status=status,
                command="echo hi",
                cwd=str(workspace),
                finished_at=datetime.utcnow() if finished else None,
                metadata_json=json.dumps(meta),
            )
        )
        db.commit()
    finally:
        db.close()
    return rid


# ---------------------------------------------------------------------------
# P0.0 — awareness
# ---------------------------------------------------------------------------


def test_manage_coding_is_indexed_for_retrieval():
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS

    assert "manage_coding" in BUILTIN_TOOL_DESCRIPTIONS
    desc = BUILTIN_TOOL_DESCRIPTIONS["manage_coding"].lower()
    assert "code station" in desc


def test_manage_coding_has_a_prompt_section():
    from src.agent_loop import TOOL_SECTIONS

    assert "manage_coding" in TOOL_SECTIONS


@pytest.mark.parametrize(
    "query",
    [
        "have an agent fix the bug in metalheart",
        "start a coding agent on the odysseus project",
        "how's that coding run going?",
        "stop the coding run",
    ],
)
def test_coding_queries_force_include_manage_coding(query):
    """Keyword-hint path only — no embeddings needed (retrieval stubbed)."""
    from src.tool_index import ToolIndex

    ti = ToolIndex.__new__(ToolIndex)
    ti.retrieve = lambda q, k=8: []
    tools = ti.get_tools_for_query(query)
    assert "manage_coding" in tools, f"{query!r} did not surface manage_coding"


# ---------------------------------------------------------------------------
# P0.1 — session auto-link
# ---------------------------------------------------------------------------


def test_create_thread_autolinks_session(store):
    SessionLocal, workspace = store
    _seed_session(SessionLocal, "sess-123")
    pid = _seed_project(SessionLocal, workspace)
    from src.tool_implementations import do_manage_coding

    res = asyncio.run(
        do_manage_coding(
            json.dumps({"action": "create_thread", "project_id": pid, "title": "T"}),
            owner="tester",
            session_id="sess-123",
        )
    )
    assert "error" not in res, res
    tid = res["thread"]["id"]
    assert res["thread"]["session_id"] == "sess-123"

    db = SessionLocal()
    try:
        t = db.query(CodingThread).filter(CodingThread.id == tid).first()
        assert t.session_id == "sess-123"
    finally:
        db.close()


def test_agent_supplied_session_id_is_ignored_for_security(store):
    """Confused-deputy guard: an agent-supplied session_id must NOT override the
    trusted invoking session (it drives headless re-invocation + chat injection)."""
    SessionLocal, workspace = store
    _seed_session(SessionLocal, "caller")
    pid = _seed_project(SessionLocal, workspace)
    from src.tool_implementations import do_manage_coding

    res = asyncio.run(
        do_manage_coding(
            json.dumps(
                {
                    "action": "create_thread",
                    "project_id": pid,
                    "title": "T",
                    "session_id": "victim-session",  # agent tries to redirect
                }
            ),
            owner="tester",
            session_id="caller",  # trusted invoking session
        )
    )
    assert res["thread"]["session_id"] == "caller"


# ---------------------------------------------------------------------------
# P1 — deferred run_thread contract
# ---------------------------------------------------------------------------


def _stub_runtime(monkeypatch):
    import src.coding_runtime as coding_runtime

    captured = {}

    class _StubRun(SimpleNamespace):
        pass

    def _mk_run(thread_id):
        return _StubRun(
            id="run-x",
            thread_id=thread_id,
            owner="tester",
            harness_id="generic",
            status="queued",
            command="echo hi",
            cwd="/tmp",
            tmux_session=None,
            run_dir=None,
            log_path=None,
            exit_code=None,
            error=None,
            queued_at=datetime.utcnow(),
            started_at=None,
            finished_at=None,
            idempotency_key=None,
            metadata_json=None,
        )

    class _StubRuntime:
        async def enqueue_run(self, **kwargs):
            captured.update(kwargs)
            return _mk_run(kwargs.get("thread_id"))

    monkeypatch.setattr(coding_runtime, "get_coding_runtime_service", lambda: _StubRuntime())
    return captured


def test_run_thread_linked_returns_deferred_contract(monkeypatch):
    captured = _stub_runtime(monkeypatch)
    from src.tool_implementations import do_manage_coding

    res = asyncio.run(
        do_manage_coding(
            json.dumps({"action": "run_thread", "thread_id": "thread-1"}),
            owner="tester",
            session_id="sess-9",
        )
    )
    assert "error" not in res, res
    assert captured.get("session_id") == "sess-9"
    # Honest deferred contract: auto-notify on finish, read_run for interim.
    resp = res["response"].lower()
    assert "auto-notified" in resp and "read_run" in resp


def test_run_thread_unlinked_plain_message(monkeypatch):
    _stub_runtime(monkeypatch)
    from src.tool_implementations import do_manage_coding

    res = asyncio.run(
        do_manage_coding(
            json.dumps({"action": "run_thread", "thread_id": "thread-1"}),
            owner="tester",
        )
    )
    assert "error" not in res, res
    assert "do not poll" not in res["response"].lower()


# ---------------------------------------------------------------------------
# P1 — wake-on-done follow-up data layer
# ---------------------------------------------------------------------------


def test_pending_followup_only_finished_linked_unreported(store):
    SessionLocal, workspace = store
    import src.coding_followup as cf

    target = _seed_finished_run(SessionLocal, workspace, session_id="sess-A")
    _seed_finished_run(SessionLocal, workspace, session_id="sess-B", reported=True)
    _seed_finished_run(SessionLocal, workspace, session_id=None)  # unlinked
    _seed_finished_run(SessionLocal, workspace, session_id="sess-C", status="running", finished=False)

    pending = cf.pending_coding_followups()
    ids = {p["run_id"] for p in pending}
    assert target in ids
    assert len(ids) == 1, f"expected only the finished+linked+unreported run, got {pending}"
    rec = next(p for p in pending if p["run_id"] == target)
    assert rec["session_id"] == "sess-A"
    assert rec["status"] == "exited"


def test_ui_run_without_report_marker_is_not_surfaced(store):
    """A finished run whose thread is session-linked but which was NOT started by
    the chat tool (no report_to_session marker) must never inject into chat."""
    SessionLocal, workspace = store
    import src.coding_followup as cf

    _seed_session(SessionLocal, "sess-UI")
    pid = _seed_project(SessionLocal, workspace)
    tid = f"thread-{uuid.uuid4()}"
    rid = f"run-{uuid.uuid4()}"
    db = SessionLocal()
    try:
        db.add(CodingThread(id=tid, project_id=pid, owner="tester", session_id="sess-UI",
                            title="UI thread", cwd=str(workspace), harness_id="generic",
                            status="idle", metadata_json="{}"))
        # No report_to_session in metadata → UI-initiated run.
        db.add(CodingRun(id=rid, thread_id=tid, owner="tester", harness_id="generic",
                        status="exited", command="x", cwd=str(workspace),
                        finished_at=datetime.utcnow(), metadata_json="{}"))
        db.commit()
    finally:
        db.close()
    assert not any(p["run_id"] == rid for p in cf.pending_coding_followups())


def test_mark_followed_up_is_idempotent(store):
    SessionLocal, workspace = store
    import src.coding_followup as cf

    rid = _seed_finished_run(SessionLocal, workspace, session_id="sess-A")
    assert any(p["run_id"] == rid for p in cf.pending_coding_followups())
    cf.mark_coding_followed_up(rid)
    assert not any(p["run_id"] == rid for p in cf.pending_coding_followups())


def test_build_followup_inject_mentions_run(store):
    import src.coding_followup as cf

    text = cf.build_followup_inject(
        {"run_id": "run-7", "thread_title": "auth", "status": "exited", "exit_code": 0}
    )
    assert "run-7" in text
    assert "auth" in text


# ---------------------------------------------------------------------------
# P2 — thread<->issue link + beads/memory dispatch guard
# ---------------------------------------------------------------------------


def test_create_thread_sets_issue_id_and_reads_back(store):
    SessionLocal, workspace = store
    pid = _seed_project(SessionLocal, workspace)
    from src.tool_implementations import do_manage_coding

    res = asyncio.run(
        do_manage_coding(
            json.dumps(
                {
                    "action": "create_thread",
                    "project_id": pid,
                    "title": "T",
                    "issue_id": "bd-42",
                }
            ),
            owner="tester",
        )
    )
    assert "error" not in res, res
    tid = res["thread"]["id"]
    assert res["thread"]["issue_id"] == "bd-42"

    read = asyncio.run(
        do_manage_coding(
            json.dumps({"action": "read_thread", "thread_id": tid}),
            owner="tester",
        )
    )
    assert read["thread"]["issue_id"] == "bd-42"


def test_beads_action_requires_project_id(store):
    from src.tool_implementations import do_manage_coding

    res = asyncio.run(
        do_manage_coding(json.dumps({"action": "beads", "sub_action": "list"}), owner="tester")
    )
    assert "error" in res
    assert "project_id" in res["error"].lower()
