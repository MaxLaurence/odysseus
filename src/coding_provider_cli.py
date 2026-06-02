"""Provider-tool HTTP client CLI for coding harness runs."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
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


def _gate_store_path(store: str | None) -> str:
    if store:
        return store
    run_id = (os.environ.get("ODYSSEUS_RUN_ID", "") or "default").replace("/", "_")
    return os.path.join(tempfile.gettempdir(), f"odysseus-task-slot-{run_id}")


def _gate_payload(action: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool": "task",
        "action": action,
        "args": args,
        "arguments": args,
        "thread_id": os.environ.get("ODYSSEUS_THREAD_ID", ""),
        "project_id": os.environ.get("ODYSSEUS_PROJECT_ID", ""),
        "run_id": os.environ.get("ODYSSEUS_RUN_ID", ""),
    }


def cmd_gate(args: argparse.Namespace) -> None:
    """Block a harness hook on a per-endpoint task slot.

    `acquire` long-polls the task tool until a slot is granted (then records it),
    or gives up after --max-wait and proceeds ungated (best-effort: never wedge the
    agent). `release` frees the recorded slot. Emits nothing on stdout so harness
    hook parsers see a clean "proceed" (no decision).
    """
    store = _gate_store_path(args.store)
    url = _tool_call_url()
    if args.gate_action == "acquire":
        deadline = time.monotonic() + max(1.0, float(args.max_wait))
        while time.monotonic() < deadline:
            try:
                resp = _request(url, _gate_payload("acquire", {"wait_seconds": 15}))
            except SystemExit:
                return  # bridge unreachable → proceed without gating
            result = (resp or {}).get("result") or resp or {}
            if result.get("granted") and result.get("slot_id"):
                try:
                    with open(store, "w", encoding="utf-8") as handle:
                        handle.write(str(result["slot_id"]))
                except OSError:
                    pass
                return
            # queued → loop and re-acquire (the server already blocked ~15s)
        return  # timed out waiting → proceed ungated
    if args.gate_action == "release":
        try:
            with open(store, "r", encoding="utf-8") as handle:
                slot_id = handle.read().strip()
        except OSError:
            slot_id = ""
        if slot_id:
            try:
                _request(url, _gate_payload("release", {"slot_id": slot_id}))
            except SystemExit:
                pass
        try:
            os.remove(store)
        except OSError:
            pass
        return


def cmd_state(args: argparse.Namespace) -> None:
    """Report this run's semantic agent state from a harness hook.

    POSTs ``{"tool":"agent","action":"state","args":{"state":<state>}}`` to the
    provider bridge. Best-effort like ``gate``: if the bridge is unreachable it
    proceeds silently. Emits nothing on stdout so harness hook parsers see a
    clean "proceed" (no decision).
    """
    payload = {
        "tool": "agent",
        "action": "state",
        "args": {"state": args.agent_state},
        "arguments": {"state": args.agent_state},
        "thread_id": os.environ.get("ODYSSEUS_THREAD_ID", ""),
        "project_id": os.environ.get("ODYSSEUS_PROJECT_ID", ""),
        "run_id": os.environ.get("ODYSSEUS_RUN_ID", ""),
    }
    try:
        _request(_tool_call_url(), payload)
    except SystemExit:
        return  # bridge unreachable / unauth → proceed without reporting state


MCP_SERVER_NAME = "odysseus-provider-tools"


def _mcp_json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _mcp_error_text(message: str) -> str:
    return _mcp_json_text({"ok": False, "error": message})


def build_mcp_server() -> Any:
    """Build the Codex-only stdio MCP server (kept intact — Codex consumes tools
    via MCP, not a JS extension).

    NOTE: This is now a *thin adapter*. Its two tools (``odysseus_provider`` /
    ``odysseus_list_tools``) just forward to the run-scoped provider bridge
    (``POST /api/coding/provider/tool``), exactly like the ``ody`` CLI and the
    Pi/OMP extension tools. The primary agent interface for driving Code Station
    (spaces/tabs/panes/agents + ``agent report-state``) is the ``ody`` CLI on the
    pane's PATH; this MCP surface exists only because Codex speaks MCP. Do not add
    new behavior here — extend the provider bridge / ``ody`` instead.
    """
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

    gate_cmd = sub.add_parser(
        "gate", parents=[common], help="Acquire/release a per-endpoint task slot (for harness hooks)"
    )
    gate_cmd.add_argument("gate_action", choices=["acquire", "release"])
    gate_cmd.add_argument("--store", default="", help="Slot-id store file (default: temp file keyed by ODYSSEUS_RUN_ID)")
    gate_cmd.add_argument("--max-wait", type=float, default=28.0, help="Max seconds to block on acquire before proceeding ungated")
    gate_cmd.set_defaults(func=cmd_gate)

    state_cmd = sub.add_parser(
        "state", parents=[common], help="Report this run's semantic agent state (for harness hooks)"
    )
    state_cmd.add_argument("agent_state", choices=["working", "blocked", "idle", "done", "unknown"])
    state_cmd.set_defaults(func=cmd_state)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
