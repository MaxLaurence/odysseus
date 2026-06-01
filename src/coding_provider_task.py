"""Task-slot provider tool: harnesses acquire/release a per-endpoint LLM-call slot.

A coding harness's hook calls this (via odysseus-tool / the provider HTTP route)
right before an LLM call (``acquire``) and after it (``release``), with optional
``heartbeat`` for long calls. The slot pool is per model endpoint, so different
models don't block each other. See src/coding_task_slots.py.
"""

from __future__ import annotations

import json
from typing import Any

from core.database import CodingRun, CodingThread
from src.coding_provider_tokens import (
    CodingProviderError,
    ProviderContext,
    require_capability,
)
from src.coding_provider_tool_common import int_arg, session_local
from src.coding_task_slots import DEFAULT_ACQUIRE_WAIT_SECONDS, get_task_slot_service

# Cap the server-side long-poll so it stays under the tightest harness hook
# timeout (Claude UserPromptSubmit = 30s); the hook re-acquires on "queued".
_MAX_ACQUIRE_WAIT_SECONDS = 25.0


def _safe_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _endpoint_key_for_context(context: ProviderContext) -> str:
    """Resolve the model-endpoint key a run's LLM calls should be limited under.

    Prefers the run's launched endpoint (stored in run metadata at enqueue),
    then the thread's configured endpoint, then its model name, else "default".
    """
    db = session_local()()
    try:
        run_id = (context.run_id or "").strip()
        if run_id:
            run = (
                db.query(CodingRun)
                .filter(CodingRun.id == run_id, CodingRun.owner == context.owner)
                .first()
            )
            if run is not None:
                endpoint_id = (
                    (_safe_json(run.metadata_json).get("model_config") or {}).get("endpoint_id") or ""
                ).strip()
                if endpoint_id:
                    return endpoint_id
        thread = (
            db.query(CodingThread)
            .filter(CodingThread.id == context.thread_id, CodingThread.owner == context.owner)
            .first()
        )
        if thread is not None:
            if (thread.model_endpoint_id or "").strip():
                return thread.model_endpoint_id.strip()
            if (thread.model or "").strip():
                return f"model:{thread.model.strip()}"
    finally:
        db.close()
    return "default"


def _run_identity(context: ProviderContext) -> str:
    # The slot holder's run id must match what the runtime passes to
    # release_run() on teardown, so prefer the real run id.
    return (context.run_id or "").strip() or (context.thread_id or "").strip()


async def handle_task_tool(
    context: ProviderContext,
    action: str,
    args: dict[str, Any],
    services,
) -> dict[str, Any]:
    del services
    return await call_task_tool(context, action, args)


async def call_task_tool(context: ProviderContext, action: str, args: dict[str, Any]) -> dict[str, Any]:
    action = (action or "").strip().lower()
    service = get_task_slot_service()

    if action == "acquire":
        require_capability(context, "task.acquire")
        run_id = _run_identity(context)
        if not run_id:
            raise CodingProviderError(400, "task.acquire requires a run-scoped credential")
        endpoint_key = _endpoint_key_for_context(context)
        wait = args.get("wait_seconds", args.get("wait"))
        wait_seconds = float(int_arg(wait, int(DEFAULT_ACQUIRE_WAIT_SECONDS))) if wait is not None else DEFAULT_ACQUIRE_WAIT_SECONDS
        wait_seconds = max(0.5, min(wait_seconds, _MAX_ACQUIRE_WAIT_SECONDS))
        return await service.acquire(
            owner=context.owner,
            run_id=run_id,
            endpoint_key=endpoint_key,
            wait_seconds=wait_seconds,
        )

    if action == "release":
        require_capability(context, "task.release")
        slot_id = str(args.get("slot_id") or "").strip()
        if slot_id:
            return service.release(slot_id)
        # No slot id → release everything this run holds (safety / coarse release).
        service.release_run(_run_identity(context))
        return {"released": True, "scope": "run"}

    if action == "heartbeat":
        require_capability(context, "task.heartbeat")
        slot_id = str(args.get("slot_id") or "").strip()
        if not slot_id:
            raise CodingProviderError(400, "task.heartbeat requires slot_id")
        return service.heartbeat(slot_id)

    raise CodingProviderError(400, "Unknown task action")


__all__ = ["call_task_tool", "handle_task_tool"]
