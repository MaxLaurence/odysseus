"""Provider-tool HTTP client CLI for coding harness runs."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

from src.coding_provider_bridge import bridge_env_status, provider_tool_url


def _json_payload(value: str | None) -> Any:
    if not value:
        return {}
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON: {exc}") from exc


def _tool_call_url() -> str:
    return provider_tool_url()


def _request(url: str, payload: dict[str, Any] | None = None, method: str = "POST") -> Any:
    token = os.environ.get("ODYSSEUS_TOOL_TOKEN", "")
    if not token:
        raise SystemExit("ODYSSEUS_TOOL_TOKEN is not set")
    body = None
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Odysseus-Thread-ID": os.environ.get("ODYSSEUS_THREAD_ID", ""),
        "X-Odysseus-Project-ID": os.environ.get("ODYSSEUS_PROJECT_ID", ""),
        "X-Odysseus-Run-ID": os.environ.get("ODYSSEUS_RUN_ID", ""),
    }
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            if not raw:
                return {}
            ctype = resp.headers.get("content-type", "")
            if "json" in ctype:
                return json.loads(raw.decode("utf-8"))
            return {"response": raw.decode("utf-8", errors="replace")}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"provider tool request failed ({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"provider tool request failed: {exc.reason}") from exc


def _emit(obj: Any, pretty: bool = False) -> None:
    json.dump(obj, sys.stdout, indent=2 if pretty else None, ensure_ascii=False, default=str)
    sys.stdout.write("\n")


def cmd_env(args: argparse.Namespace) -> None:
    _emit(bridge_env_status(), args.pretty)


def cmd_list(args: argparse.Namespace) -> None:
    _emit(_request(_tool_call_url(), {"tool": "provider", "action": "list", "args": {}}), args.pretty)


def cmd_call(args: argparse.Namespace) -> None:
    tool = args.tool
    action = args.action or ""
    if not action and "." in tool:
        tool, action = tool.split(".", 1)
    arguments = _json_payload(args.json)
    payload = {
        "tool": tool,
        "action": action,
        "args": arguments,
        "arguments": arguments,
        "thread_id": os.environ.get("ODYSSEUS_THREAD_ID", ""),
        "project_id": os.environ.get("ODYSSEUS_PROJECT_ID", ""),
        "run_id": os.environ.get("ODYSSEUS_RUN_ID", ""),
    }
    _emit(_request(_tool_call_url(), payload), args.pretty)


MCP_SERVER_NAME = "odysseus-provider-tools"


def _mcp_json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _mcp_error_text(message: str) -> str:
    return _mcp_json_text({"ok": False, "error": message})


def build_mcp_server() -> Any:
    try:
        from mcp.server.fastmcp import FastMCP
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "mcp Python package is not installed; install Odysseus requirements or run from the Odysseus venv"
        ) from exc

    mcp = FastMCP(MCP_SERVER_NAME)

    @mcp.tool(
        name="odysseus_list_tools",
        title="Odysseus Tools",
        description="List Odysseus provider tools available to this run-scoped coding-agent token.",
    )
    def odysseus_list_tools() -> str:
        try:
            return _mcp_json_text(_request(_tool_call_url(), {"tool": "provider", "action": "list", "args": {}}))
        except SystemExit as exc:
            return _mcp_error_text(str(exc))
        except Exception as exc:
            return _mcp_error_text(str(exc))

    @mcp.tool(
        name="odysseus_provider",
        title="Odysseus Provider",
        description=(
            "Call an Odysseus provider tool. Use odysseus_list_tools first to discover "
            "available tool names and actions."
        ),
    )
    def odysseus_provider(tool: str, action: str, args: dict[str, Any] | None = None) -> str:
        tool = (tool or "").strip()
        action = (action or "").strip()
        if not tool or not action:
            return _mcp_error_text("tool and action are required")
        arguments = args if isinstance(args, dict) else {}
        payload = {
            "tool": tool,
            "action": action,
            "args": arguments,
            "arguments": arguments,
        }
        try:
            return _mcp_json_text(_request(_tool_call_url(), payload))
        except SystemExit as exc:
            return _mcp_error_text(str(exc))
        except Exception as exc:
            return _mcp_error_text(str(exc))

    return mcp


def cmd_mcp_server(_args: argparse.Namespace) -> None:
    print("Odysseus MCP provider server ready", file=sys.stderr, flush=True)
    build_mcp_server().run(transport="stdio")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    parser = argparse.ArgumentParser(prog="odysseus-tool", parents=[common])
    sub = parser.add_subparsers(dest="command_name", required=True)

    env_cmd = sub.add_parser("env", parents=[common], help="Show injected provider-tool environment")
    env_cmd.set_defaults(func=cmd_env)

    list_cmd = sub.add_parser("list", parents=[common], help="List provider tools")
    list_cmd.set_defaults(func=cmd_list)

    call_cmd = sub.add_parser("call", parents=[common], help="Call a provider tool")
    call_cmd.add_argument("tool")
    call_cmd.add_argument("--action", default="", help="Tool action, e.g. list/read/add")
    call_cmd.add_argument("--json", default="{}", help="JSON object arguments for the tool")
    call_cmd.set_defaults(func=cmd_call)

    mcp_cmd = sub.add_parser("mcp-server", parents=[common], help="Run Odysseus provider tools as a stdio MCP server")
    mcp_cmd.set_defaults(func=cmd_mcp_server)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
