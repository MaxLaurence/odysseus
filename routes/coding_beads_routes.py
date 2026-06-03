"""Beads (`bd`) per-project backlog routes.

The repo-scoped, dependency-aware source of truth for work. Reads go through the
`bd` CLI (never the raw `.beads/` store) so its cache stays authoritative.

These endpoints live in their own module — mirroring the service/bridge/provider
split the rest of the integration already uses — and are mounted onto the main
coding router via :func:`register_beads_routes`. Owner-scoping + ``root_path``
resolution funnels through the single canonical resolver
``coding_beads_bridge.resolve_beads_project``; this module never re-queries
``CodingProject`` itself.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from src.coding_beads import BeadsError, get_beads_service
from src.coding_beads_bridge import (
    emit_beads_changed,
    resolve_beads_project,
    schedule_reconcile,
    set_beads_enabled,
)


class BeadsIssueCreate(BaseModel):
    title: str
    issue_type: str | None = None
    priority: int | None = None
    description: str | None = None
    discovered_from: str | None = None


class BeadsIssueUpdate(BaseModel):
    status: str | None = None
    priority: int | None = None
    title: str | None = None


class BeadsIssueClose(BaseModel):
    reason: str | None = None


class BeadsDepCreate(BaseModel):
    blocked_id: str
    blocker_id: str
    type: str | None = None


class BeadsInit(BaseModel):
    prefix: str | None = None


def register_beads_routes(router: APIRouter) -> None:
    """Mount the Beads endpoints onto an existing coding ``APIRouter``."""
    # Imported lazily so this module does not import the (larger) routes module
    # at import time — register_beads_routes is only called once the parent
    # module has finished loading.
    from routes.coding_routes import _owner

    def _resolve(owner: str, project_id: str) -> dict[str, Any]:
        """Owner-gate + resolve ``project_id`` → ``{root_path, beads_enabled, …}``."""
        try:
            return resolve_beads_project(owner, project_id)
        except BeadsError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc

    @router.get("/projects/{project_id}/beads")
    async def get_project_beads(request: Request, project_id: str,
                                include_closed: bool = Query(False)):
        owner = _owner(request)
        info = _resolve(owner, project_id)
        root_path = info["root_path"]
        service = get_beads_service()
        available = service.available()
        initialized = service.is_initialized(root_path)
        payload: dict[str, Any] = {
            "project_id": project_id,
            "enabled": info["beads_enabled"],
            "available": available,
            "initialized": initialized,
        }
        if available and initialized:
            try:
                payload["summary"] = await asyncio.to_thread(service.summary, root_path)
                payload["ready"] = await asyncio.to_thread(service.ready, root_path)
                payload["issues"] = await asyncio.to_thread(
                    service.list_issues, root_path, include_closed=include_closed, limit=200
                )
            except BeadsError as exc:
                raise HTTPException(exc.status_code, exc.detail) from exc
            # Catch up RAG if an agent mutated the backlog via raw `bd` in its PTY
            # (that path skips emit_beads_changed). Fingerprint-gated + detached, so
            # it costs nothing unless the backlog actually drifted, and never delays
            # this response.
            schedule_reconcile(owner, project_id, root_path)
        return {"beads": payload}

    @router.get("/projects/{project_id}/beads/graph")
    async def get_project_beads_graph(request: Request, project_id: str):
        root_path = _resolve(_owner(request), project_id)["root_path"]
        try:
            graph = await asyncio.to_thread(get_beads_service().graph, root_path)
        except BeadsError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc
        return {"graph": graph}

    @router.get("/projects/{project_id}/beads/issues/{issue_id}")
    async def show_project_beads_issue(request: Request, project_id: str, issue_id: str):
        root_path = _resolve(_owner(request), project_id)["root_path"]
        try:
            issue = await asyncio.to_thread(get_beads_service().show, root_path, issue_id)
        except BeadsError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc
        return {"issue": issue}

    @router.post("/projects/{project_id}/beads/init")
    async def init_project_beads(request: Request, project_id: str, body: BeadsInit | None = None):
        owner = _owner(request)
        root_path = _resolve(owner, project_id)["root_path"]
        service = get_beads_service()
        if not service.available():
            raise HTTPException(503, "bd (Beads) is not installed on the server.")
        try:
            result = await asyncio.to_thread(service.init, root_path, prefix=(body.prefix if body else None) or None)
        except BeadsError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc
        # Persist the opt-in flag now that the repo actually has a `.beads/` dir.
        set_beads_enabled(owner, project_id, True)
        return {"beads": {"project_id": project_id, "enabled": True, **result}}

    @router.post("/projects/{project_id}/beads/issues")
    async def create_project_beads_issue(request: Request, project_id: str, body: BeadsIssueCreate):
        owner = _owner(request)
        root_path = _resolve(owner, project_id)["root_path"]
        try:
            issue = await asyncio.to_thread(
                get_beads_service().create,
                root_path,
                body.title,
                issue_type=body.issue_type,
                priority=body.priority,
                description=body.description,
                discovered_from=body.discovered_from,
            )
        except BeadsError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc
        await emit_beads_changed(owner, project_id, root_path)
        return {"issue": issue}

    @router.patch("/projects/{project_id}/beads/issues/{issue_id}")
    async def update_project_beads_issue(request: Request, project_id: str, issue_id: str,
                                         body: BeadsIssueUpdate):
        owner = _owner(request)
        root_path = _resolve(owner, project_id)["root_path"]
        try:
            issue = await asyncio.to_thread(
                get_beads_service().update,
                root_path,
                issue_id,
                status=body.status,
                priority=body.priority,
                title=body.title,
            )
        except BeadsError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc
        await emit_beads_changed(owner, project_id, root_path)
        return {"issue": issue}

    @router.post("/projects/{project_id}/beads/issues/{issue_id}/close")
    async def close_project_beads_issue(request: Request, project_id: str, issue_id: str,
                                        body: BeadsIssueClose | None = None):
        owner = _owner(request)
        root_path = _resolve(owner, project_id)["root_path"]
        try:
            result = await asyncio.to_thread(
                get_beads_service().close, root_path, [issue_id], reason=(body.reason if body else None) or None
            )
        except BeadsError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc
        await emit_beads_changed(owner, project_id, root_path)
        return result

    @router.post("/projects/{project_id}/beads/deps")
    async def add_project_beads_dep(request: Request, project_id: str, body: BeadsDepCreate):
        owner = _owner(request)
        root_path = _resolve(owner, project_id)["root_path"]
        try:
            result = await asyncio.to_thread(
                get_beads_service().dep_add,
                root_path,
                body.blocked_id,
                body.blocker_id,
                dep_type=(body.type or "blocks"),
            )
        except BeadsError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc
        await emit_beads_changed(owner, project_id, root_path)
        return result

    @router.delete("/projects/{project_id}/beads/deps")
    async def remove_project_beads_dep(request: Request, project_id: str, body: BeadsDepCreate):
        owner = _owner(request)
        root_path = _resolve(owner, project_id)["root_path"]
        try:
            result = await asyncio.to_thread(
                get_beads_service().dep_remove, root_path, body.blocked_id, body.blocker_id
            )
        except BeadsError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc
        await emit_beads_changed(owner, project_id, root_path)
        return result


__all__ = ["register_beads_routes"]
