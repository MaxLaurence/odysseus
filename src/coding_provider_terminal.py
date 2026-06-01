"""Terminal provider tool handlers."""

from __future__ import annotations

from typing import Any

from core.database import CodingRun, CodingThread
from src.coding_provider_tokens import (
    CREDENTIAL_CLASS_RUN,
    CodingProviderError,
    ProviderContext,
    require_capability,
)
from src.coding_provider_tool_common import int_arg, manage_coding, session_local


async def handle_terminal_tool(
    context: ProviderContext,
    action: str,
    args: dict[str, Any],
    services,
) -> dict[str, Any]:
    del services
    return await call_terminal_tool(context, action, args)


async def call_terminal_tool(context: ProviderContext, action: str, args: dict[str, Any]) -> dict[str, Any]:
    action = {
        "get": "read",
        "get_run": "read",
        "read_run": "read",
        "write": "stdin",
        "send": "stdin",
        "send_stdin": "stdin",
        "input": "stdin",
        "run": "start",
        "run_thread": "start",
    }.get(action or "read", action or "read")
    if action == "read":
        require_capability(context, "terminal.read")
        run_id = run_id_for_terminal_action(context, args)
        log_chars = min(max(int_arg(args.get("log_chars"), 12_000), 0), 50_000)
        return await manage_coding(
            "read_run",
            context.owner,
            {
                "run_id": run_id,
                "after_seq": int_arg(args.get("after_seq"), 0),
                "log_chars": log_chars,
                "include_events": bool(args.get("include_events", True)),
            },
        )
    if action == "start":
        if context.credential_class == CREDENTIAL_CLASS_RUN:
            raise CodingProviderError(403, "Run-scoped provider credentials cannot start terminal runs")
        require_capability(context, "terminal.start")
        scoped_args = {
            key: value
            for key, value in args.items()
            if key
            in {
                "command",
                "cwd",
                "harness_id",
                "model_endpoint_id",
                "model",
                "replace",
                "idempotency_key",
                "metadata",
                "cols",
                "rows",
            }
        }
        scoped_args["thread_id"] = context.thread_id
        return await manage_coding("run_thread", context.owner, scoped_args)
    if action == "stdin":
        require_capability(context, "terminal.stdin")
        if args.get("data") is None:
            raise CodingProviderError(400, "data is required")
        run_id = run_id_for_terminal_action(context, args)
        return await manage_coding("send_stdin", context.owner, {"run_id": run_id, "data": str(args.get("data"))})
    if action == "resize":
        require_capability(context, "terminal.resize")
        cols = int_arg(args.get("cols"), 0)
        rows = int_arg(args.get("rows"), 0)
        if cols <= 0 or rows <= 0:
            raise CodingProviderError(400, "cols and rows must be positive")
        run_id = run_id_for_terminal_action(context, args)
        return await manage_coding("resize_run", context.owner, {"run_id": run_id, "cols": cols, "rows": rows})
    if action == "stop":
        require_capability(context, "terminal.stop")
        run_id = run_id_for_terminal_action(context, args)
        return await manage_coding(
            "stop_run",
            context.owner,
            {"run_id": run_id, "reason": args.get("reason") or "provider stopped"},
        )
    raise CodingProviderError(400, "Unknown terminal action")


def run_id_for_terminal_action(context: ProviderContext, args: dict[str, Any], *, required: bool = True) -> str:
    if context.credential_class == CREDENTIAL_CLASS_RUN:
        expected_run_id = str(context.run_id or "").strip()
        if not expected_run_id:
            if required:
                raise CodingProviderError(403, "Run-scoped provider credential is missing run_id")
            return ""
        if "run_id" in args and str(args.get("run_id") or "").strip() != expected_run_id:
            raise CodingProviderError(
                403,
                "Run-scoped provider credentials cannot access a different terminal run",
            )
        run_id = expected_run_id
    else:
        run_id = str(args.get("run_id") or "").strip()
    db = session_local()()
    try:
        if not run_id:
            thread = (
                db.query(CodingThread)
                .filter(CodingThread.id == context.thread_id, CodingThread.owner == context.owner)
                .first()
            )
            run_id = (thread.last_run_id if thread else None) or ""
        if not run_id:
            if required:
                raise CodingProviderError(400, "run_id is required")
            return ""
        run = (
            db.query(CodingRun)
            .filter(CodingRun.id == run_id, CodingRun.owner == context.owner)
            .first()
        )
        if not run or run.thread_id != context.thread_id:
            raise CodingProviderError(404, "Run not found")
        return run.id
    finally:
        db.close()


__all__ = [
    "call_terminal_tool",
    "handle_terminal_tool",
    "run_id_for_terminal_action",
]
