"""Unix-socket JSON-RPC server for Code Station (the ``ody`` control plane).

A herdr-shaped control surface so an agent running *inside* a Code Station
terminal pane can drive its own layout (spaces, tabs, panes, agents) and the
runs behind those panes, without going through the HTTP API (whose port is
dynamic in the packaged app — see ``/tmp/recon/build_run.md``).

Transport
---------
``asyncio.start_unix_server`` over a loopback Unix domain socket. The wire is
newline-delimited JSON, one request object per line::

    request : {"id": <str>, "method": "<resource>.<action>", "params": {...}}
    success : {"id": <str>, "result": {...}}
    error   : {"id": <str>, "error": {"code": "<machine>", "message": "<text>"}}

``events.subscribe`` is the one streaming method: it emits zero or more
``{"id": <str>, "event": {...}}`` frames as runtime events arrive, until the
client disconnects.

Trust model
-----------
The socket is loopback-only and filesystem-permission gated (parent dir + the
socket file are created ``0o700``). We therefore treat every connected client as
trusted and do not re-authenticate: the owner is taken from ``params["owner"]``,
else ``$ODYSSEUS_OWNER``, else ``""`` (the single-user / loopback default that
``CodingRuntimeService`` already uses). This mirrors how harness hooks already
ride a run-scoped credential rather than a per-request login.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

# Allow large JSON-RPC lines (a layout.put tree or a pane.read response can exceed
# asyncio's default 64KB line limit, which would otherwise drop the connection).
_SOCKET_LINE_LIMIT = 4 * 1024 * 1024

from core.constants import DATA_DIR
from src.coding_runtime import CodingRuntimeError, get_coding_runtime_service
from src.coding_workspace import get_coding_workspace_service

logger = logging.getLogger(__name__)


# Named-key → escape sequence map for ``pane.send_keys`` / ``agent.send`` so a
# caller can say "enter" / "ctrl-c" instead of embedding control bytes in JSON.
_NAMED_KEYS: dict[str, str] = {
    "enter": "\r",
    "return": "\r",
    "newline": "\n",
    "tab": "\t",
    "esc": "\x1b",
    "escape": "\x1b",
    "space": " ",
    "backspace": "\x7f",
    "delete": "\x1b[3~",
    "up": "\x1b[A",
    "down": "\x1b[B",
    "right": "\x1b[C",
    "left": "\x1b[D",
    "home": "\x1b[H",
    "end": "\x1b[F",
    "pageup": "\x1b[5~",
    "pagedown": "\x1b[6~",
    "ctrl-c": "\x03",
    "ctrl-d": "\x04",
    "ctrl-z": "\x1a",
    "ctrl-l": "\x0c",
    "ctrl-u": "\x15",
    "ctrl-a": "\x01",
    "ctrl-e": "\x05",
    "ctrl-r": "\x12",
}

# How far to tail a pane's raw.log for ``pane.read`` / ``agent.read``.
_DEFAULT_READ_LINES = 200
_MAX_READ_LINES = 5000


def default_socket_path() -> str:
    """Resolve the socket path: ``$ODYSSEUS_ODY_SOCKET`` else ``<data_dir>/ody.sock``.

    ``DATA_DIR`` is the app data dir (``core.constants`` resolves it from the
    ``DATA_DIR`` env the desktop wrapper injects, else ``<repo>/data``). Fall
    back to ``./data/ody.sock`` if that yields nothing usable.
    """
    explicit = os.environ.get("ODYSSEUS_ODY_SOCKET", "").strip()
    if explicit:
        return explicit
    base = (DATA_DIR or "").strip() or os.path.join(os.getcwd(), "data")
    return os.path.join(base, "ody.sock")


def _resolve_owner(params: dict[str, Any]) -> str:
    """Trusted-owner resolution (see module docstring)."""
    owner = params.get("owner")
    if owner is None:
        owner = os.environ.get("ODYSSEUS_OWNER", "")
    return (owner or "").strip()


def _error_code(status_code: int) -> str:
    return {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        409: "conflict",
        422: "bad_request",
    }.get(int(status_code), "error")


def _named_keys_to_bytes(keys: Any) -> str:
    """Translate a key spec (str or list) into the literal bytes to write.

    A token matching a known key name (case-insensitive) expands to its escape
    sequence; anything else is sent literally. Accepts a single string or a list
    of tokens (joined with no separator).
    """
    if isinstance(keys, (list, tuple)):
        return "".join(_named_keys_to_bytes(k) for k in keys)
    token = str(keys or "")
    return _NAMED_KEYS.get(token.strip().lower(), token)


def _tail_log(log_path: str | None, lines: int) -> str:
    if not log_path:
        return ""
    path = Path(log_path)
    if not path.exists():
        return ""
    try:
        with path.open("rb") as fh:
            data = fh.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    if lines and lines > 0:
        text = "\n".join(text.splitlines()[-lines:])
    return text


class OdySocketServer:
    """JSON-RPC dispatcher + asyncio Unix-socket server lifecycle holder."""

    def __init__(self, socket_path: str | None = None) -> None:
        self.socket_path = socket_path or default_socket_path()
        self._server: asyncio.AbstractServer | None = None
        self._serve_task: asyncio.Task | None = None
        self._workspace = get_coding_workspace_service()
        self._runtime = get_coding_runtime_service()
        # method name -> (handler, is_streaming)
        self._dispatch: dict[str, Callable[[dict[str, Any]], Awaitable[Any]]] = {}
        self._build_dispatch()

    # ----------------------------------------------------------- lifecycle
    async def start(self) -> "OdySocketServer":
        path = Path(self.socket_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(path.parent, 0o700)
        except OSError:
            logger.debug("Could not chmod ody socket dir %s", path.parent, exc_info=True)
        # Remove a stale socket from a previous (crashed) process.
        try:
            if path.exists() or path.is_socket():
                os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError:
            logger.debug("Could not unlink stale ody socket %s", path, exc_info=True)

        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(path), limit=_SOCKET_LINE_LIMIT
        )
        try:
            os.chmod(path, 0o700)
        except OSError:
            logger.debug("Could not chmod ody socket %s", path, exc_info=True)
        # Hold a strong ref to the serve task so it isn't GC'd.
        self._serve_task = asyncio.create_task(self._server.serve_forever(), name="ody-socket-serve")
        logger.info("ody socket server listening on %s", path)
        return self

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                logger.debug("ody socket wait_closed error", exc_info=True)
            self._server = None
        if self._serve_task is not None:
            self._serve_task.cancel()
            try:
                await self._serve_task
            except (asyncio.CancelledError, Exception):
                pass
            self._serve_task = None
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        except OSError:
            logger.debug("Could not unlink ody socket on stop", exc_info=True)
        logger.info("ody socket server stopped")

    # ------------------------------------------------------------- transport
    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                try:
                    line = await reader.readline()
                except asyncio.LimitOverrunError:
                    await self._send(writer, {"id": None, "error": {"code": "bad_request", "message": "request line too large"}})
                    break
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                await self._handle_line(line, writer)
        except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
            pass
        except Exception:
            logger.debug("ody socket client error", exc_info=True)
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _handle_line(self, line: bytes, writer: asyncio.StreamWriter) -> None:
        try:
            request = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            await self._send(writer, {"id": None, "error": {"code": "bad_request", "message": "invalid JSON"}})
            return
        if not isinstance(request, dict):
            await self._send(writer, {"id": None, "error": {"code": "bad_request", "message": "request must be an object"}})
            return

        req_id = request.get("id")
        method = request.get("method")
        params = request.get("params")
        if not isinstance(params, dict):
            params = {}

        if not isinstance(method, str) or not method:
            await self._send(writer, {"id": req_id, "error": {"code": "bad_request", "message": "missing method"}})
            return

        # Streaming method gets its own driver (emits 'event' frames).
        if method == "events.subscribe":
            await self._stream_events(req_id, params, writer)
            return

        handler = self._dispatch.get(method)
        if handler is None:
            await self._send(writer, {"id": req_id, "error": {"code": "bad_request", "message": f"unknown method: {method}"}})
            return
        try:
            result = await handler(params)
            await self._send(writer, {"id": req_id, "result": result})
        except CodingRuntimeError as exc:
            await self._send(
                writer,
                {"id": req_id, "error": {"code": _error_code(exc.status_code), "message": exc.detail}},
            )
        except KeyError as exc:
            await self._send(
                writer,
                {"id": req_id, "error": {"code": "bad_request", "message": f"missing param: {exc}"}},
            )
        except Exception as exc:
            logger.debug("ody method %s failed", method, exc_info=True)
            await self._send(writer, {"id": req_id, "error": {"code": "error", "message": str(exc)}})

    async def _send(self, writer: asyncio.StreamWriter, obj: dict[str, Any]) -> None:
        data = (json.dumps(obj, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        writer.write(data)
        try:
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            raise

    # ----------------------------------------------------- streaming method
    async def _stream_events(
        self, req_id: Any, params: dict[str, Any], writer: asyncio.StreamWriter
    ) -> None:
        owner = _resolve_owner(params)
        thread_id = params.get("thread_id") or params.get("agent_id")
        if not thread_id:
            await self._send(writer, {"id": req_id, "error": {"code": "bad_request", "message": "thread_id is required"}})
            return
        after_seq = int(params.get("after_seq") or 0)
        run_id = params.get("run_id")
        try:
            async for item in self._runtime.event_stream(
                thread_id=str(thread_id),
                owner=owner,
                after_seq=after_seq,
                run_id=str(run_id) if run_id else None,
            ):
                await self._send(writer, {"id": req_id, "event": item})
        except CodingRuntimeError as exc:
            await self._send(
                writer,
                {"id": req_id, "error": {"code": _error_code(exc.status_code), "message": exc.detail}},
            )
        except (ConnectionResetError, BrokenPipeError):
            return  # client gone — stop streaming
        except Exception as exc:
            logger.debug("ody events.subscribe failed", exc_info=True)
            try:
                await self._send(writer, {"id": req_id, "error": {"code": "error", "message": str(exc)}})
            except Exception:
                pass

    # -------------------------------------------------------------- agents
    async def _agent_start(self, params: dict[str, Any]) -> dict[str, Any]:
        """Create a CodingThread (the Agent row) then enqueue its first run.

        Mirrors the inline ORM in ``routes/coding_routes.py::create_thread`` but
        kept minimal: resolve the space, create the thread with the requested
        harness/cwd/title, then ``runtime.enqueue_run`` to launch it.
        """
        from core.database import CodingProject, CodingThread, SessionLocal

        owner = _resolve_owner(params)
        space_id = params.get("space_id") or params.get("project_id")
        if not space_id:
            raise CodingRuntimeError(400, "space_id is required")
        harness = (params.get("harness") or params.get("harness_id") or "").strip() or "generic"
        command = params.get("command")
        cwd = params.get("cwd")
        title = params.get("title")
        tab_id = params.get("tab_id")
        pane_id = params.get("pane_id")

        db = SessionLocal()
        try:
            project = (
                db.query(CodingProject)
                .filter(CodingProject.id == space_id, CodingProject.owner == owner)
                .first()
            )
            if not project:
                raise CodingRuntimeError(404, "Space not found")
            if project.archived:
                raise CodingRuntimeError(409, "Space is archived")
            thread = CodingThread(
                id=str(uuid.uuid4()),
                project_id=project.id,
                owner=owner,
                title=(title or project.name or "Agent").strip(),
                cwd=str(Path(cwd or project.root_path or Path.home()).expanduser()),
                harness_id=harness,
                model_endpoint_id=(project.default_endpoint_id or "").strip(),
                model=(project.default_model or "").strip(),
                status="idle",
                tab_id=tab_id,
                pane_id=pane_id,
                metadata_json=json.dumps({}),
            )
            db.add(thread)
            project.updated_at = datetime.utcnow()  # column default hasn't flushed yet
            db.commit()
            db.refresh(thread)
            thread_id = thread.id
        finally:
            db.close()

        run = await self._runtime.enqueue_run(
            thread_id=thread_id,
            owner=owner,
            command=command,
            cwd=cwd,
            harness_id=harness,
            cols=params.get("cols"),
            rows=params.get("rows"),
        )
        agent = self._workspace.get_agent(owner, thread_id)
        return {"agent": agent, "run": self._run_dict(run)}

    async def _agent_send(self, params: dict[str, Any]) -> dict[str, Any]:
        owner = _resolve_owner(params)
        agent_id = params["agent_id"]
        run_id = self._workspace.get_agent(owner, agent_id).get("last_run_id")
        if not run_id:
            raise CodingRuntimeError(400, "Agent has no active run")
        text = params.get("text", "")
        run = await self._runtime.send_stdin(str(run_id), owner, text)
        return {"run": self._run_dict(run)}

    async def _agent_read(self, params: dict[str, Any]) -> dict[str, Any]:
        owner = _resolve_owner(params)
        agent_id = params["agent_id"]
        run_id = self._workspace.get_agent(owner, agent_id).get("last_run_id")
        if not run_id:
            return {"run_id": None, "text": ""}
        return await self._run_read(owner, str(run_id), params)

    # ---------------------------------------------------------------- panes
    async def _pane_read(self, params: dict[str, Any]) -> dict[str, Any]:
        owner = _resolve_owner(params)
        run_id = params["run_id"]
        return await self._run_read(owner, str(run_id), params)

    async def _run_read(self, owner: str, run_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """Tail a run's capture log. ``source`` (visible|recent|recent-unwrapped)
        is accepted for forward-compat but in v1 all simply tail ``raw.log``."""
        from core.database import CodingRun, SessionLocal

        db = SessionLocal()
        try:
            run = (
                db.query(CodingRun)
                .filter(CodingRun.id == run_id, CodingRun.owner == owner)
                .first()
            )
            if not run:
                raise CodingRuntimeError(404, "Run not found")
            log_path = run.log_path
        finally:
            db.close()
        lines = params.get("lines")
        try:
            lines = int(lines) if lines is not None else _DEFAULT_READ_LINES
        except (TypeError, ValueError):
            lines = _DEFAULT_READ_LINES
        lines = max(0, min(lines, _MAX_READ_LINES))
        return {
            "run_id": run_id,
            "source": params.get("source") or "recent",
            "text": _tail_log(log_path, lines),
        }

    async def _pane_send_text(self, params: dict[str, Any]) -> dict[str, Any]:
        owner = _resolve_owner(params)
        run_id = params["run_id"]
        run = await self._runtime.send_stdin(str(run_id), owner, params.get("text", ""))
        return {"run": self._run_dict(run)}

    async def _pane_send_keys(self, params: dict[str, Any]) -> dict[str, Any]:
        owner = _resolve_owner(params)
        run_id = params["run_id"]
        data = _named_keys_to_bytes(params.get("keys", ""))
        run = await self._runtime.send_stdin(str(run_id), owner, data)
        return {"run": self._run_dict(run)}

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _run_dict(run: Any) -> dict[str, Any]:
        return {
            "id": run.id,
            "thread_id": run.thread_id,
            "status": run.status,
            "command": run.command,
            "cwd": run.cwd,
            "run_dir": run.run_dir,
            "log_path": run.log_path,
            "exit_code": getattr(run, "exit_code", None),
        }

    # ------------------------------------------------------- dispatch table
    def _build_dispatch(self) -> None:
        ws = self._workspace

        async def ping(_p: dict[str, Any]) -> dict[str, Any]:
            return {"pong": True}

        # ---- spaces
        async def space_list(p):
            return {"spaces": ws.list_spaces(_resolve_owner(p), include_archived=bool(p.get("include_archived")))}

        async def space_get(p):
            return {"space": ws.get_space(_resolve_owner(p), p["space_id"])}

        async def space_rename(p):
            return {"space": ws.rename_space(_resolve_owner(p), p["space_id"], p.get("name", ""))}

        async def space_focus(p):
            # No backend layout state to mutate — return the space so the caller
            # can confirm it exists (focus is a UI concern).
            return {"ok": True, "space": ws.get_space(_resolve_owner(p), p["space_id"])}

        # ---- tabs
        async def tab_list(p):
            return {"tabs": ws.list_tabs(_resolve_owner(p), p["space_id"])}

        async def tab_create(p):
            return {"tab": ws.create_tab(_resolve_owner(p), p["space_id"], label=p.get("label"), position=p.get("position"))}

        async def tab_get(p):
            return {"tab": ws.get_tab(_resolve_owner(p), p["tab_id"])}

        async def tab_rename(p):
            return {"tab": ws.update_tab(_resolve_owner(p), p["tab_id"], label=p.get("label"))}

        async def tab_focus(p):
            return {"ok": True, "tab": ws.get_tab(_resolve_owner(p), p["tab_id"])}

        async def tab_close(p):
            ws.delete_tab(_resolve_owner(p), p["tab_id"])
            return {"ok": True, "tab_id": p["tab_id"]}

        # ---- layouts
        async def layout_get(p):
            return {"layout": ws.get_layout(_resolve_owner(p), p["tab_id"])}

        async def layout_put(p):
            return {"layout": ws.put_layout(_resolve_owner(p), p["tab_id"], p.get("tree"), focus_pane_id=p.get("focus_pane_id"))}

        # ---- panes
        async def pane_list(p):
            return {"panes": ws.list_panes(_resolve_owner(p), p["tab_id"])}

        async def pane_split(p):
            return ws.split_pane(_resolve_owner(p), p["tab_id"], pane_id=p.get("pane_id"), direction=p.get("direction") or "right")

        async def pane_close(p):
            return ws.close_pane(_resolve_owner(p), p["tab_id"], p["pane_id"])

        async def pane_rename(p):
            return ws.rename_pane(_resolve_owner(p), p["tab_id"], p["pane_id"], p.get("title"))

        async def pane_report_agent(p):
            return await ws.set_agent_state(
                _resolve_owner(p), p["thread_id"], p.get("state", "unknown"),
                run_id=p.get("run_id"), message=p.get("message"),
            )

        # ---- agents
        async def agent_list(p):
            return {"agents": ws.list_agents(_resolve_owner(p), space_id=p.get("space_id"), running_only=bool(p.get("running_only")))}

        async def agent_get(p):
            return {"agent": ws.get_agent(_resolve_owner(p), p["agent_id"])}

        async def agent_focus(p):
            return {"ok": True, "agent": ws.get_agent(_resolve_owner(p), p["agent_id"])}

        async def agent_report_state(p):
            return await ws.set_agent_state(
                _resolve_owner(p), p["agent_id"], p.get("state", "unknown"),
                run_id=p.get("run_id"), message=p.get("message"),
            )

        self._dispatch = {
            "ping": ping,
            "space.list": space_list,
            "space.get": space_get,
            "space.rename": space_rename,
            "space.focus": space_focus,
            "tab.list": tab_list,
            "tab.create": tab_create,
            "tab.get": tab_get,
            "tab.rename": tab_rename,
            "tab.focus": tab_focus,
            "tab.close": tab_close,
            "layout.get": layout_get,
            "layout.put": layout_put,
            "pane.list": pane_list,
            "pane.split": pane_split,
            "pane.close": pane_close,
            "pane.rename": pane_rename,
            "pane.send_text": self._pane_send_text,
            "pane.send_keys": self._pane_send_keys,
            "pane.read": self._pane_read,
            "pane.report_agent": pane_report_agent,
            "agent.list": agent_list,
            "agent.get": agent_get,
            "agent.start": self._agent_start,
            "agent.send": self._agent_send,
            "agent.read": self._agent_read,
            "agent.focus": agent_focus,
            "agent.report_state": agent_report_state,
            # events.subscribe is handled specially in _handle_line (streaming).
        }


# Module-level convenience API for app.py wiring -----------------------------

async def start_ody_socket_server(socket_path: str | None = None) -> OdySocketServer:
    """Create + start the ody Unix-socket server. Returns the server object;
    hold the returned reference so the serve task isn't garbage-collected."""
    server = OdySocketServer(socket_path)
    await server.start()
    return server


async def stop_ody_socket_server(server: OdySocketServer | None) -> None:
    """Stop a server previously returned by :func:`start_ody_socket_server`."""
    if server is not None:
        await server.stop()
