"""Tests for the Beads (`bd`) integration: service, provider tool, and routes.

The service tests drive the real ``bd`` CLI against a throwaway git repo, so they
skip cleanly when ``bd`` isn't installed. The provider-tool and route tests use an
isolated SQLite DB whose project points at that repo.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest

_MODULE_DATA_DIR = Path(tempfile.mkdtemp(prefix="odysseus-beads-tests-")) / "data"
_MODULE_DATA_DIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("DATA_DIR", str(_MODULE_DATA_DIR))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_MODULE_DATA_DIR / 'app.sqlite'}")

from src.coding_beads import BeadsError, BeadsService, bd_bin, get_beads_service  # noqa: E402

bd_missing = pytest.mark.skipif(bd_bin() is None, reason="bd (Beads) not installed")


@pytest.fixture(scope="module")
def bd_repo():
    """A throwaway git repo with Beads initialised and a small dependency graph."""
    if bd_bin() is None:
        pytest.skip("bd not installed")
    root = Path(tempfile.mkdtemp(prefix="odysseus-bd-repo-"))
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=root, check=True)
    service = BeadsService()
    service.init(str(root))
    a = service.create(str(root), "Set up auth", issue_type="feature", priority=1)
    b = service.create(str(root), "Fix login bug", issue_type="bug", priority=0)
    service.create(str(root), "Add OAuth", issue_type="task")
    # a depends on b → only b (and the third) are "ready".
    service.dep_add(str(root), a["id"], b["id"])
    return {"root": str(root), "a": a["id"], "b": b["id"]}


# ── BeadsService ────────────────────────────────────────────────────────────
@bd_missing
def test_service_available():
    assert get_beads_service().available() is True


@bd_missing
def test_init_is_idempotent(bd_repo):
    again = BeadsService().init(bd_repo["root"])
    assert again["initialized"] is True
    assert again["already_initialized"] is True


@bd_missing
def test_summary_counts(bd_repo):
    summary = BeadsService().summary(bd_repo["root"])
    assert summary["total"] == 3
    assert summary["open"] == 3
    assert summary["blocked"] == 1  # `a` is blocked by `b`
    assert summary["ready"] == 2


@bd_missing
def test_ready_excludes_blocked(bd_repo):
    ready_ids = {i["id"] for i in BeadsService().ready(bd_repo["root"])}
    assert bd_repo["b"] in ready_ids
    assert bd_repo["a"] not in ready_ids


@bd_missing
def test_show_includes_dependencies(bd_repo):
    issue = BeadsService().show(bd_repo["root"], bd_repo["a"])
    assert issue["id"] == bd_repo["a"]
    dep_ids = {d["id"] for d in issue.get("dependencies", [])}
    assert bd_repo["b"] in dep_ids


@bd_missing
def test_graph_nodes_and_edges(bd_repo):
    graph = BeadsService().graph(bd_repo["root"])
    assert len(graph["nodes"]) == 3
    assert {"from": bd_repo["b"], "to": bd_repo["a"]} in graph["edges"]


@bd_missing
def test_create_and_close(bd_repo):
    service = BeadsService()
    created = service.create(bd_repo["root"], "Throwaway task", issue_type="task")
    assert created["title"] == "Throwaway task"
    result = service.close(bd_repo["root"], [created["id"]])
    assert created["id"] in result["closed"]
    open_ids = {i["id"] for i in service.list_issues(bd_repo["root"], include_closed=False)}
    assert created["id"] not in open_ids


@bd_missing
def test_read_before_init_raises():
    empty = Path(tempfile.mkdtemp(prefix="odysseus-bd-noinit-"))
    with pytest.raises(BeadsError) as exc:
        BeadsService().summary(str(empty))
    assert exc.value.status_code == 409


# ── Provider tool + routes (isolated DB) ────────────────────────────────────
@pytest.fixture
def beads_store(monkeypatch, bd_repo):
    """Isolated SQLite DB with one beads-enabled project pointing at ``bd_repo``."""
    import core.database as database
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from core.database import Base, CodingProject, CodingThread

    tmp = Path(tempfile.mkdtemp(prefix="odysseus-beads-db-"))
    db_path = tmp / "beads.sqlite"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    # Create the full schema: SQLite runs with PRAGMA foreign_keys=ON, so a
    # CodingThread insert needs every table its FKs reference (e.g. coding_tabs)
    # to exist, not just the handful we write to directly.
    Base.metadata.create_all(bind=engine)

    import routes.coding_routes as coding_routes
    import src.coding_runtime as coding_runtime

    monkeypatch.setattr(database, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(coding_routes, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(coding_runtime, "SessionLocal", TestingSessionLocal)
    # Keep RAG enrichment a no-op so tests don't spin up ChromaDB.
    import src.coding_beads_bridge as bridge
    monkeypatch.setattr(bridge, "reindex_project_in_rag", lambda *a, **k: None)

    project_id = f"project-{uuid.uuid4()}"
    thread_id = f"thread-{uuid.uuid4()}"
    db = TestingSessionLocal()
    try:
        db.add(CodingProject(
            id=project_id, owner="tester", name="Beads Project",
            root_path=bd_repo["root"], default_harness="generic", beads_enabled=True,
        ))
        db.add(CodingThread(
            id=thread_id, project_id=project_id, owner="tester", title="Agent",
            cwd=bd_repo["root"], harness_id="generic", status="idle", metadata_json="{}",
        ))
        db.commit()
    finally:
        db.close()
    return {"project_id": project_id, "thread_id": thread_id, "SessionLocal": TestingSessionLocal}


def _context(store):
    from src.coding_provider_tokens import ProviderContext
    return ProviderContext(
        token_id="tok",
        thread_id=store["thread_id"],
        project_id=store["project_id"],
        owner="tester",
        session_id=None,
        capabilities=frozenset({"beads.read", "beads.write"}),
        run_id=None,
    )


@bd_missing
def test_provider_tool_ready(beads_store):
    import asyncio

    from src.coding_provider_beads import call_beads_tool

    result = asyncio.run(call_beads_tool(_context(beads_store), "ready", {}))
    assert "ready" in result
    assert result["count"] >= 1


@bd_missing
def test_provider_tool_requires_capability(beads_store):
    import asyncio

    from src.coding_provider_beads import call_beads_tool
    from src.coding_provider_tokens import CodingProviderError, ProviderContext

    read_only = ProviderContext(
        token_id="tok", thread_id=beads_store["thread_id"], project_id=beads_store["project_id"],
        owner="tester", session_id=None, capabilities=frozenset({"beads.read"}), run_id=None,
    )
    with pytest.raises(CodingProviderError) as exc:
        asyncio.run(call_beads_tool(read_only, "create", {"title": "nope"}))
    assert exc.value.status_code == 403


@bd_missing
def test_provider_tool_create_and_list(beads_store):
    import asyncio

    from src.coding_provider_beads import call_beads_tool

    created = asyncio.run(call_beads_tool(_context(beads_store), "create", {"title": "From the tool", "issue_type": "task"}))
    assert created["issue"]["title"] == "From the tool"
    listed = asyncio.run(call_beads_tool(_context(beads_store), "list", {}))
    titles = {i["title"] for i in listed["issues"]}
    assert "From the tool" in titles


@bd_missing
def test_provider_tool_update(beads_store):
    import asyncio

    from src.coding_provider_beads import call_beads_tool

    created = asyncio.run(call_beads_tool(_context(beads_store), "create", {"title": "to update", "issue_type": "task"}))
    iid = created["issue"]["id"]
    res = asyncio.run(call_beads_tool(_context(beads_store), "update", {"issue_id": iid, "status": "in_progress", "priority": 1}))
    assert res["issue"]["status"] == "in_progress"
    assert res["issue"]["priority"] == 1


# ── str_arg helper ──────────────────────────────────────────────────────────
def test_str_arg_helper():
    from src.coding_provider_tool_common import str_arg

    assert str_arg({"x": "  hi "}, "x") == "hi"
    assert str_arg({"x": ""}, "x") is None
    assert str_arg({"x": "   "}, "x") is None
    assert str_arg({}, "x", "fallback") == "fallback"
    assert str_arg({"x": None}, "x") is None
    assert str_arg({"x": 5}, "x") == "5"


# ── Service: update ─────────────────────────────────────────────────────────
@bd_missing
def test_service_update(bd_repo):
    service = BeadsService()
    created = service.create(bd_repo["root"], "update via service", issue_type="task", priority=3)
    updated = service.update(bd_repo["root"], created["id"], status="in_progress", priority=1)
    assert updated["status"] == "in_progress"
    assert updated["priority"] == 1
    service.close(bd_repo["root"], [created["id"]])


@bd_missing
def test_service_update_rejects_bad_status(bd_repo):
    service = BeadsService()
    created = service.create(bd_repo["root"], "bad status target", issue_type="task")
    with pytest.raises(BeadsError) as exc:
        service.update(bd_repo["root"], created["id"], status="nonsense")
    assert exc.value.status_code == 400
    service.close(bd_repo["root"], [created["id"]])


# ── Routes (isolated DB + TestClient) ───────────────────────────────────────
@pytest.fixture
def beads_client(beads_store):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    import routes.coding_routes as coding_routes

    app = FastAPI()

    @app.middleware("http")
    async def stamp_user(request: Request, call_next):
        request.state.current_user = "tester"
        request.state.api_token = False
        return await call_next(request)

    app.include_router(coding_routes.setup_coding_routes())
    with TestClient(app) as client:
        yield client, beads_store["project_id"]


@bd_missing
def test_route_get_beads(beads_client):
    client, project_id = beads_client
    resp = client.get(f"/api/coding/projects/{project_id}/beads")
    assert resp.status_code == 200
    beads = resp.json()["beads"]
    assert beads["enabled"] is True
    assert beads["initialized"] is True
    assert "summary" in beads and beads["summary"]["total"] >= 1
    assert isinstance(beads.get("issues"), list)


@bd_missing
def test_route_create_show_update_close(beads_client):
    client, project_id = beads_client
    base = f"/api/coding/projects/{project_id}/beads"

    created = client.post(f"{base}/issues", json={"title": "route lifecycle", "issue_type": "task", "priority": 2})
    assert created.status_code == 200
    iid = created.json()["issue"]["id"]

    shown = client.get(f"{base}/issues/{iid}")
    assert shown.status_code == 200
    assert shown.json()["issue"]["id"] == iid

    patched = client.patch(f"{base}/issues/{iid}", json={"status": "in_progress", "priority": 0})
    assert patched.status_code == 200
    assert patched.json()["issue"]["status"] == "in_progress"
    assert patched.json()["issue"]["priority"] == 0

    closed = client.post(f"{base}/issues/{iid}/close", json={})
    assert closed.status_code == 200
    assert iid in closed.json()["closed"]


@bd_missing
def test_route_add_dependency(beads_client):
    client, project_id = beads_client
    base = f"/api/coding/projects/{project_id}/beads"

    blocker = client.post(f"{base}/issues", json={"title": "route blocker"}).json()["issue"]["id"]
    blocked = client.post(f"{base}/issues", json={"title": "route blocked"}).json()["issue"]["id"]

    resp = client.post(f"{base}/deps", json={"blocked_id": blocked, "blocker_id": blocker})
    assert resp.status_code == 200
    assert resp.json()["blocked"] == blocked and resp.json()["blocker"] == blocker

    # The blocked issue now lists the blocker among its dependencies.
    deps = client.get(f"{base}/issues/{blocked}").json()["issue"].get("dependencies", [])
    assert blocker in {d["id"] for d in deps}

    # DELETE removes it again.
    rm = client.request("DELETE", f"{base}/deps", json={"blocked_id": blocked, "blocker_id": blocker})
    assert rm.status_code == 200
    deps_after = client.get(f"{base}/issues/{blocked}").json()["issue"].get("dependencies", [])
    assert blocker not in {d["id"] for d in deps_after}

    client.post(f"{base}/issues/{blocked}/close", json={})
    client.post(f"{base}/issues/{blocker}/close", json={})


@bd_missing
def test_route_unknown_project_is_404(beads_client):
    client, _project_id = beads_client
    resp = client.get("/api/coding/projects/does-not-exist/beads")
    assert resp.status_code == 404


# ── Provider: dep_remove + graph envelope ───────────────────────────────────
@bd_missing
def test_provider_tool_graph_is_wrapped(beads_store):
    import asyncio

    from src.coding_provider_beads import call_beads_tool

    result = asyncio.run(call_beads_tool(_context(beads_store), "graph", {}))
    # Must match the socket/REST envelope: {"graph": {nodes, edges}}.
    assert "graph" in result
    assert "nodes" in result["graph"] and "edges" in result["graph"]


@bd_missing
def test_provider_tool_dep_remove(beads_store):
    import asyncio

    from src.coding_provider_beads import call_beads_tool

    ctx = _context(beads_store)
    blocked = asyncio.run(call_beads_tool(ctx, "create", {"title": "prov dep blocked"}))["issue"]["id"]
    blocker = asyncio.run(call_beads_tool(ctx, "create", {"title": "prov dep blocker"}))["issue"]["id"]
    asyncio.run(call_beads_tool(ctx, "dep", {"blocked_id": blocked, "blocker_id": blocker}))
    shown = asyncio.run(call_beads_tool(ctx, "show", {"issue_id": blocked}))
    assert blocker in {d["id"] for d in shown["issue"].get("dependencies", [])}
    # Alias 'undep' resolves to dep_remove.
    asyncio.run(call_beads_tool(ctx, "undep", {"blocked_id": blocked, "blocker_id": blocker}))
    shown2 = asyncio.run(call_beads_tool(ctx, "show", {"issue_id": blocked}))
    assert blocker not in {d["id"] for d in shown2["issue"].get("dependencies", [])}


# ── Socket dispatch (bead.* ops) ────────────────────────────────────────────
@bd_missing
def test_socket_bead_ops(beads_store):
    import asyncio

    from src.coding_socket import OdySocketServer

    server = OdySocketServer()
    sid = beads_store["project_id"]

    async def run():
        async def call(method, **params):
            return await server._dispatch[method]({"owner": "tester", "space_id": sid, **params})

        # Envelope parity with the provider tool: ready/list carry a count.
        ready = await call("bead.ready")
        assert "ready" in ready and ready["count"] == len(ready["ready"])
        listed = await call("bead.list")
        assert "issues" in listed and listed["count"] == len(listed["issues"])
        graph = await call("bead.graph")
        assert "graph" in graph and "nodes" in graph["graph"]

        created = await call("bead.create", title="socket issue", issue_type="task")
        iid = created["issue"]["id"]
        updated = await call("bead.update", issue_id=iid, status="in_progress")
        assert updated["issue"]["status"] == "in_progress"

        blocker = (await call("bead.create", title="socket blocker"))["issue"]["id"]
        await call("bead.dep", blocked_id=iid, blocker_id=blocker)
        shown = await call("bead.show", issue_id=iid)
        assert blocker in {d["id"] for d in shown["issue"].get("dependencies", [])}
        await call("bead.dep_remove", blocked_id=iid, blocker_id=blocker)
        shown2 = await call("bead.show", issue_id=iid)
        assert blocker not in {d["id"] for d in shown2["issue"].get("dependencies", [])}

        await call("bead.close", issue_id=iid)
        await call("bead.close", issue_id=blocker)
        await asyncio.sleep(0.1)  # let detached reindex tasks drain before loop close

    asyncio.run(run())


@bd_missing
def test_socket_bead_missing_param_is_400(beads_store):
    import asyncio

    from src.coding_runtime import CodingRuntimeError
    from src.coding_socket import OdySocketServer

    server = OdySocketServer()
    with pytest.raises(CodingRuntimeError) as exc:
        asyncio.run(server._dispatch["bead.show"]({"owner": "tester", "space_id": beads_store["project_id"]}))
    assert exc.value.status_code == 400


# ── Detached RAG reindex scheduling ─────────────────────────────────────────
def test_schedule_reindex_runs_detached(monkeypatch):
    import asyncio

    import src.coding_beads_bridge as bridge

    calls = []
    monkeypatch.setattr(bridge, "reindex_project_in_rag", lambda *a, **k: calls.append(a))

    async def run():
        bridge._schedule_reindex("owner", "proj", "/tmp/whatever")
        await asyncio.sleep(0.05)  # give the detached task a tick to run

    asyncio.run(run())
    assert calls and calls[0] == ("owner", "proj", "/tmp/whatever")


def test_schedule_reindex_without_loop_is_noop():
    import src.coding_beads_bridge as bridge

    # No running event loop → returns quietly instead of raising.
    bridge._schedule_reindex("o", "p", "/tmp/x")
