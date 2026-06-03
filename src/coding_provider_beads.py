"""Beads provider tool — first-class, capability-gated agent access to `bd`.

The lowest-friction layer is the raw ``bd`` CLI on the agent's PATH; this is the
*structured* layer that the UI and peer agents can call without scraping terminal
output. It mirrors the sibling provider tools (memory/agent/task): resolve the
owner-scoped project → repo ``root_path`` → :class:`BeadsService`, gated by
``beads.read`` / ``beads.write``. Writes announce themselves on the thread event
stream and re-enrich RAG via the bridge.
"""

from __future__ import annotations

import asyncio
from typing import Any

from src.coding_beads import BeadsError, get_beads_service
from src.coding_beads_bridge import emit_beads_changed, resolve_beads_project
from src.coding_provider_tokens import (
    CodingProviderError,
    ProviderContext,
    require_capability,
)
from src.coding_provider_tool_common import int_arg, str_arg

_READ_ACTIONS = {"list", "ready", "show", "status", "graph"}
_WRITE_ACTIONS = {"create", "update", "close", "dep", "dep_remove"}


async def handle_beads_tool(
    context: ProviderContext,
    action: str,
    args: dict[str, Any],
    services,
) -> dict[str, Any]:
    del services
    return await call_beads_tool(context, action, args)


async def call_beads_tool(context: ProviderContext, action: str, args: dict[str, Any]) -> dict[str, Any]:
    action = (action or "").strip().lower().replace("-", "_")
    # Friendly aliases.
    action = {
        "get": "show", "summary": "status", "create_dep": "dep", "add_dep": "dep",
        "remove_dep": "dep_remove", "undep": "dep_remove", "del_dep": "dep_remove",
    }.get(action, action)

    if action not in _READ_ACTIONS and action not in _WRITE_ACTIONS:
        raise CodingProviderError(400, f"Unknown beads action: {action}")

    project = _resolve_project(context)
    root_path = project["root_path"]
    service = get_beads_service()

    try:
        if action in _READ_ACTIONS:
            require_capability(context, "beads.read")
            return await _handle_read(service, root_path, action, args)

        require_capability(context, "beads.write")
        result = await _handle_write(service, root_path, action, args)
        # Announce the change on the thread stream + re-enrich RAG (best-effort).
        await emit_beads_changed(
            context.owner,
            context.project_id,
            root_path,
            thread_id=context.thread_id,
            run_id=context.run_id,
        )
        return result
    except BeadsError as exc:
        raise CodingProviderError(exc.status_code, exc.detail) from exc


def _resolve_project(context: ProviderContext) -> dict[str, Any]:
    if not context.project_id:
        raise CodingProviderError(400, "This token is not scoped to a project")
    try:
        return resolve_beads_project(context.owner, context.project_id)
    except BeadsError as exc:
        raise CodingProviderError(exc.status_code, exc.detail) from exc


async def _handle_read(service, root_path: str, action: str, args: dict[str, Any]) -> dict[str, Any]:
    if action == "list":
        include_closed = bool(args.get("include_closed") or args.get("all"))
        limit = int_arg(args.get("limit"), 200)
        issues = await asyncio.to_thread(
            service.list_issues, root_path, include_closed=include_closed, limit=limit
        )
        return {"issues": issues, "count": len(issues)}
    if action == "ready":
        limit = int_arg(args.get("limit"), 100)
        issues = await asyncio.to_thread(service.ready, root_path, limit=limit)
        return {"ready": issues, "count": len(issues)}
    if action == "show":
        issue_id = str_arg(args, "issue_id") or str_arg(args, "id")
        if not issue_id:
            raise CodingProviderError(400, "issue_id is required")
        issue = await asyncio.to_thread(service.show, root_path, issue_id)
        return {"issue": issue}
    if action == "status":
        summary = await asyncio.to_thread(service.summary, root_path)
        return {"summary": summary}
    if action == "graph":
        graph = await asyncio.to_thread(service.graph, root_path)
        return {"graph": graph}
    raise CodingProviderError(400, f"Unknown beads action: {action}")


async def _handle_write(service, root_path: str, action: str, args: dict[str, Any]) -> dict[str, Any]:
    if action == "create":
        title = str_arg(args, "title")
        if not title:
            raise CodingProviderError(400, "title is required")
        priority = args.get("priority")
        issue = await asyncio.to_thread(
            service.create,
            root_path,
            title,
            issue_type=str_arg(args, "issue_type"),
            priority=(int_arg(priority) if priority is not None else None),
            description=str_arg(args, "description"),
            discovered_from=str_arg(args, "discovered_from"),
        )
        return {"issue": issue}
    if action == "update":
        issue_id = str_arg(args, "issue_id") or str_arg(args, "id")
        if not issue_id:
            raise CodingProviderError(400, "issue_id is required")
        priority = args.get("priority")
        issue = await asyncio.to_thread(
            service.update,
            root_path,
            issue_id,
            status=str_arg(args, "status"),
            priority=(int_arg(priority) if priority is not None else None),
            title=str_arg(args, "title"),
        )
        return {"issue": issue}
    if action == "close":
        ids = args.get("issue_ids")
        if not ids:
            single = str_arg(args, "issue_id") or str_arg(args, "id")
            ids = [single] if single else []
        if isinstance(ids, str):
            ids = [ids]
        result = await asyncio.to_thread(
            service.close, root_path, list(ids), reason=str_arg(args, "reason")
        )
        return result
    if action == "dep":
        blocked = str_arg(args, "blocked_id") or str_arg(args, "blocked")
        blocker = str_arg(args, "blocker_id") or str_arg(args, "blocker")
        if not blocked or not blocker:
            raise CodingProviderError(400, "blocked_id and blocker_id are required")
        result = await asyncio.to_thread(
            service.dep_add,
            root_path,
            blocked,
            blocker,
            dep_type=(str_arg(args, "type") or "blocks"),
        )
        return result
    if action == "dep_remove":
        blocked = str_arg(args, "blocked_id") or str_arg(args, "blocked")
        blocker = str_arg(args, "blocker_id") or str_arg(args, "blocker")
        if not blocked or not blocker:
            raise CodingProviderError(400, "blocked_id and blocker_id are required")
        result = await asyncio.to_thread(service.dep_remove, root_path, blocked, blocker)
        return result
    raise CodingProviderError(400, f"Unknown beads action: {action}")


__all__ = ["call_beads_tool", "handle_beads_tool"]
