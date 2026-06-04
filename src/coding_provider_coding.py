"""Coding thread and model-config provider tool handlers."""

from __future__ import annotations

from typing import Any

from src.coding_provider_tokens import CodingProviderError, ProviderContext, require_capability
from src.coding_provider_tool_common import manage_coding


async def handle_coding_tool(
    context: ProviderContext,
    action: str,
    args: dict[str, Any],
    services,
) -> dict[str, Any]:
    del services
    return await call_coding_tool(context, action, args)


async def handle_model_config_tool(
    context: ProviderContext,
    action: str,
    args: dict[str, Any],
    services,
) -> dict[str, Any]:
    del services
    return await call_model_config_tool(context, action, args)


async def call_coding_tool(context: ProviderContext, action: str, args: dict[str, Any]) -> dict[str, Any]:
    action = {
        "thread": "get_thread",
        "read": "read_thread",
        "project": "get_project",
        "update": "update_thread",
        "patch_thread": "update_thread",
    }.get(action or "get_thread", action or "get_thread")
    if action in {"get_thread", "read_thread"}:
        require_capability(context, "coding.read")
        scoped_args = dict(args)
        scoped_args["thread_id"] = context.thread_id
        return await manage_coding(action, context.owner, scoped_args)
    if action == "get_project":
        require_capability(context, "coding.read")
        return await manage_coding("get_project", context.owner, {"project_id": context.project_id})
    if action == "update_thread":
        require_capability(context, "coding.write")
        scoped_args = {
            key: value
            for key, value in args.items()
            if key in {"title", "cwd", "harness_id", "model_endpoint_id", "model", "effort", "auth_mode", "provider", "metadata", "status"}
        }
        scoped_args["thread_id"] = context.thread_id
        return await manage_coding("update_thread", context.owner, scoped_args)
    raise CodingProviderError(400, "Unknown coding action")


async def call_model_config_tool(context: ProviderContext, action: str, args: dict[str, Any]) -> dict[str, Any]:
    del args
    action = {
        "get": "read",
        "model_config": "read",
        "derive_config": "derive",
        "restore_config": "restore",
    }.get(action or "read", action or "read")
    if action == "read":
        require_capability(context, "model_config.read")
        return await manage_coding("model_config", context.owner, {"thread_id": context.thread_id})
    if action == "derive":
        require_capability(context, "model_config.derive")
        return await manage_coding("derive_model_config", context.owner, {"thread_id": context.thread_id})
    if action == "restore":
        require_capability(context, "model_config.restore")
        return await manage_coding("restore_config", context.owner, {"thread_id": context.thread_id})
    raise CodingProviderError(400, "Unknown model_config action")


__all__ = [
    "call_coding_tool",
    "call_model_config_tool",
    "handle_coding_tool",
    "handle_model_config_tool",
]
