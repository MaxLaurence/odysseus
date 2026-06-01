"""PTY/WebSocket transport for Coding Station tmux-backed runs."""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import pty
import shlex
import struct
import termios
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable


TMUX_SOCKET = "odysseus-cs"


@dataclass(frozen=True)
class TmuxResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class CodingPtyBridge:
    """Low-level tmux/PTY transport used by the coding runtime."""

    def __init__(
        self,
        *,
        tmux_available: Callable[[], bool],
        get_owned_run: Callable[[str, str], Awaitable[Any]],
        finished_statuses: set[str],
        logger: logging.Logger | None = None,
    ) -> None:
        self._tmux_available = tmux_available
        self._get_owned_run = get_owned_run
        self._finished_statuses = finished_statuses
        self._logger = logger or logging.getLogger(__name__)

    async def tmux_exec(self, *args: str, check: bool = True) -> TmuxResult:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "-L",
            TMUX_SOCKET,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        result = TmuxResult(proc.returncode, stdout, stderr)
        if check and proc.returncode != 0:
            raise RuntimeError(stderr.decode(errors="replace") or f"tmux exited {proc.returncode}")
        return result

    async def tmux_has_session(self, session: str) -> bool:
        if not session or not self._tmux_available():
            return False
        result = await self.tmux_exec("has-session", "-t", session, check=False)
        return result.returncode == 0

    async def start_pane_capture(self, session: str, log_path: Path) -> None:
        """Mirror a tmux pane's raw output to ``log_path`` via ``pipe-pane``."""
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        await self.tmux_exec(
            "pipe-pane",
            "-t",
            session,
            f"cat >> {shlex.quote(str(log_path))}",
            check=False,
        )

    @staticmethod
    def _set_winsize(fd: int, rows: int, cols: int) -> None:
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", max(1, rows), max(1, cols), 0, 0))
        except Exception:
            pass

    async def attach_pty(self, websocket, run_id: str, owner: str, cols: int = 120, rows: int = 40) -> None:
        """Bridge a WebSocket to the run's tmux session through a real PTY."""
        cols = max(20, min(int(cols or 120), 400))
        rows = max(5, min(int(rows or 40), 120))
        run = await self._get_owned_run(run_id, owner)
        session = run.tmux_session
        alive = bool(session) and await self.tmux_has_session(session)

        # Fresh queued runs launch asynchronously; wait for the tmux session before
        # falling back to read-only history replay.
        if not alive and (run.status or "").lower() in ("queued", "starting", "pending", ""):
            for _ in range(300):
                await asyncio.sleep(0.2)
                try:
                    run = await self._get_owned_run(run_id, owner)
                except Exception:
                    break
                session = run.tmux_session
                if session and await self.tmux_has_session(session):
                    alive = True
                    break
                if (run.status or "").lower() in self._finished_statuses:
                    break

        if not alive:
            try:
                log_path = Path(run.log_path) if run.log_path else None
                if log_path and log_path.exists():
                    data = log_path.read_bytes()
                    for i in range(0, len(data), 65536):
                        await websocket.send_bytes(data[i:i + 65536])
            except Exception:
                self._logger.debug("history replay failed", exc_info=True)
            try:
                await websocket.send_json({"type": "exit", "exit_code": run.exit_code})
            except Exception:
                pass
            return

        # Size the tmux window to THIS client up front (window-size is manual), so the
        # harness renders at the real pane width from the first frame instead of staying
        # at the session's creation size → wrapped/garbled output until a later resize.
        await self.tmux_exec("resize-window", "-t", session, "-x", str(cols), "-y", str(rows), check=False)

        master, slave = pty.openpty()
        self._set_winsize(master, rows, cols)

        def _child_setup() -> None:
            os.setsid()
            try:
                fcntl.ioctl(0, termios.TIOCSCTTY, 0)
            except Exception:
                pass

        attach_env = dict(os.environ)
        attach_env["TERM"] = "xterm-256color"
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "-L",
            TMUX_SOCKET,
            "attach-session",
            "-d",
            "-t",
            session,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            preexec_fn=_child_setup,
            env=attach_env,
        )
        os.close(slave)
        os.set_blocking(master, False)

        loop = asyncio.get_event_loop()
        out_queue: asyncio.Queue[bytes] = asyncio.Queue()

        def _on_readable() -> None:
            try:
                data = os.read(master, 65536)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                data = b""
            out_queue.put_nowait(data)

        loop.add_reader(master, _on_readable)

        async def _pump_out() -> None:
            while True:
                data = await out_queue.get()
                if not data:
                    return
                await websocket.send_bytes(data)

        async def _pump_in() -> None:
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    return
                if msg.get("bytes") is not None:
                    os.write(master, msg["bytes"])
                elif msg.get("text") is not None:
                    try:
                        ctrl = json.loads(msg["text"])
                    except Exception:
                        continue
                    if not isinstance(ctrl, dict):
                        continue
                    if ctrl.get("type") == "resize":
                        new_cols = max(20, min(int(ctrl.get("cols") or cols), 400))
                        new_rows = max(5, min(int(ctrl.get("rows") or rows), 120))
                        self._set_winsize(master, new_rows, new_cols)
                        # window-size is manual, so the PTY winsize alone won't move the
                        # tmux window — resize it explicitly to keep the harness in sync.
                        await self.tmux_exec(
                            "resize-window", "-t", session, "-x", str(new_cols), "-y", str(new_rows), check=False
                        )
                    elif ctrl.get("type") == "input" and ctrl.get("data") is not None:
                        os.write(master, str(ctrl["data"]).encode("utf-8"))

        out_task = asyncio.create_task(_pump_out())
        in_task = asyncio.create_task(_pump_in())
        try:
            _done, pending = await asyncio.wait({out_task, in_task}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
        finally:
            loop.remove_reader(master)
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                os.close(master)
            except OSError:
                pass
