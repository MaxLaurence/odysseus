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


# ── beads → RAG enrichment round-trip ───────────────────────────────────────
class _FakeRag:
    """Minimal in-memory stand-in for VectorRAG with the surface the bridge and
    chat search both use: ``healthy``, ``delete_by_source``, ``add_documents_batch``
    and an ``owner``-filtered ``search``. Lets us prove the enrichment round-trips
    (issue → indexed doc → recalled by an owner-scoped query) without standing up
    ChromaDB + an embedding backend."""

    def __init__(self):
        self.healthy = True
        self.docs: list[tuple[str, dict]] = []

    def delete_by_source(self, source: str) -> int:
        before = len(self.docs)
        self.docs = [(t, m) for t, m in self.docs if m.get("source") != source]
        return before - len(self.docs)

    def add_documents_batch(self, docs):
        self.docs.extend(docs)
        return {"success": True, "added_count": len(docs)}

    def search(self, query: str, k: int = 5, owner=None):
        q = set(query.lower().split())
        hits = []
        for text, meta in self.docs:
            if owner is not None and meta.get("owner") != owner:
                continue  # mirror VectorRAG's owner where-filter
            if q & set(text.lower().split()):
                hits.append({"document": text, "metadata": meta})
        return hits[:k]


@bd_missing
def test_beads_enrichment_round_trips_into_rag(bd_repo, monkeypatch):
    """An open issue is indexed under the per-project source with owner metadata,
    and a later owner-scoped chat search recalls it — the exact path that lets a
    normal chat 'remember' the codebase backlog. This is the seam the suite used
    to mock out wholesale."""
    import src.coding_beads_bridge as bridge

    fake = _FakeRag()
    monkeypatch.setattr(bridge, "get_rag_manager", lambda: fake, raising=False)
    # get_rag_manager is imported lazily inside reindex_project_in_rag from
    # src.rag_singleton, so patch it at the source too.
    import src.rag_singleton as rag_singleton
    monkeypatch.setattr(rag_singleton, "get_rag_manager", lambda: fake)

    bridge.reindex_project_in_rag("tester", "proj-rt", bd_repo["root"])

    # Every indexed doc is tagged with the per-project source + owner, and points
    # back at a real issue id — the contract chat retrieval and re-enrichment rely on.
    assert fake.docs, "expected open issues to be indexed into RAG"
    for _text, meta in fake.docs:
        assert meta["source"] == bridge._rag_source("proj-rt")
        assert meta["owner"] == "tester"
        assert meta["kind"] == "beads_issue"
        assert meta["issue_id"]

    # The seed repo has an open 'Fix login bug' issue → an owner-scoped query for
    # it comes back, while a query under a *different* owner sees nothing.
    recalled = fake.search("login bug", owner="tester")
    assert any("login" in d["document"].lower() for d in recalled)
    assert fake.search("login bug", owner="someone-else") == []


@bd_missing
def test_beads_enrichment_replaces_stale_docs(bd_repo, monkeypatch):
    """Re-indexing is delete-by-source then re-add, so a project's docs don't
    accumulate stale rows across runs."""
    import src.coding_beads_bridge as bridge

    fake = _FakeRag()
    # A pre-existing stale doc for this project under the same source.
    source = bridge._rag_source("proj-stale")
    fake.docs.append(("Beads issue OLD: ancient removed issue", {"source": source, "owner": "tester"}))
    import src.rag_singleton as rag_singleton
    monkeypatch.setattr(rag_singleton, "get_rag_manager", lambda: fake)
    monkeypatch.setattr(bridge, "get_rag_manager", lambda: fake, raising=False)

    bridge.reindex_project_in_rag("tester", "proj-stale", bd_repo["root"])

    # The stale doc is gone; only freshly-indexed live issues remain.
    assert all("ancient removed issue" not in t for t, _m in fake.docs)
    assert fake.docs  # but the live issues are present


@pytest.mark.skipif(
    os.environ.get("ODYSSEUS_RAG_INTEGRATION") != "1",
    reason="real ChromaDB round-trip; set ODYSSEUS_RAG_INTEGRATION=1 to run",
)
@bd_missing
def test_beads_enrichment_real_chromadb(bd_repo):
    """Opt-in: exercise the genuine VectorRAG (ChromaDB + embeddings) so the
    metadata/owner-filter contract is verified against the real store, not a fake.
    Gated behind an env flag because it needs an embedding backend + Chroma."""
    from src.coding_beads_bridge import reindex_project_in_rag
    from src.rag_singleton import get_rag_manager

    rag = get_rag_manager()
    if rag is None or not getattr(rag, "healthy", False):
        pytest.skip("RAG/ChromaDB backend not available")

    reindex_project_in_rag("tester", "proj-real", bd_repo["root"])
    hits = rag.search("login bug", k=5, owner="tester")
    assert any(h.get("metadata", {}).get("kind") == "beads_issue" for h in hits)


# ── memory → Beads routing guard (structural, not just ody-guide prose) ──────
class _FakeMemoryManager:
    """Just enough of MemoryManager for the provider tool's add/edit paths."""

    def __init__(self):
        self.saved: list[dict] = []

    def load(self, owner=None):
        return list(self.saved)

    def load_all(self):
        return list(self.saved)

    def add_entry(self, text, source="user", category="fact", owner=None):
        return {"id": str(uuid.uuid4()), "text": text, "category": category,
                "source": source, "owner": owner, "uses": 0}

    def save(self, entries):
        self.saved = list(entries)


def _mem_context(*, project_id):
    from src.coding_provider_tokens import ProviderContext
    return ProviderContext(
        token_id="tok",
        thread_id="thread-x",
        project_id=project_id,
        owner="tester",
        session_id=None,
        capabilities=frozenset({"memory.read", "memory.write"}),
        run_id=None,
    )


def test_memory_add_rejects_work_category_when_project_scoped():
    """A project-scoped agent may not file a task/bug into owner memory — it gets
    a 400 pointing at the beads tool, so the 'memory = facts, Beads = work' split
    is enforced in code, not just the prompt."""
    from src.coding_provider_memory import call_memory_tool
    from src.coding_provider_tokens import CodingProviderError

    mgr = _FakeMemoryManager()
    for category in ("task", "todo", "bug", "backlog"):
        with pytest.raises(CodingProviderError) as exc:
            call_memory_tool(
                _mem_context(project_id="proj-1"),
                "add",
                {"text": "fix the flaky auth test", "category": category},
                mgr,
            )
        assert exc.value.status_code == 400
        assert "beads" in exc.value.detail.lower()
    assert mgr.saved == []  # nothing leaked into the store


def test_memory_add_allows_facts_when_project_scoped():
    """The guard is surgical: durable user facts still go through unimpeded."""
    from src.coding_provider_memory import call_memory_tool

    mgr = _FakeMemoryManager()
    result = call_memory_tool(
        _mem_context(project_id="proj-1"),
        "add",
        {"text": "I prefer trunk-based development", "category": "fact"},
        mgr,
    )
    assert result["memory"]["text"] == "I prefer trunk-based development"
    assert len(mgr.saved) == 1


def test_memory_add_allows_work_category_without_project_scope():
    """Outside a project context (no project_id) there's no Beads repo to route
    to, so the guard stays out of the way — non-coding memory writes are unchanged."""
    from src.coding_provider_memory import call_memory_tool

    mgr = _FakeMemoryManager()
    result = call_memory_tool(
        _mem_context(project_id=""),
        "add",
        {"text": "follow up on the invoice", "category": "task"},
        mgr,
    )
    assert result["memory"]["category"] == "task"
    assert len(mgr.saved) == 1


def test_memory_edit_rejects_relabel_to_work_category_when_project_scoped():
    """Closing the smuggling path: an existing fact can't be re-categorized into a
    work category to sneak a task into a project-scoped agent's memory."""
    from src.coding_provider_memory import call_memory_tool
    from src.coding_provider_tokens import CodingProviderError

    mgr = _FakeMemoryManager()
    existing = mgr.add_entry("some note", category="fact", owner="tester")
    mgr.saved = [existing]

    with pytest.raises(CodingProviderError) as exc:
        call_memory_tool(
            _mem_context(project_id="proj-1"),
            "edit",
            {"memory_id": existing["id"], "text": "actually a task now", "category": "task"},
            mgr,
        )
    assert exc.value.status_code == 400
    assert "beads" in exc.value.detail.lower()


# ── reconcile: catch direct-CLI (raw `bd`) drift on read ────────────────────
@bd_missing
def test_reconcile_reindexes_only_on_drift(bd_repo, monkeypatch):
    """The read-path reconcile is fingerprint-gated: it re-embeds when the backlog
    has drifted (e.g. an agent ran raw `bd create` in its PTY, bypassing the
    bridge) and no-ops when it hasn't — so reads stay cheap."""
    import src.coding_beads_bridge as bridge

    fake = _FakeRag()
    monkeypatch.setattr(bridge, "get_rag_manager", lambda: fake, raising=False)
    import src.rag_singleton as rag_singleton
    monkeypatch.setattr(rag_singleton, "get_rag_manager", lambda: fake)
    # Start from a clean fingerprint table so this test is order-independent.
    monkeypatch.setattr(bridge, "_RAG_FINGERPRINTS", {})

    # First reconcile on an un-indexed project always reindexes (no fingerprint yet).
    assert bridge.reconcile_project_in_rag("tester", "proj-recon", bd_repo["root"]) is True
    indexed_after_first = len(fake.docs)
    assert indexed_after_first > 0

    # No change → second reconcile is a no-op (fingerprint matches, no re-embed).
    assert bridge.reconcile_project_in_rag("tester", "proj-recon", bd_repo["root"]) is False
    assert len(fake.docs) == indexed_after_first

    # Simulate a raw-CLI write: a new issue appears in `.beads/` out-of-band.
    get_beads_service().create(bd_repo["root"], "Snuck in via raw bd", issue_type="task")
    # Now the fingerprint differs → reconcile detects drift and reindexes.
    assert bridge.reconcile_project_in_rag("tester", "proj-recon", bd_repo["root"]) is True
    assert any("Snuck in via raw bd" in t for t, _m in fake.docs)


def test_schedule_reconcile_without_loop_is_noop():
    import src.coding_beads_bridge as bridge

    # No running event loop → returns quietly instead of raising.
    bridge.schedule_reconcile("o", "p", "/tmp/x")


def test_issues_fingerprint_is_order_independent():
    from src.coding_beads_bridge import _issues_fingerprint

    a = [{"id": "x1", "title": "A", "issue_type": "bug", "status": "open"},
         {"id": "x2", "title": "B", "issue_type": "task", "status": "open"}]
    b = list(reversed(a))
    assert _issues_fingerprint(a) == _issues_fingerprint(b)
    # A status change moves the fingerprint (the doc text would change).
    c = [dict(a[0], status="closed"), a[1]]
    assert _issues_fingerprint(a) != _issues_fingerprint(c)

