"""Memory provider tool handlers."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from src.coding_provider_tokens import (
    CodingProviderError,
    ProviderContext,
    memory_owner,
    require_capability,
)
from src.coding_provider_tool_common import MAX_MEMORY_TEXT, int_arg


async def handle_memory_tool(
    context: ProviderContext,
    action: str,
    args: dict[str, Any],
    services,
) -> dict[str, Any]:
    return call_memory_tool(
        context,
        action,
        args,
        services.memory_manager,
        memory_vector=services.memory_vector,
    )


def call_memory_tool(
    context: ProviderContext,
    action: str,
    args: dict[str, Any],
    memory_manager,
    memory_vector=None,
) -> dict[str, Any]:
    action = {
        "read": "list",
        "create": "add",
        "update": "edit",
        "remove": "delete",
    }.get(action or "list", action or "list")
    owner = context.owner
    scoped_owner = memory_owner(owner)

    if action in {"list", "get", "search"}:
        require_capability(context, "memory.read")
        memories = memory_manager.load(owner=scoped_owner)
        if action == "list":
            category = str(args.get("category") or "").strip().lower()
            if category:
                memories = [m for m in memories if str(m.get("category", "")).lower() == category]
            limit = min(max(int_arg(args.get("limit"), 100), 1), 500)
            return {"memories": [_memory_entry(m) for m in memories[:limit]], "count": len(memories)}
        if action == "get":
            memory = _find_memory(memories, str(args.get("memory_id") or ""), owner)
            if not memory:
                raise CodingProviderError(404, "Memory not found")
            return {"memory": _memory_entry(memory)}
        query = str(args.get("query") or "").strip()
        if not query:
            raise CodingProviderError(400, "query is required")
        if hasattr(memory_manager, "get_relevant_memories"):
            results = memory_manager.get_relevant_memories(query, memories, threshold=0.05, max_items=20)
        else:
            query_lower = query.lower()
            results = [m for m in memories if query_lower in str(m.get("text", "")).lower()][:20]
        return {"memories": [_memory_entry(m) for m in results], "count": len(results)}

    if action in {"add", "edit", "delete"}:
        require_capability(context, "memory.write")
        all_memories = memory_manager.load_all()
        if action == "add":
            text = str(args.get("text") or "").strip()
            if not text:
                raise CodingProviderError(400, "text is required")
            if len(text) > MAX_MEMORY_TEXT:
                raise CodingProviderError(400, f"text exceeds {MAX_MEMORY_TEXT} characters")
            category = str(args.get("category") or "fact").strip().lower() or "fact"
            session_id = str(args.get("session_id") or context.session_id or "").strip() or None
            if session_id and session_id != context.session_id:
                raise CodingProviderError(403, "memory session_id must match this coding thread")
            entry = memory_manager.add_entry(text, source="coding_provider", category=category, owner=scoped_owner)
            if session_id:
                entry["session_id"] = session_id
            all_memories.append(entry)
            memory_manager.save(all_memories)
            if memory_vector and getattr(memory_vector, "healthy", False):
                try:
                    memory_vector.add(entry["id"], text)
                except Exception:
                    pass
            _fire_memory_added(owner)
            return {"memory": _memory_entry(entry)}

        memory = _find_memory(all_memories, str(args.get("memory_id") or ""), owner)
        if not memory:
            raise CodingProviderError(404, "Memory not found")
        if action == "edit":
            text = str(args.get("text") or "").strip()
            if not text:
                raise CodingProviderError(400, "text is required")
            if len(text) > MAX_MEMORY_TEXT:
                raise CodingProviderError(400, f"text exceeds {MAX_MEMORY_TEXT} characters")
            memory["text"] = text
            if args.get("category") is not None:
                memory["category"] = str(args.get("category") or "fact").strip().lower() or "fact"
            memory["timestamp"] = int(datetime.utcnow().timestamp())
            memory_manager.save(all_memories)
            if memory_vector and getattr(memory_vector, "healthy", False):
                try:
                    memory_vector.add(memory["id"], text)
                except Exception:
                    pass
            return {"memory": _memory_entry(memory)}

        memory_id = memory.get("id")
        remaining = [m for m in all_memories if m.get("id") != memory_id]
        memory_manager.save(remaining)
        if memory_vector and memory_id and getattr(memory_vector, "healthy", False):
            try:
                memory_vector.remove(memory_id)
            except Exception:
                pass
        return {"deleted": memory_id}

    raise CodingProviderError(400, "Unknown memory action")


def _memory_entry(memory: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": memory.get("id"),
        "text": memory.get("text", ""),
        "category": memory.get("category", "fact"),
        "source": memory.get("source", "unknown"),
        "session_id": memory.get("session_id"),
        "timestamp": memory.get("timestamp"),
        "uses": memory.get("uses", 0),
    }


def _find_memory(memories: list[dict[str, Any]], memory_id: str, owner: str) -> dict[str, Any] | None:
    memory_id = (memory_id or "").strip()
    if not memory_id:
        return None
    for memory in memories:
        if not str(memory.get("id", "")).startswith(memory_id):
            continue
        if owner and memory.get("owner") != owner:
            return None
        return memory
    return None


def _fire_memory_added(owner: str) -> None:
    try:
        from src.event_bus import fire_event

        fire_event("memory_added", owner or None)
    except Exception:
        pass


__all__ = [
    "call_memory_tool",
    "handle_memory_tool",
]
