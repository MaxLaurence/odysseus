"""``ody`` — thin client CLI for the Code Station Unix-socket JSON-RPC server.

Connects to the ody socket (``$ODYSSEUS_ODY_SOCKET`` or ``<data_dir>/ody.sock``),
sends one newline-delimited JSON-RPC request, and prints the ``result`` as pretty
JSON. ``events subscribe`` instead streams ``event`` frames until interrupted.

Lets an agent inside a Code Station pane drive its own layout::

    ody ping
    ody space list
    ody tab create --space <id> --label build
    ody pane split <pane_id> --tab <tab_id> --direction right
    ody agent start --space <id> --harness claude --command "claude"
    ody agent report-state <agent_id> --state working --message "compiling"
    ody events subscribe --thread <thread_id>

Run via ``python -m src.coding_cli`` or the ``scripts/ody`` shim.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from typing import Any

logger = logging.getLogger(__name__)


def _socket_path() -> str:
    explicit = os.environ.get("ODYSSEUS_ODY_SOCKET", "").strip()
    if explicit:
        return explicit
    # Resolve the same way the server does, without importing the whole runtime.
    try:
        from core.constants import DATA_DIR

        base = (DATA_DIR or "").strip()
    except Exception:
        base = ""
    if not base:
        base = os.path.join(os.getcwd(), "data")
    return os.path.join(base, "ody.sock")


async def _open_connection() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    path = _socket_path()
    try:
        return await asyncio.open_unix_connection(path=path)
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        raise SystemExit(f"ody socket not available at {path}: {exc}") from exc
    except OSError as exc:
        raise SystemExit(f"could not connect to ody socket at {path}: {exc}") from exc


def _emit(obj: Any) -> None:
    json.dump(obj, sys.stdout, indent=2, ensure_ascii=False, default=str)
    sys.stdout.write("\n")
    sys.stdout.flush()


async def _request(method: str, params: dict[str, Any]) -> int:
    """Send one request, print result (or error), return an exit code."""
    reader, writer = await _open_connection()
    req_id = uuid.uuid4().hex
    payload = {"id": req_id, "method": method, "params": params}
    try:
        writer.write((json.dumps(payload) + "\n").encode("utf-8"))
        await writer.drain()
        line = await reader.readline()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    if not line:
        print("ody: no response from server", file=sys.stderr)
        return 1
    try:
        response = json.loads(line.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        print(f"ody: invalid response: {exc}", file=sys.stderr)
        return 1
    if "error" in response:
        err = response["error"] or {}
        print(f"ody: {err.get('code', 'error')}: {err.get('message', '')}", file=sys.stderr)
        return 1
    _emit(response.get("result"))
    return 0


async def _subscribe(method: str, params: dict[str, Any]) -> int:
    """Stream ``event`` frames until the server ends the stream or we're interrupted."""
    reader, writer = await _open_connection()
    req_id = uuid.uuid4().hex
    payload = {"id": req_id, "method": method, "params": params}
    writer.write((json.dumps(payload) + "\n").encode("utf-8"))
    await writer.drain()
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if "error" in frame:
                err = frame["error"] or {}
                print(f"ody: {err.get('code', 'error')}: {err.get('message', '')}", file=sys.stderr)
                return 1
            if "event" in frame:
                _emit(frame["event"])
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    return 0


def _params(args: argparse.Namespace, *names: str) -> dict[str, Any]:
    """Collect non-None attrs by name into a params dict, plus owner if set."""
    out: dict[str, Any] = {}
    for name in names:
        value = getattr(args, name, None)
        if value is not None:
            out[name] = value
    owner = getattr(args, "owner", None)
    if owner:
        out["owner"] = owner
    return out


# ----------------------------------------------------------- command handlers


def _run(args: argparse.Namespace, method: str, params: dict[str, Any]) -> int:
    return asyncio.run(_request(method, params))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ody", description="Code Station control-plane CLI")
    parser.add_argument("--owner", default=os.environ.get("ODYSSEUS_OWNER") or None,
                        help="Owner scope (default: $ODYSSEUS_OWNER)")
    sub = parser.add_subparsers(dest="resource", required=True)

    # ping
    p_ping = sub.add_parser("ping", help="Health check")
    p_ping.set_defaults(_method="ping", _collect=lambda a: {})

    # space
    space = sub.add_parser("space", help="Spaces (projects)").add_subparsers(dest="action", required=True)
    sp = space.add_parser("list", help="List spaces")
    sp.add_argument("--include-archived", action="store_true")
    sp.set_defaults(_method="space.list",
                    _collect=lambda a: {"include_archived": a.include_archived} if a.include_archived else {})
    sp = space.add_parser("get", help="Get a space")
    sp.add_argument("space_id")
    sp.set_defaults(_method="space.get", _collect=lambda a: _params(a, "space_id"))
    sp = space.add_parser("rename", help="Rename a space")
    sp.add_argument("space_id")
    sp.add_argument("--name", required=True)
    sp.set_defaults(_method="space.rename", _collect=lambda a: _params(a, "space_id", "name"))
    sp = space.add_parser("focus", help="Focus a space (no-op confirm)")
    sp.add_argument("space_id")
    sp.set_defaults(_method="space.focus", _collect=lambda a: _params(a, "space_id"))

    # tab
    tab = sub.add_parser("tab", help="Tabs").add_subparsers(dest="action", required=True)
    tp = tab.add_parser("list", help="List tabs in a space")
    tp.add_argument("--space", dest="space_id", required=True)
    tp.set_defaults(_method="tab.list", _collect=lambda a: _params(a, "space_id"))
    tp = tab.add_parser("create", help="Create a tab")
    tp.add_argument("--space", dest="space_id", required=True)
    tp.add_argument("--label")
    tp.add_argument("--position", type=int)
    tp.set_defaults(_method="tab.create", _collect=lambda a: _params(a, "space_id", "label", "position"))
    tp = tab.add_parser("get", help="Get a tab")
    tp.add_argument("tab_id")
    tp.set_defaults(_method="tab.get", _collect=lambda a: _params(a, "tab_id"))
    tp = tab.add_parser("rename", help="Rename a tab")
    tp.add_argument("tab_id")
    tp.add_argument("--label", required=True)
    tp.set_defaults(_method="tab.rename", _collect=lambda a: _params(a, "tab_id", "label"))
    tp = tab.add_parser("focus", help="Focus a tab (no-op confirm)")
    tp.add_argument("tab_id")
    tp.set_defaults(_method="tab.focus", _collect=lambda a: _params(a, "tab_id"))
    tp = tab.add_parser("close", help="Close (delete) a tab")
    tp.add_argument("tab_id")
    tp.set_defaults(_method="tab.close", _collect=lambda a: _params(a, "tab_id"))

    # layout
    layout = sub.add_parser("layout", help="Tab pane layout").add_subparsers(dest="action", required=True)
    lp = layout.add_parser("get", help="Get a tab's layout tree")
    lp.add_argument("tab_id")
    lp.set_defaults(_method="layout.get", _collect=lambda a: _params(a, "tab_id"))
    lp = layout.add_parser("put", help="Replace a tab's layout tree")
    lp.add_argument("tab_id")
    lp.add_argument("--tree", required=True, help="JSON split-tree")
    lp.add_argument("--focus-pane-id", dest="focus_pane_id")
    lp.set_defaults(_method="layout.put",
                    _collect=lambda a: {**_params(a, "tab_id", "focus_pane_id"), "tree": json.loads(a.tree)})

    # pane
    pane = sub.add_parser("pane", help="Panes").add_subparsers(dest="action", required=True)
    pp = pane.add_parser("list", help="List panes in a tab")
    pp.add_argument("tab_id")
    pp.set_defaults(_method="pane.list", _collect=lambda a: _params(a, "tab_id"))
    pp = pane.add_parser("split", help="Split a pane (or the tab root)")
    pp.add_argument("pane_id", nargs="?")
    pp.add_argument("--tab", dest="tab_id", required=True)
    pp.add_argument("--direction", choices=["right", "left", "up", "down"], default="right")
    pp.set_defaults(_method="pane.split", _collect=lambda a: _params(a, "tab_id", "pane_id", "direction"))
    pp = pane.add_parser("close", help="Close a pane")
    pp.add_argument("pane_id")
    pp.add_argument("--tab", dest="tab_id", required=True)
    pp.set_defaults(_method="pane.close", _collect=lambda a: _params(a, "tab_id", "pane_id"))
    pp = pane.add_parser("rename", help="Rename a pane")
    pp.add_argument("pane_id")
    pp.add_argument("--tab", dest="tab_id", required=True)
    pp.add_argument("--title", required=True)
    pp.set_defaults(_method="pane.rename", _collect=lambda a: _params(a, "tab_id", "pane_id", "title"))
    pp = pane.add_parser("send-text", help="Send literal text to a run's stdin")
    pp.add_argument("run_id")
    pp.add_argument("--text", required=True)
    pp.set_defaults(_method="pane.send_text", _collect=lambda a: _params(a, "run_id", "text"))
    pp = pane.add_parser("send-keys", help="Send named keys (enter/tab/esc/ctrl-c/up...) or literals")
    pp.add_argument("run_id")
    pp.add_argument("keys", nargs="+", help="Key names or literal strings")
    pp.set_defaults(_method="pane.send_keys", _collect=lambda a: {**_params(a, "run_id"), "keys": a.keys})
    pp = pane.add_parser("read", help="Read tail of a run's capture log")
    pp.add_argument("run_id")
    pp.add_argument("--source", choices=["visible", "recent", "recent-unwrapped"], default="recent")
    pp.add_argument("--lines", type=int)
    pp.set_defaults(_method="pane.read", _collect=lambda a: _params(a, "run_id", "source", "lines"))
    pp = pane.add_parser("report-agent", help="Report a thread's agent state from a pane")
    pp.add_argument("thread_id")
    pp.add_argument("--state", required=True, choices=["working", "blocked", "done", "idle", "unknown"])
    pp.add_argument("--message")
    pp.add_argument("--run-id", dest="run_id")
    pp.set_defaults(_method="pane.report_agent", _collect=lambda a: _params(a, "thread_id", "state", "message", "run_id"))

    # agent
    agent = sub.add_parser("agent", help="Agents (threads)").add_subparsers(dest="action", required=True)
    ap = agent.add_parser("list", help="List agents")
    ap.add_argument("--space", dest="space_id")
    ap.add_argument("--running-only", action="store_true")
    ap.set_defaults(_method="agent.list",
                    _collect=lambda a: {**_params(a, "space_id"), **({"running_only": True} if a.running_only else {})})
    ap = agent.add_parser("get", help="Get an agent")
    ap.add_argument("agent_id")
    ap.set_defaults(_method="agent.get", _collect=lambda a: _params(a, "agent_id"))
    ap = agent.add_parser("start", help="Start a new agent (thread + run) in a space")
    ap.add_argument("--space", dest="space_id", required=True)
    ap.add_argument("--harness", default="generic")
    ap.add_argument("--command")
    ap.add_argument("--cwd")
    ap.add_argument("--title")
    ap.add_argument("--tab", dest="tab_id")
    ap.add_argument("--pane", dest="pane_id")
    ap.set_defaults(_method="agent.start",
                    _collect=lambda a: _params(a, "space_id", "harness", "command", "cwd", "title", "tab_id", "pane_id"))
    ap = agent.add_parser("send", help="Send text to an agent's active run")
    ap.add_argument("agent_id")
    ap.add_argument("--text", required=True)
    ap.set_defaults(_method="agent.send", _collect=lambda a: _params(a, "agent_id", "text"))
    ap = agent.add_parser("read", help="Read tail of an agent's active run log")
    ap.add_argument("agent_id")
    ap.add_argument("--source", choices=["visible", "recent", "recent-unwrapped"], default="recent")
    ap.add_argument("--lines", type=int)
    ap.set_defaults(_method="agent.read", _collect=lambda a: _params(a, "agent_id", "source", "lines"))
    ap = agent.add_parser("focus", help="Focus an agent (no-op confirm)")
    ap.add_argument("agent_id")
    ap.set_defaults(_method="agent.focus", _collect=lambda a: _params(a, "agent_id"))
    ap = agent.add_parser("report-state", help="Report an agent's semantic state")
    ap.add_argument("agent_id")
    ap.add_argument("--state", required=True, choices=["working", "blocked", "done", "idle", "unknown"])
    ap.add_argument("--message")
    ap.add_argument("--run-id", dest="run_id")
    ap.set_defaults(_method="agent.report_state", _collect=lambda a: _params(a, "agent_id", "state", "message", "run_id"))

    # events
    events = sub.add_parser("events", help="Event stream").add_subparsers(dest="action", required=True)
    ep = events.add_parser("subscribe", help="Stream events for a thread until interrupted")
    ep.add_argument("--thread", dest="thread_id", required=True)
    ep.add_argument("--after-seq", dest="after_seq", type=int)
    ep.add_argument("--run-id", dest="run_id")
    ep.set_defaults(_method="events.subscribe", _stream=True,
                    _collect=lambda a: _params(a, "thread_id", "after_seq", "run_id"))

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    method = getattr(args, "_method", None)
    if method is None:
        parser.print_help(sys.stderr)
        return 2
    try:
        params = args._collect(args)
    except json.JSONDecodeError as exc:
        print(f"ody: invalid JSON argument: {exc}", file=sys.stderr)
        return 2
    if getattr(args, "owner", None):
        params.setdefault("owner", args.owner)
    if getattr(args, "_stream", False):
        return asyncio.run(_subscribe(method, params))
    return asyncio.run(_request(method, params))


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    raise SystemExit(main())
