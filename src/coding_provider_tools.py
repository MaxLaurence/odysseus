"""Provider tool catalog and dispatcher for Coding Station provider tokens."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from src.coding_provider_coding import (
    call_coding_tool as _call_coding_tool,
    call_model_config_tool as _call_model_config_tool,
    handle_coding_tool as _handle_coding_tool,
    handle_model_config_tool as _handle_model_config_tool,
)
from src.coding_provider_memory import call_memory_tool as _call_memory_tool
from src.coding_provider_memory import handle_memory_tool as _handle_memory_tool
from src.coding_provider_terminal import (
    call_terminal_tool as _call_terminal_tool,
    handle_terminal_tool as _handle_terminal_tool,
    run_id_for_terminal_action as _run_id_for_terminal_action,
)
from src.coding_provider_thread_messages import (
    call_thread_messages_tool as _call_thread_messages_tool,
    handle_thread_messages_tool as _handle_thread_messages_tool,
    message_dict as _message_dict,
    thread_session_id as _thread_session_id,
)
from src.coding_provider_task import (
    call_task_tool as _call_task_tool,
    handle_task_tool as _handle_task_tool,
)
from src.coding_provider_agent import (
    call_agent_tool as _call_agent_tool,
    handle_agent_tool as _handle_agent_tool,
)
from src.coding_provider_beads import (
    call_beads_tool as _call_beads_tool,
    handle_beads_tool as _handle_beads_tool,
)
from src.coding_provider_tool_common import (
    SessionLocal,
    int_arg as _int,
    manage_coding as _manage_coding,
    sanitize_result,
    session_local as _session_local,
)
from src.coding_provider_tokens import ALL_CAPABILITIES, CodingProviderError, ProviderContext

PROVIDER_TOOL_CATALOG = [
    {
        "tool": "provider",
        "actions": ["list", "capabilities"],
        "capabilities": [],
        "description": "Discover provider tools available to this thread-scoped token.",
    },
    {
        "tool": "memory",
        "actions": ["list", "get", "search", "add", "edit", "delete"],
        "capabilities": ["memory.read", "memory.write"],
        "description": "Read or write owner-scoped memories through the memory manager.",
    },
    {
        "tool": "coding",
        "actions": ["get_thread", "read_thread", "get_project", "update_thread"],
        "capabilities": ["coding.read", "coding.write"],
        "description": "Read or update the current coding thread through manage_coding.",
    },
    {
        "tool": "terminal",
        "actions": ["read", "start", "stdin", "resize", "stop"],
        "capabilities": [
            "terminal.read",
            "terminal.start",
            "terminal.stdin",
            "terminal.resize",
            "terminal.stop",
        ],
        "description": "Mediate current-thread terminal runs through the coding runtime.",
    },
    {
        "tool": "thread_messages",
        "actions": ["list", "add"],
        "capabilities": ["thread.messages.read", "thread.messages.write"],
        "description": "Read or append messages on the chat session linked to this coding thread.",
    },
    {
        "tool": "model_config",
        "actions": ["read", "derive", "restore"],
        "capabilities": ["model_config.read", "model_config.derive", "model_config.restore"],
        "description": "Read, derive, or restore thread model config via existing services.",
    },
    {
        "tool": "task",
        "actions": ["acquire", "release", "heartbeat"],
        "capabilities": ["task.acquire", "task.release", "task.heartbeat"],
        "description": "Acquire/release a per-model-endpoint LLM-call slot so concurrent LLM calls are throttled (blocks until a slot is free).",
    },
    {
        "tool": "agent",
        "actions": ["state"],
        "capabilities": ["agent.state"],
        "description": "Report this run's semantic agent state (working|blocked|idle|done|unknown) for the herdr rollup.",
    },
    {
        "tool": "beads",
        "actions": ["list", "ready", "show", "status", "graph", "create", "update", "close", "dep", "dep_remove"],
        "capabilities": ["beads.read", "beads.write"],
        "description": "Read or write this project's Beads (bd) issue backlog — the repo-scoped, dependency-aware source of truth for work. Use `ready` to find unblocked work, `create` to record discovered work, `close` as you finish.",
    },
]

TOOL_ALIASES = {
    "tools": "provider",
    "capabilities": "provider",
    "discover": "provider",
    "messages": "thread_messages",
    "model": "model_config",
    "bd": "beads",
    "issues": "beads",
}


@dataclass(frozen=True)
class ProviderToolServices:
    memory_manager: Any
    session_manager: Any
    memory_vector: Any = None


ProviderToolHandler = Callable[
    [ProviderContext, str, dict[str, Any], ProviderToolServices],
    Awaitable[dict[str, Any]],
]


def provider_capabilities_response(context: ProviderContext | None = None) -> dict[str, Any]:
    token_capabilities = sorted(context.capabilities) if context is not None else None
    token_capability_set = set(token_capabilities or [])
    tools = []
    for item in PROVIDER_TOOL_CATALOG:
        capabilities = list(item["capabilities"])
        exposed = {
            "tool": item["tool"],
            "actions": list(item["actions"]),
            "capabilities": capabilities,
            "description": item["description"],
        }
        if context is not None:
            exposed["enabled"] = not capabilities or any(cap in token_capability_set for cap in capabilities)
            exposed["available_capabilities"] = [cap for cap in capabilities if cap in token_capability_set]
        tools.append(exposed)

    response: dict[str, Any] = {
        "capabilities": sorted(ALL_CAPABILITIES),
        "tools": tools,
    }
    if token_capabilities is not None:
        response["token_capabilities"] = token_capabilities
    return response


def _sanitize_result(value: Any) -> Any:
    return sanitize_result(value)


def _normalize_tool_action(payload: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    tool = str(payload.get("tool") or "").strip().lower().replace("-", "_")
    action = str(payload.get("action") or "").strip().lower().replace("-", "_")
    if not tool and "." in action:
        tool, action = action.split(".", 1)
        tool = tool.replace("-", "_")
        action = action.replace("-", "_")
    if not tool:
        raise CodingProviderError(400, "tool is required")
    args = payload.get("args", payload.get("arguments", {}))
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise CodingProviderError(400, "args must be an object")
    return TOOL_ALIASES.get(tool, tool), action, args


async def call_provider_tool(
    context: ProviderContext,
    payload: dict[str, Any],
    *,
    memory_manager,
    session_manager,
    memory_vector=None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise CodingProviderError(400, "Tool payload must be an object")
    tool, action, args = _normalize_tool_action(payload)
    handler = TOOL_HANDLERS.get(tool)
    if handler is None:
        raise CodingProviderError(400, f"Unknown provider tool: {tool}")

    services = ProviderToolServices(
        memory_manager=memory_manager,
        session_manager=session_manager,
        memory_vector=memory_vector,
    )
    result = await handler(context, action, args, services)
    return {
        "ok": True,
        "thread_id": context.thread_id,
        "tool": tool,
        "action": action,
        "result": _sanitize_result(result),
    }


async def _handle_provider_tool(
    context: ProviderContext,
    action: str,
    args: dict[str, Any],
    services: ProviderToolServices,
) -> dict[str, Any]:
    del args, services
    action = action or "list"
    if action not in {"list", "capabilities", "tools", "discover"}:
        raise CodingProviderError(400, "Unknown provider action")
    return provider_capabilities_response(context)


TOOL_HANDLERS: dict[str, ProviderToolHandler] = {
    "provider": _handle_provider_tool,
    "memory": _handle_memory_tool,
    "coding": _handle_coding_tool,
    "terminal": _handle_terminal_tool,
    "thread_messages": _handle_thread_messages_tool,
    "model_config": _handle_model_config_tool,
    "task": _handle_task_tool,
    "agent": _handle_agent_tool,
    "beads": _handle_beads_tool,
}


__all__ = [
    "PROVIDER_TOOL_CATALOG",
    "ProviderToolServices",
    "TOOL_ALIASES",
    "TOOL_HANDLERS",
    "call_provider_tool",
    "provider_capabilities_response",
]
