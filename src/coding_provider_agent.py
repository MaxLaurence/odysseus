"""Agent-state provider tool: harnesses report their semantic agent state.

A coding harness's hook (or the agent itself) calls this — via odysseus-tool /
the provider HTTP route — to report whether it is ``working``, ``blocked``,
``idle``, ``done``, or ``unknown``. The state is persisted on
``coding_threads.agent_state`` and broadcast as a ``CodingThreadEvent`` of kind
``agent_state_changed`` so every SSE/socket subscriber sees the herdr rollup.

This rides the same run-scoped HTTP bridge (auth/transport) the task-slot hooks
already use; it does not invent new auth. Run tokens get ``capabilities="all"``
so they automatically gain ``agent.state``. See src/coding_workspace.py for the
``set_agent_state`` contract and src/coding_provider_task.py for the sibling
``task`` tool this mirrors.
"""

from __future__ import annotations

from typing import Any

from src.coding_provider_tokens import (
    CodingProviderError,
    ProviderContext,
    require_capability,
)
from src.coding_workspace import get_coding_workspace_service

# The valid semantic states, mirrored from the workspace/CLI contract.
_AGENT_STATES = {"working", "blocked", "idle", "done", "unknown"}


async def handle_agent_tool(
    context: ProviderContext,
    action: str,
    args: dict[str, Any],
    services,
) -> dict[str, Any]:
    del services
    return await call_agent_tool(context, action, args)


async def call_agent_tool(context: ProviderContext, action: str, args: dict[str, Any]) -> dict[str, Any]:
    action = (action or "").strip().lower()

    # Accept both "state" and "report_state" so hook ergonomics stay flexible.
    if action in {"state", "report_state"}:
        require_capability(context, "agent.state")
        state = str(args.get("state") or "").strip().lower()
        if state not in _AGENT_STATES:
            raise CodingProviderError(400, f"Invalid agent state: {state}")
        await get_coding_workspace_service().set_agent_state(
            context.owner,
            context.thread_id,
            state,
            run_id=context.run_id,
            message=args.get("message"),
        )
        return {"ok": True, "state": state}

    raise CodingProviderError(400, "Unknown agent action")


__all__ = ["call_agent_tool", "handle_agent_tool"]
