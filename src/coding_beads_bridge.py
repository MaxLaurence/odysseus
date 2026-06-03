"""Bridges Beads to the rest of Odysseus — *bridge, don't merge*.

Beads stays the source of truth for work; the other stores only ever *reference*
it. The seams:

  * **Project resolution** — map an owner-scoped ``CodingProject`` to the repo
    ``root_path`` Beads operates on (the owner boundary is enforced here, before
    any ``bd`` call).
  * **RAG read-enrichment** — index issue titles/descriptions into the RAG
    collection under a per-project ``source`` so chatting about a project can
    semantically recall "did we ever deal with X?". This is one-way and
    read-only; we never copy work items into the memory store (whose LLM auditor
    would consolidate/forget them — exactly what a backlog must not do).
  * **Events** — when work changes, append a ``beads_changed`` thread event (the
    existing stream the UI dots already poll) and fire an app event for task
    automation.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.coding_beads import BeadsError, get_beads_service

logger = logging.getLogger(__name__)

# Metadata source prefix so we can clear + reindex a single project's issues.
RAG_SOURCE_PREFIX = "beads:"

# Strong refs to in-flight detached reindex tasks so they aren't GC'd mid-run.
_REINDEX_TASKS: set[asyncio.Task] = set()


def resolve_beads_project(owner: str, project_id: str) -> dict[str, Any]:
    """Resolve ``(owner, project_id)`` → ``{root_path, beads_enabled, name}``.

    Raises ``BeadsError(404)`` if the owner has no such project. This is the
    owner-scope gate: every Beads caller goes through it before touching ``bd``.
    """
    from core.database import CodingProject, SessionLocal

    db = SessionLocal()
    try:
        project = (
            db.query(CodingProject)
            .filter(CodingProject.owner == owner, CodingProject.id == project_id)
            .first()
        )
        if not project:
            raise BeadsError(404, "Project not found")
        return {
            "root_path": project.root_path,
            "beads_enabled": bool(getattr(project, "beads_enabled", False)),
            "name": project.name,
        }
    finally:
        db.close()


def set_beads_enabled(owner: str, project_id: str, enabled: bool) -> None:
    """Flip the opt-in flag on a project (persisted)."""
    from datetime import datetime

    from core.database import CodingProject, SessionLocal

    db = SessionLocal()
    try:
        project = (
            db.query(CodingProject)
            .filter(CodingProject.owner == owner, CodingProject.id == project_id)
            .first()
        )
        if not project:
            raise BeadsError(404, "Project not found")
        project.beads_enabled = bool(enabled)
        project.updated_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()


def _rag_source(project_id: str) -> str:
    return f"{RAG_SOURCE_PREFIX}{project_id}"


def _issue_document(issue: dict[str, Any]) -> str:
    parts = [f"Beads issue {issue.get('id')}: {issue.get('title', '')}".strip()]
    itype = issue.get("issue_type")
    status = issue.get("status")
    meta = " ".join(p for p in (itype, status) if p)
    if meta:
        parts.append(f"[{meta}]")
    return " ".join(parts)


def reindex_project_in_rag(owner: str, project_id: str, root_path: str) -> None:
    """Best-effort: replace this project's Beads docs in the RAG collection.

    Delete-by-source then batch-add every open issue, so retrieval reflects the
    current backlog without accumulating stale rows. Never raises — RAG is an
    enrichment, not the source of truth. Runs ``bd`` + embeddings, so callers
    should invoke it off the request hot path (e.g. via a thread)."""
    try:
        from src.rag_singleton import get_rag_manager

        rag = get_rag_manager()
        if rag is None or not getattr(rag, "healthy", False):
            return
        issues = get_beads_service().list_issues(root_path, include_closed=False, limit=1000)
        source = _rag_source(project_id)
        try:
            rag.delete_by_source(source)
        except Exception:
            logger.debug("beads RAG delete_by_source failed", exc_info=True)
        if not issues:
            return
        docs = [
            (
                _issue_document(issue),
                {
                    "source": source,
                    "owner": owner or "",
                    "project_id": project_id,
                    "issue_id": issue.get("id") or "",
                    "issue_title": issue.get("title") or "",
                    "kind": "beads_issue",
                },
            )
            for issue in issues
            if issue.get("id")
        ]
        if docs:
            rag.add_documents_batch(docs)
    except BeadsError:
        return  # bd unavailable / not initialised — nothing to enrich
    except Exception:
        logger.debug("beads RAG reindex failed", exc_info=True)


def _schedule_reindex(owner: str, project_id: str, root_path: str) -> None:
    """Kick off the RAG re-enrichment as a detached background task.

    Re-embedding the whole backlog is slow; it must not sit on the create/close
    request's critical path. Fire-and-forget on the running loop and keep a strong
    ref so the task survives until it finishes."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no loop (sync context) — nothing the UI is waiting on anyway
    task = loop.create_task(
        asyncio.to_thread(reindex_project_in_rag, owner, project_id, root_path)
    )
    _REINDEX_TASKS.add(task)
    task.add_done_callback(_REINDEX_TASKS.discard)


async def emit_beads_changed(
    owner: str,
    project_id: str,
    root_path: str,
    *,
    thread_id: str | None = None,
    run_id: str | None = None,
    reindex: bool = True,
) -> dict[str, int]:
    """Announce a backlog change: append a ``beads_changed`` thread event (so the
    UI rollup updates over the existing stream), fire an app event for task
    automation, and best-effort re-enrich RAG. Returns the fresh summary counts.

    Only the cheap, UI-relevant work (summary + event append) is awaited; the RAG
    re-enrichment is detached so it never delays the write that triggered it. All
    side effects are guarded — a broadcast/enrichment failure must never fail the
    write."""
    summary: dict[str, int] = {}
    try:
        summary = get_beads_service().summary(root_path)
    except Exception:
        logger.debug("beads summary failed for event", exc_info=True)

    if thread_id:
        try:
            from src.coding_runtime import get_coding_runtime_service

            await get_coding_runtime_service().append_event(
                thread_id, run_id, "beads_changed",
                {"project_id": project_id, "summary": summary},
            )
        except Exception:
            logger.debug("beads_changed event append failed", exc_info=True)

    try:
        from src.event_bus import fire_event

        fire_event("coding.beads.changed", owner or None)
    except Exception:
        logger.debug("beads fire_event failed", exc_info=True)

    if reindex:
        _schedule_reindex(owner, project_id, root_path)

    return summary


__all__ = [
    "RAG_SOURCE_PREFIX",
    "emit_beads_changed",
    "reindex_project_in_rag",
    "resolve_beads_project",
    "set_beads_enabled",
]
