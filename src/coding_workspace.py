"""Code Station workspace service — Spaces, Tabs, Layouts, and agent state.

This is the reusable seam for the herdr-style hierarchy that sits *above* the
run/PTY layer owned by ``CodingRuntimeService``:

    Space (= CodingProject + live git branch, worktree children nested)
      └─ Tab (CodingTab, renamable, owns one pane split-tree)
          └─ Pane (a leaf in the tab's serialized layout tree)
              └─ Agent (= CodingThread, with semantic ``agent_state``)

REST routes (``routes/coding_routes.py``), the Unix-socket JSON-RPC server, and
the in-process provider tool all call this one service so the three callers stay
in lockstep — mirroring how they already share ``CodingRuntimeService``.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import uuid
from datetime import datetime
from typing import Any

from core.database import (
    CodingLayout,
    CodingProject,
    CodingTab,
    CodingThread,
    SessionLocal,
)
from src.coding_runtime import CodingRuntimeError

# Semantic agent states, ordered by urgency (most → least) for space rollups.
AGENT_STATES = ("blocked", "working", "done", "idle", "unknown")
_AGENT_STATE_SET = set(AGENT_STATES)
_AGENT_URGENCY = {state: len(AGENT_STATES) - i for i, state in enumerate(AGENT_STATES)}


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except Exception:
        return fallback


def agent_state_urgency(state: str | None) -> int:
    """Higher = more attention-worthy. Used to roll panes/tabs up to a space."""
    return _AGENT_URGENCY.get(state or "idle", 0)


def rollup_state(states: list[str | None]) -> str:
    """Most-urgent state across a collection (the herdr space/tab dot)."""
    best = "idle"
    best_rank = -1
    for state in states:
        rank = agent_state_urgency(state)
        if rank > best_rank:
            best_rank = rank
            best = state or "idle"
    return best


# Dot-token urgency mirroring the frontend's STATE_URGENCY so server- and client-
# computed rollups agree (covers run-lifecycle tokens, not just semantic states).
_TOKEN_URGENCY = {
    "blocked": 6, "working": 5, "running": 5, "starting": 4, "queued": 4, "pending": 4,
    "stopping": 4, "done": 2, "exited": 2, "failed": 2, "stopped": 2, "cancelled": 2,
    "complete": 2, "completed": 2, "idle": 1, "empty": 0, "unknown": 0,
}


def dot_token(agent_state: str | None, status: str | None) -> str:
    """Mirror of the frontend agentDotStatus(): semantic state wins, else run status."""
    sem = (agent_state or "").lower()
    if sem in ("working", "blocked", "done"):
        return sem
    s = (status or "idle").lower()
    if s == "running":
        return "working"
    if s in ("queued", "starting", "pending", "stopping"):
        return "queued"
    if s in ("exited", "failed", "stopped", "cancelled", "canceled", "complete", "completed"):
        return "done"
    return "unknown" if sem == "unknown" else "idle"


def rollup_tokens(tokens: list[str]) -> str:
    """Most-urgent dot-token across a collection."""
    best = "idle"
    best_rank = -1
    for token in tokens:
        rank = _TOKEN_URGENCY.get(token, 0)
        if rank > best_rank:
            best_rank = rank
            best = token
    return best


class CodingWorkspaceService:
    """Stateless-ish CRUD service for spaces, tabs, layouts and agent state."""

    def __init__(self) -> None:
        # Tiny TTL cache so listing N spaces doesn't shell out to git N times per poll.
        self._branch_cache: dict[str, tuple[float, str | None]] = {}
        self._branch_ttl = 5.0
        # Per-tab locks serialize layout read-modify-write so concurrent pane ops
        # (e.g. two agents splitting the same tab over the socket) can't clobber.
        self._tab_locks: dict[str, threading.Lock] = {}
        self._tab_locks_guard = threading.Lock()

    def _tab_lock(self, tab_id: str) -> threading.Lock:
        with self._tab_locks_guard:
            lock = self._tab_locks.get(tab_id)
            if lock is None:
                lock = threading.Lock()
                self._tab_locks[tab_id] = lock
            return lock

    def _mutate_layout(self, owner: str, tab_id: str, mutate) -> dict[str, Any]:
        """Atomic read-modify-write of a tab's layout. `mutate(tree, focus)` returns
        (new_tree, new_focus). Serialized per tab + done in one transaction so
        concurrent pane mutations don't lose each other's changes."""
        with self._tab_lock(tab_id):
            db = SessionLocal()
            try:
                self._require_tab(db, owner, tab_id)  # gates ownership via the tab
                layout = (
                    db.query(CodingLayout)
                    .filter(CodingLayout.tab_id == tab_id, CodingLayout.owner == owner)
                    .first()
                )
                tree = _json_loads(layout.tree_json, None) if layout else None
                cur_focus = layout.focus_pane_id if layout else None
                new_tree, new_focus = mutate(tree, cur_focus)
                tree_json = json.dumps(new_tree, ensure_ascii=False, separators=(",", ":"))
                if layout is None:
                    layout = CodingLayout(tab_id=tab_id, owner=owner, tree_json=tree_json, focus_pane_id=new_focus)
                    db.add(layout)
                else:
                    layout.tree_json = tree_json
                    layout.focus_pane_id = new_focus
                    layout.updated_at = datetime.utcnow()
                db.commit()
                db.refresh(layout)
                return self.layout_dict(tab_id, layout)
            finally:
                db.close()

    # ------------------------------------------------------------------ git
    def git_branch(self, root_path: str | None, owner: str | None = None) -> str | None:
        if not root_path:
            return None
        now = time.time()
        # Key on (owner, path) so two owners pointing at the same path can't read
        # each other's cached branch label.
        cache_key = f"{owner or ''}\x00{root_path}"
        cached = self._branch_cache.get(cache_key)
        if cached and (now - cached[0]) < self._branch_ttl:
            return cached[1]
        branch: str | None = None
        try:
            proc = subprocess.run(
                ["git", "-C", root_path, "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=2.0,
            )
            if proc.returncode == 0:
                branch = (proc.stdout or "").strip() or None
                if branch == "HEAD":  # detached: show short sha instead
                    sha = subprocess.run(
                        ["git", "-C", root_path, "rev-parse", "--short", "HEAD"],
                        capture_output=True, text=True, timeout=2.0,
                    )
                    branch = f"({(sha.stdout or '').strip()})" if sha.returncode == 0 else "(detached)"
        except Exception:
            branch = None
        self._branch_cache[cache_key] = (now, branch)
        return branch

    # --------------------------------------------------------------- spaces
    def space_dict(self, project: CodingProject) -> dict[str, Any]:
        return {
            "id": project.id,
            "owner": project.owner,
            "name": project.name,
            "root_path": project.root_path,
            "description": project.description or "",
            "default_harness": project.default_harness or "generic",
            "default_endpoint_id": project.default_endpoint_id or "",
            "default_model": project.default_model or "",
            "archived": bool(project.archived),
            "parent_project_id": getattr(project, "parent_project_id", None),
            "kind": getattr(project, "kind", None) or "root",
            "worktree_branch": getattr(project, "worktree_branch", None),
            "worktree_path": getattr(project, "worktree_path", None),
            "branch": self.git_branch(project.root_path, project.owner),
            "created_at": _iso(project.created_at),
            "updated_at": _iso(project.updated_at),
        }

    def list_spaces(self, owner: str, include_archived: bool = False) -> list[dict[str, Any]]:
        """Flat list of spaces (projects). The UI nests worktrees under their
        ``parent_project_id`` and computes status rollups from agent_state."""
        db = SessionLocal()
        try:
            query = db.query(CodingProject).filter(CodingProject.owner == owner)
            if not include_archived:
                query = query.filter(CodingProject.archived == False)  # noqa: E712
            projects = query.order_by(CodingProject.updated_at.desc()).all()
            spaces = [self.space_dict(p) for p in projects]
            # Roll each space's agent state up from its threads (one query).
            rows = (
                db.query(CodingThread.project_id, CodingThread.agent_state)
                .filter(CodingThread.owner == owner)
                .all()
            )
            by_project: dict[str, list[str | None]] = {}
            for project_id, state in rows:
                by_project.setdefault(project_id, []).append(state)
            for space in spaces:
                space["agent_state"] = rollup_state(by_project.get(space["id"], []))
            return spaces
        finally:
            db.close()

    # ----------------------------------------------------------------- tabs
    def tab_dict(self, tab: CodingTab) -> dict[str, Any]:
        return {
            "id": tab.id,
            "owner": tab.owner,
            "project_id": tab.project_id,
            "space_id": tab.project_id,
            "label": tab.label,
            "position": tab.position,
            "metadata": _json_loads(tab.metadata_json, {}),
            "created_at": _iso(tab.created_at),
            "updated_at": _iso(tab.updated_at),
        }

    def _require_space(self, db, owner: str, project_id: str) -> CodingProject:
        project = (
            db.query(CodingProject)
            .filter(CodingProject.owner == owner, CodingProject.id == project_id)
            .first()
        )
        if not project:
            raise CodingRuntimeError(404, "Space not found")
        return project

    def _require_tab(self, db, owner: str, tab_id: str) -> CodingTab:
        tab = (
            db.query(CodingTab)
            .filter(CodingTab.owner == owner, CodingTab.id == tab_id)
            .first()
        )
        if not tab:
            raise CodingRuntimeError(404, "Tab not found")
        return tab

    def list_tabs(self, owner: str, project_id: str, ensure_default: bool = True) -> list[dict[str, Any]]:
        db = SessionLocal()
        try:
            self._require_space(db, owner, project_id)
            tabs = (
                db.query(CodingTab)
                .filter(CodingTab.owner == owner, CodingTab.project_id == project_id)
                .order_by(CodingTab.position.asc(), CodingTab.created_at.asc())
                .all()
            )
            if not tabs and ensure_default:
                tab = CodingTab(
                    id=str(uuid.uuid4()), owner=owner, project_id=project_id,
                    label="terminal", position=0,
                )
                db.add(tab)
                db.commit()
                db.refresh(tab)
                tabs = [tab]
            dicts = [self.tab_dict(t) for t in tabs]
            self._attach_tab_rollups(db, owner, project_id, tabs, dicts)
            return dicts
        finally:
            db.close()

    def _attach_tab_rollups(self, db, owner: str, project_id: str, tabs, dicts) -> None:
        """Compute each tab's rollup dot from its stored layout's threadIds + those
        threads' live state — so even background tabs show a real status."""
        try:
            tab_ids = [t.id for t in tabs]
            if not tab_ids:
                return
            tree_by_tab = {
                row.tab_id: _json_loads(row.tree_json, None)
                for row in db.query(CodingLayout).filter(CodingLayout.tab_id.in_(tab_ids)).all()
            }
            state_by_thread = {
                tid: (st, status)
                for tid, st, status in db.query(
                    CodingThread.id, CodingThread.agent_state, CodingThread.status
                ).filter(CodingThread.owner == owner, CodingThread.project_id == project_id).all()
            }
            for d in dicts:
                tokens = []
                for leaf in self._iter_leaves(tree_by_tab.get(d["id"])):
                    tid = leaf.get("threadId")
                    if tid and tid in state_by_thread:
                        sem, status = state_by_thread[tid]
                        tokens.append(dot_token(sem, status))
                d["agent_state"] = rollup_tokens(tokens)
        except Exception:
            for d in dicts:
                d.setdefault("agent_state", "idle")

    def create_tab(self, owner: str, project_id: str, label: str | None = None,
                   position: int | None = None) -> dict[str, Any]:
        db = SessionLocal()
        try:
            self._require_space(db, owner, project_id)
            if position is None:
                top = (
                    db.query(CodingTab.position)
                    .filter(CodingTab.owner == owner, CodingTab.project_id == project_id)
                    .order_by(CodingTab.position.desc())
                    .first()
                )
                position = (top[0] + 1) if top and top[0] is not None else 0
            tab = CodingTab(
                id=str(uuid.uuid4()), owner=owner, project_id=project_id,
                label=(label or "terminal").strip() or "terminal", position=position,
            )
            db.add(tab)
            db.commit()
            db.refresh(tab)
            return self.tab_dict(tab)
        finally:
            db.close()

    def update_tab(self, owner: str, tab_id: str, *, label: str | None = None,
                   position: int | None = None, metadata: dict | None = None) -> dict[str, Any]:
        db = SessionLocal()
        try:
            tab = self._require_tab(db, owner, tab_id)
            if label is not None:
                cleaned = label.strip()
                if not cleaned:
                    raise CodingRuntimeError(400, "Tab label is required")
                tab.label = cleaned
            if position is not None:
                tab.position = position
            if metadata is not None:
                tab.metadata_json = json.dumps(metadata)
            tab.updated_at = datetime.utcnow()
            db.commit()
            db.refresh(tab)
            return self.tab_dict(tab)
        finally:
            db.close()

    def delete_tab(self, owner: str, tab_id: str) -> None:
        db = SessionLocal()
        try:
            tab = self._require_tab(db, owner, tab_id)
            # Detach any agents mounted in this tab so they aren't orphaned.
            db.query(CodingThread).filter(
                CodingThread.owner == owner, CodingThread.tab_id == tab_id
            ).update({CodingThread.tab_id: None, CodingThread.pane_id: None})
            db.delete(tab)  # cascade removes the layout row
            db.commit()
        finally:
            db.close()

    # -------------------------------------------------------------- layouts
    def layout_dict(self, tab_id: str, layout: CodingLayout | None) -> dict[str, Any]:
        if layout is None:
            return {"tab_id": tab_id, "tree": None, "focus_pane_id": None, "updated_at": None}
        return {
            "tab_id": layout.tab_id,
            "tree": _json_loads(layout.tree_json, None),
            "focus_pane_id": layout.focus_pane_id,
            "updated_at": _iso(layout.updated_at),
        }

    def get_layout(self, owner: str, tab_id: str) -> dict[str, Any]:
        db = SessionLocal()
        try:
            self._require_tab(db, owner, tab_id)  # gates ownership via the tab
            layout = (
                db.query(CodingLayout)
                .filter(CodingLayout.tab_id == tab_id, CodingLayout.owner == owner)
                .first()
            )
            return self.layout_dict(tab_id, layout)
        finally:
            db.close()

    def put_layout(self, owner: str, tab_id: str, tree: Any,
                   focus_pane_id: str | None = None) -> dict[str, Any]:
        db = SessionLocal()
        try:
            self._require_tab(db, owner, tab_id)  # gates ownership via the tab
            tree_json = json.dumps(tree, ensure_ascii=False, separators=(",", ":"))
            layout = (
                db.query(CodingLayout)
                .filter(CodingLayout.tab_id == tab_id, CodingLayout.owner == owner)
                .first()
            )
            if layout is None:
                layout = CodingLayout(
                    tab_id=tab_id, owner=owner, tree_json=tree_json,
                    focus_pane_id=focus_pane_id,
                )
                db.add(layout)
            else:
                layout.tree_json = tree_json
                layout.focus_pane_id = focus_pane_id
                layout.updated_at = datetime.utcnow()
            db.commit()
            db.refresh(layout)
            return self.layout_dict(tab_id, layout)
        finally:
            db.close()

    # --------------------------------------------------------------- agents
    async def set_agent_state(self, owner: str, thread_id: str, state: str, *,
                              run_id: str | None = None, message: str | None = None) -> dict[str, Any]:
        """Persist a thread's semantic agent state and broadcast it on the event
        stream so every SSE/socket subscriber sees the change (the herdr rollup)."""
        if state not in _AGENT_STATE_SET:
            raise CodingRuntimeError(400, f"Invalid agent state: {state}")
        db = SessionLocal()
        try:
            thread = (
                db.query(CodingThread)
                .filter(CodingThread.owner == owner, CodingThread.id == thread_id)
                .first()
            )
            if not thread:
                raise CodingRuntimeError(404, "Agent (thread) not found")
            thread.agent_state = state
            thread.state_changed_at = datetime.utcnow()
            # Read before commit — expire_on_commit would otherwise force a reload.
            effective_run = run_id or thread.last_run_id
            project_id = thread.project_id
            db.commit()
        finally:
            db.close()
        # Broadcast via the runtime's event entrypoint (lazy import avoids cycles).
        from src.coding_runtime import get_coding_runtime_service
        runtime = get_coding_runtime_service()
        payload = {"state": state}
        if message:
            payload["message"] = message
        try:
            await runtime.append_event(thread_id, effective_run, "agent_state_changed", payload)
        except Exception:
            pass  # state is persisted regardless; broadcast is best-effort
        return {"thread_id": thread_id, "project_id": project_id, "state": state}


    def get_space(self, owner: str, space_id: str) -> dict[str, Any]:
        db = SessionLocal()
        try:
            return self.space_dict(self._require_space(db, owner, space_id))
        finally:
            db.close()

    def rename_space(self, owner: str, space_id: str, name: str) -> dict[str, Any]:
        cleaned = (name or "").strip()
        if not cleaned:
            raise CodingRuntimeError(400, "Space name is required")
        db = SessionLocal()
        try:
            project = self._require_space(db, owner, space_id)
            project.name = cleaned
            project.updated_at = datetime.utcnow()
            db.commit()
            db.refresh(project)
            return self.space_dict(project)
        finally:
            db.close()

    def get_tab(self, owner: str, tab_id: str) -> dict[str, Any]:
        db = SessionLocal()
        try:
            return self.tab_dict(self._require_tab(db, owner, tab_id))
        finally:
            db.close()

    # --------------------------------------------------------- agents (read)
    def agent_dict(self, thread: CodingThread) -> dict[str, Any]:
        return {
            "id": thread.id,
            "thread_id": thread.id,
            "owner": thread.owner,
            "space_id": thread.project_id,
            "project_id": thread.project_id,
            "tab_id": thread.tab_id,
            "pane_id": thread.pane_id,
            "title": thread.title,
            "name": thread.title,
            "harness": thread.harness_id or "generic",
            "harness_id": thread.harness_id or "generic",
            "cwd": thread.cwd,
            "status": thread.status or "idle",
            "agent_state": thread.agent_state or "idle",
            "last_run_id": thread.last_run_id,
            "state_changed_at": _iso(thread.state_changed_at),
            "updated_at": _iso(thread.updated_at),
        }

    def list_agents(self, owner: str, *, space_id: str | None = None,
                    running_only: bool = False) -> list[dict[str, Any]]:
        db = SessionLocal()
        try:
            q = db.query(CodingThread).filter(CodingThread.owner == owner)
            if space_id:
                q = q.filter(CodingThread.project_id == space_id)
            threads = q.order_by(CodingThread.updated_at.desc()).all()
            agents = [self.agent_dict(t) for t in threads]
            if running_only:
                agents = [a for a in agents if a["agent_state"] in ("working", "blocked")]
            return agents
        finally:
            db.close()

    def get_agent(self, owner: str, agent_id: str) -> dict[str, Any]:
        db = SessionLocal()
        try:
            thread = (
                db.query(CodingThread)
                .filter(CodingThread.owner == owner, CodingThread.id == agent_id)
                .first()
            )
            if not thread:
                raise CodingRuntimeError(404, "Agent (thread) not found")
            return self.agent_dict(thread)
        finally:
            db.close()

    # ------------------------------------------------ layout tree (pane) ops
    # Node shape mirrors the frontend split-tree exactly so the stored layout is
    # interchangeable between the browser and socket/CLI callers:
    #   LEAF  { id, kind:'leaf', threadId, runId, paneId, status, title }
    #   SPLIT { id, kind:'split', dir:'row'|'col', a, b, ratio }
    @staticmethod
    def _new_node_id(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _iter_leaves(node):
        if not node:
            return
        if node.get("kind") == "leaf":
            yield node
        else:
            yield from CodingWorkspaceService._iter_leaves(node.get("a"))
            yield from CodingWorkspaceService._iter_leaves(node.get("b"))

    @staticmethod
    def _locate(node, pane_id, parent=None, side=None):
        """Locate a LEAF by its paneId → (node, parent, side)."""
        if not node:
            return None
        if node.get("kind") == "leaf":
            return (node, parent, side) if node.get("paneId") == pane_id else None
        return (CodingWorkspaceService._locate(node.get("a"), pane_id, node, "a")
                or CodingWorkspaceService._locate(node.get("b"), pane_id, node, "b"))

    @staticmethod
    def _locate_node(node, node_id, parent=None, side=None):
        """Locate any node by its id → (node, parent, side)."""
        if not node:
            return None
        if node.get("id") == node_id:
            return (node, parent, side)
        if node.get("kind") == "split":
            return (CodingWorkspaceService._locate_node(node.get("a"), node_id, node, "a")
                    or CodingWorkspaceService._locate_node(node.get("b"), node_id, node, "b"))
        return None

    def _empty_leaf(self) -> dict[str, Any]:
        return {
            "id": self._new_node_id("p"), "kind": "leaf", "threadId": None,
            "runId": "", "paneId": self._new_node_id("pane"), "status": "empty", "title": None,
        }

    def list_panes(self, owner: str, tab_id: str) -> list[dict[str, Any]]:
        layout = self.get_layout(owner, tab_id)
        return [
            {"pane_id": leaf.get("paneId"), "id": leaf.get("id"), "thread_id": leaf.get("threadId"),
             "run_id": leaf.get("runId"), "status": leaf.get("status"), "title": leaf.get("title")}
            for leaf in self._iter_leaves(layout.get("tree"))
        ]

    def split_pane(self, owner: str, tab_id: str, pane_id: str | None = None,
                   direction: str = "right") -> dict[str, Any]:
        dir_ = "row" if direction in ("right", "left") else "col"
        side = "a" if direction in ("left", "up") else "b"
        new_leaf = self._empty_leaf()

        def mutate(tree, _focus):
            if not tree:
                return new_leaf, new_leaf["paneId"]
            loc = self._locate(tree, pane_id) if pane_id else None
            if pane_id and not loc:
                raise CodingRuntimeError(404, "Pane not found")
            target = loc[0] if loc else tree
            split = {
                "id": self._new_node_id("n"), "kind": "split", "dir": dir_, "ratio": 0.5,
                "a": new_leaf if side == "a" else target,
                "b": target if side == "a" else new_leaf,
            }
            if loc and loc[1]:
                loc[1][loc[2]] = split
                return tree, new_leaf["paneId"]
            return split, new_leaf["paneId"]

        saved = self._mutate_layout(owner, tab_id, mutate)
        return {"pane": {"pane_id": new_leaf["paneId"], "id": new_leaf["id"]}, "layout": saved}

    def close_pane(self, owner: str, tab_id: str, pane_id: str) -> dict[str, Any]:
        def mutate(tree, _focus):
            loc = self._locate(tree, pane_id) if tree else None
            if not loc:
                raise CodingRuntimeError(404, "Pane not found")
            _node, parent, side = loc
            if not parent:
                new_tree = None
            else:
                sibling = parent["b" if side == "a" else "a"]
                gloc = self._locate_node(tree, parent["id"])
                if not gloc or not gloc[1]:
                    new_tree = sibling
                else:
                    gloc[1][gloc[2]] = sibling
                    new_tree = tree
            # Focus the surviving sibling's first pane so client + server agree.
            focus = next((leaf.get("paneId") for leaf in self._iter_leaves(new_tree)), None)
            return new_tree, focus

        saved = self._mutate_layout(owner, tab_id, mutate)
        return {"layout": saved}

    def rename_pane(self, owner: str, tab_id: str, pane_id: str, title: str | None) -> dict[str, Any]:
        def mutate(tree, focus):
            loc = self._locate(tree, pane_id) if tree else None
            if not loc:
                raise CodingRuntimeError(404, "Pane not found")
            loc[0]["title"] = title
            return tree, focus

        saved = self._mutate_layout(owner, tab_id, mutate)
        return {"pane": {"pane_id": pane_id, "title": title}, "layout": saved}


_SERVICE: CodingWorkspaceService | None = None


def get_coding_workspace_service() -> CodingWorkspaceService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = CodingWorkspaceService()
    return _SERVICE
