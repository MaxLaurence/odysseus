"""Thread message provider tool handlers."""

from __future__ import annotations

from typing import Any

from core.database import CodingThread, Session as DbSession
from core.models import ChatMessage
from src.coding_provider_tokens import CodingProviderError, ProviderContext, require_capability
from src.coding_provider_tool_common import MAX_MESSAGE_TEXT, int_arg, session_local


async def handle_thread_messages_tool(
    context: ProviderContext,
    action: str,
    args: dict[str, Any],
    services,
) -> dict[str, Any]:
    return call_thread_messages_tool(context, action, args, services.session_manager)


def call_thread_messages_tool(
    context: ProviderContext,
    action: str,
    args: dict[str, Any],
    session_manager,
) -> dict[str, Any]:
    action = {
        "read": "list",
        "messages": "list",
        "append": "add",
        "write": "add",
        "create": "add",
    }.get(action or "list", action or "list")
    if action == "list":
        require_capability(context, "thread.messages.read")
        session_id = thread_session_id(context)
        try:
            session = session_manager.get_session(session_id)
        except KeyError as exc:
            raise CodingProviderError(404, "Linked chat session not found") from exc
        include_hidden = bool(args.get("include_hidden", False))
        limit = min(max(int_arg(args.get("limit"), 100), 1), 500)
        messages = []
        for message in session.history:
            item = message_dict(message)
            if not include_hidden and item.get("metadata", {}).get("hidden"):
                continue
            messages.append(item)
        return {"session_id": session_id, "messages": messages[-limit:], "count": len(messages)}
    if action == "add":
        require_capability(context, "thread.messages.write")
        session_id = thread_session_id(context)
        role = str(args.get("role") or "assistant").strip().lower()
        if role not in {"system", "user", "assistant", "tool"}:
            raise CodingProviderError(400, "role must be system, user, assistant, or tool")
        content = str(args.get("content") or "")
        if not content.strip():
            raise CodingProviderError(400, "content is required")
        if len(content) > MAX_MESSAGE_TEXT:
            raise CodingProviderError(400, f"content exceeds {MAX_MESSAGE_TEXT} characters")
        metadata = args.get("metadata") if isinstance(args.get("metadata"), dict) else {}
        metadata = dict(metadata or {})
        metadata.pop("coding_thread_id", None)
        metadata.pop("coding_project_id", None)
        metadata.setdefault("source", "coding_provider")
        metadata["coding_provider_token_id"] = context.token_id
        metadata["coding_thread_id"] = context.thread_id
        message = ChatMessage(role=role, content=content, metadata=metadata)
        try:
            session_manager.add_message(session_id, message)
        except KeyError as exc:
            raise CodingProviderError(404, "Linked chat session not found") from exc
        return {"session_id": session_id, "message": message.to_dict()}
    raise CodingProviderError(400, "Unknown thread messages action")


def thread_session_id(context: ProviderContext) -> str:
    db = session_local()()
    try:
        thread = (
            db.query(CodingThread)
            .filter(CodingThread.id == context.thread_id, CodingThread.owner == context.owner)
            .first()
        )
        if not thread or not thread.session_id:
            raise CodingProviderError(404, "Coding thread has no linked chat session")
        session = db.query(DbSession).filter(DbSession.id == thread.session_id).first()
        if not session or (context.owner and session.owner != context.owner):
            raise CodingProviderError(404, "Linked chat session not found")
        return thread.session_id
    finally:
        db.close()


def message_dict(message: ChatMessage | dict[str, Any]) -> dict[str, Any]:
    if isinstance(message, ChatMessage):
        item = {"role": message.role, "content": message.content}
        if message.metadata:
            item["metadata"] = dict(message.metadata)
        return item
    item = {"role": message.get("role", ""), "content": message.get("content", "")}
    if message.get("metadata"):
        item["metadata"] = dict(message["metadata"])
    return item


__all__ = [
    "call_thread_messages_tool",
    "handle_thread_messages_tool",
    "message_dict",
    "thread_session_id",
]
