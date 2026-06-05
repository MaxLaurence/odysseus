"""PTY/dtach transport for Coding Station persistent runs.

Each run's program lives in a ``dtach`` session — a *thin* detach/attach tool with
**no terminal emulation, scrollback, or screen reflow** of its own. The program's
raw PTY byte stream is passed straight through to the browser terminal (Ghostty/
xterm.js), so the browser emulator owns scrollback and a resize is a clean, direct
SIGWINCH to the program.

We used to run each pane inside ``tmux``. tmux is itself a terminal emulator, so
stacking it under the browser emulator was the root cause of the persistent scroll
and resize bugs: on attach tmux switches the client into the alternate screen (which
has no saved lines by spec, so the browser's scrollback stayed empty), it keeps all
history in its own copy-mode buffer and streams only the viewport, and on resize it
reflows/redraws the whole screen and clamps to the smallest attached client. None of
that is tunable away — it's architectural — so tmux was replaced with dtach.

Persistence + remote access (Tailscale): the program survives a client disconnect,
and ``dtach -a /tmp/odysseus-cs/<name>`` from any machine attaches the SAME live
session. dtach client flags we always pass: ``-E`` (disable the detach character so
every byte — including pasted control bytes — reaches the program), ``-z`` (disable
the suspend key), ``-r winch`` (redraw via SIGWINCH on attach; no injected ^L).
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import pty
import shutil
import signal
import stat
import struct
import termios
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable


# Short socket dir: a unix socket path must fit in sun_path (~104 bytes on macOS),
# so we cannot place sockets under the run dir (the packaged app's data dir alone is
# ~80 chars). Sessions don't survive a reboot anyway (the programs don't), so /tmp is
# fine. Mirrors the spirit of the old private ``tmux -L odysseus-cs`` socket.
SOCKET_DIR = Path("/tmp/odysseus-cs")

# dtach attach-client flags (see module docstring).
_DTACH_CLIENT_FLAGS = ("-E", "-z", "-r", "winch")


def dtach_bin() -> str | None:
    """Resolve the dtach executable, preferring the copy bundled with the app so the
    packaged build doesn't depend on a Homebrew install. Falls back to PATH and the
    usual Homebrew locations (a GUI-launched backend can have a minimal PATH). Returns
    None if dtach can't be found anywhere (then runs use the no-session subprocess path).
    """
    # 1. Bundled with the app: <_internal>/vendor/dtach (parallels scripts_dir()).
    bundled = Path(__file__).resolve().parent.parent / "vendor" / "dtach"
    try:
        if bundled.is_file() and os.access(bundled, os.X_OK):
            return str(bundled)
    except Exception:
        pass
    # 2. On PATH.
    found = shutil.which("dtach")
    if found:
        return found
    # 3. Common Homebrew prefixes (arm64 + Intel).
    for candidate in ("/opt/homebrew/bin/dtach", "/usr/local/bin/dtach"):
        try:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        except Exception:
            pass
    return None


@dataclass(frozen=True)
class DtachResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class CodingPtyBridge:
    """Low-level dtach/PTY transport used by the coding runtime."""

    def __init__(
        self,
        *,
        backend_available: Callable[[], bool],
        get_owned_run: Callable[[str, str], Awaitable[Any]],
        finished_statuses: set[str],
        logger: logging.Logger | None = None,
    ) -> None:
        self._backend_available = backend_available
        self._get_owned_run = get_owned_run
        self._finished_statuses = finished_statuses
        self._logger = logger or logging.getLogger(__name__)

    # ---- socket helpers --------------------------------------------------

    @staticmethod
    def socket_path(name: str) -> Path:
        return SOCKET_DIR / name

    @classmethod
    def _ensure_socket_dir(cls) -> None:
        try:
            SOCKET_DIR.mkdir(parents=True, exist_ok=True)
            os.chmod(SOCKET_DIR, 0o700)
        except Exception:
            pass

    async def has_session(self, name: str) -> bool:
        if not name or not self._backend_available():
            return False
        try:
            st = os.stat(self.socket_path(name))
            return stat.S_ISSOCK(st.st_mode)
        except FileNotFoundError:
            return False
        except Exception:
            return False

    # ---- session lifecycle ----------------------------------------------

    async def create_session(
        self,
        name: str,
        script_path: Path,
        env: dict[str, str],
        *,
        cwd: str,
    ) -> DtachResult:
        """Create a detached dtach session running ``script_path``.

        ``dtach -n`` forks a daemon master (it reparents to PID 1 and keeps the full
        argv, so it can later be found/killed via the socket path) and the foreground
        invocation returns immediately. The program starts on a fresh PTY at 0x0
        until the first client attaches — the capture logger (started right after
        this) attaches at the run's initial size, so the program is sized promptly.
        """
        dtach = dtach_bin()
        if not dtach:
            return DtachResult(127, b"", b"dtach executable not found")
        self._ensure_socket_dir()
        sock = str(self.socket_path(name))
        # Clear any stale socket left by a crashed master of the same name.
        try:
            os.unlink(sock)
        except FileNotFoundError:
            pass
        except Exception:
            pass
        proc = await asyncio.create_subprocess_exec(
            dtach,
            "-n",
            sock,
            str(script_path),
            env=env,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _out, err = await proc.communicate()
        return DtachResult(proc.returncode, b"", err or b"")

    async def send_input(self, name: str, data: bytes) -> None:
        """Inject raw bytes into the session's program without a full attach
        (``dtach -p`` copies stdin to the session). Used for HTTP stdin and for
        the graceful Ctrl-C on stop."""
        if not data or not await self.has_session(name):
            return
        dtach = dtach_bin()
        if not dtach:
            return
        sock = str(self.socket_path(name))
        try:
            proc = await asyncio.create_subprocess_exec(
                dtach,
                "-p",
                sock,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate(input=data)
        except Exception:
            self._logger.debug("dtach send_input failed for %s", name, exc_info=True)

    async def kill_session(self, name: str) -> None:
        """Terminate a session by killing its dtach master (which kills the program).
        The master keeps ``dtach -n <socket>`` in its argv and removes the socket on
        exit; the socket name is unique per run so the pattern can't match another."""
        if not name:
            return
        sock = str(self.socket_path(name))
        try:
            proc = await asyncio.create_subprocess_exec(
                "pkill",
                "-f",
                f"dtach -n {sock}",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate()
        except Exception:
            self._logger.debug("pkill for dtach session %s failed", name, exc_info=True)
        # Remove a stale socket if the master was already gone / didn't clean up.
        try:
            os.unlink(sock)
        except FileNotFoundError:
            pass
        except Exception:
            pass

    def start_capture(
        self,
        name: str,
        log_path: Path,
        *,
        init_cols: int,
        init_rows: int,
        output_redactor: Callable[[bytes], bytes] | None = None,
    ) -> asyncio.Task:
        """Start the persistent capture logger (replaces ``tmux pipe-pane``).

        Returns an ``asyncio.Task``; the caller tracks it and cancels it on teardown
        (it also self-terminates when the session ends)."""
        return asyncio.create_task(
            self._capture_loop(
                name,
                Path(log_path),
                int(init_cols),
                int(init_rows),
                output_redactor=output_redactor,
            )
        )

    async def _capture_loop(
        self,
        name: str,
        log_path: Path,
        init_cols: int,
        init_rows: int,
        *,
        output_redactor: Callable[[bytes], bytes] | None = None,
    ) -> None:
        """A long-lived dtach client that tees ALL session output to ``log_path`` for
        the run's lifetime — so output is captured even when no browser is attached
        (autonomous agents), exactly like the old pipe-pane. It attaches FIRST at the
        run's initial size (giving the 0x0 program a real size); later browser
        attaches win the size (dtach: last attach/resize wins) and this logger never
        re-pushes its size, so it can't clobber the browser geometry."""
        # Give the freshly-created session a moment to come up.
        for _ in range(50):
            if await self.has_session(name):
                break
            await asyncio.sleep(0.1)
        else:
            return
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        master, slave = pty.openpty()
        self._set_winsize(master, init_rows, init_cols)
        proc = await self._spawn_attach(name, slave)
        os.close(slave)
        os.set_blocking(master, False)

        loop = asyncio.get_event_loop()
        queue: asyncio.Queue[bytes] = asyncio.Queue()

        def _on_readable() -> None:
            try:
                data = os.read(master, 65536)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                data = b""
            queue.put_nowait(data)

        loop.add_reader(master, _on_readable)
        # Push the initial size to the program (the program started at 0x0).
        try:
            os.kill(proc.pid, signal.SIGWINCH)
        except Exception:
            pass
        try:
            with open(log_path, "ab", buffering=0) as f:
                while True:
                    data = await queue.get()
                    if not data:
                        return
                    if output_redactor is not None:
                        try:
                            data = output_redactor(data)
                        except Exception:
                            self._logger.debug("capture output redactor failed", exc_info=True)
                    try:
                        f.write(data)
                    except Exception:
                        pass
        except asyncio.CancelledError:
            raise
        finally:
            loop.remove_reader(master)
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            except Exception:
                pass
            try:
                os.close(master)
            except OSError:
                pass

    # ---- low-level helpers ----------------------------------------------

    @staticmethod
    def _set_winsize(fd: int, rows: int, cols: int) -> None:
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", max(1, rows), max(1, cols), 0, 0))
        except Exception:
            pass

    async def _spawn_attach(self, name: str, slave_fd: int):
        """Spawn a ``dtach -a`` client bridged to the given PTY slave."""

        def _child_setup() -> None:
            os.setsid()
            try:
                fcntl.ioctl(0, termios.TIOCSCTTY, 0)
            except Exception:
                pass

        attach_env = dict(os.environ)
        attach_env["TERM"] = "xterm-256color"
        return await asyncio.create_subprocess_exec(
            dtach_bin() or "dtach",
            "-a",
            str(self.socket_path(name)),
            *_DTACH_CLIENT_FLAGS,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=_child_setup,
            env=attach_env,
        )

    # ---- WebSocket bridge -----------------------------------------------

    async def attach_pty(
        self,
        websocket,
        run_id: str,
        owner: str,
        cols: int = 120,
        rows: int = 40,
        on_input: Callable[[bytes], None] | None = None,
        history_redactor: Callable[[bytes], bytes] | None = None,
    ) -> None:
        """Bridge a WebSocket to the run's dtach session through a real PTY.

        ``on_input`` (optional) is called with each chunk of input bytes the client
        sends — used by the runtime to auto-title a thread from the first typed line."""
        cols = max(20, min(int(cols or 120), 400))
        rows = max(5, min(int(rows or 40), 120))
        run = await self._get_owned_run(run_id, owner)
        name = run.tmux_session
        alive = bool(name) and await self.has_session(name)

        # Fresh queued runs launch asynchronously; wait for the session before
        # falling back to read-only history replay.
        if not alive and (run.status or "").lower() in ("queued", "starting", "pending", ""):
            for _ in range(300):
                await asyncio.sleep(0.2)
                try:
                    run = await self._get_owned_run(run_id, owner)
                except Exception:
                    break
                name = run.tmux_session
                if name and await self.has_session(name):
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
                        chunk = data[i:i + 65536]
                        if history_redactor is not None:
                            chunk = history_redactor(chunk)
                        await websocket.send_bytes(chunk)
            except Exception:
                self._logger.debug("history replay failed", exc_info=True)
            try:
                await websocket.send_json({"type": "exit", "exit_code": run.exit_code})
            except Exception:
                pass
            return

        await self._bridge_ws(websocket, name, cols, rows, on_input=on_input)

    async def attach_session(
        self,
        websocket,
        name: str,
        *,
        cols: int = 100,
        rows: int = 30,
        log_path: Path | None = None,
        output_redactor: Callable[[bytes], bytes] | None = None,
    ) -> None:
        """Bridge a WebSocket to an arbitrary (non-run) dtach session by name.

        Used for the subscription-login PTYs (``CodingAuthService``), which are not
        backed by a ``CodingRun``. Identical transport to ``attach_pty`` but without
        the run lookup; replays ``log_path`` history when the session is not alive."""
        cols = max(20, min(int(cols or 100), 400))
        rows = max(5, min(int(rows or 30), 120))
        if not name or not await self.has_session(name):
            try:
                if log_path and Path(log_path).exists():
                    data = Path(log_path).read_bytes()
                    for i in range(0, len(data), 65536):
                        chunk = data[i:i + 65536]
                        if output_redactor is not None:
                            chunk = output_redactor(chunk)
                        await websocket.send_bytes(chunk)
            except Exception:
                self._logger.debug("session history replay failed", exc_info=True)
            try:
                await websocket.send_json({"type": "exit", "exit_code": None})
            except Exception:
                pass
            return
        await self._bridge_ws(websocket, name, cols, rows, output_redactor=output_redactor)

    async def _bridge_ws(
        self,
        websocket,
        name: str,
        cols: int,
        rows: int,
        on_input: Callable[[bytes], None] | None = None,
        output_redactor: Callable[[bytes], bytes] | None = None,
    ) -> None:
        """Shared PTY<->WebSocket bridge for a live dtach session (used by both
        ``attach_pty`` and ``attach_session``). Detaching leaves the session alive.

        ``on_input`` (optional) observes client input bytes; failures are swallowed so
        a buggy observer can never wedge the terminal."""
        master, slave = pty.openpty()
        self._set_winsize(master, rows, cols)
        proc = await self._spawn_attach(name, slave)
        os.close(slave)
        os.set_blocking(master, False)
        # Push this client's size to the program (browser geometry wins; dtach uses
        # the most recent attach/resize). The capture logger attached first at the
        # run's initial size — this corrects it to the actual pane size.
        try:
            os.kill(proc.pid, signal.SIGWINCH)
        except Exception:
            pass

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

        def _observe_input(data: bytes) -> None:
            if on_input is None:
                return
            try:
                on_input(data)
            except Exception:
                self._logger.debug("on_input observer raised", exc_info=True)

        async def _pump_out() -> None:
            while True:
                data = await out_queue.get()
                if not data:
                    return
                if output_redactor is not None:
                    try:
                        data = output_redactor(data)
                    except Exception:
                        self._logger.debug("bridge output redactor failed", exc_info=True)
                await websocket.send_bytes(data)

        async def _pump_in() -> None:
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    return
                if msg.get("bytes") is not None:
                    os.write(master, msg["bytes"])
                    _observe_input(msg["bytes"])
                elif msg.get("text") is not None:
                    try:
                        ctrl = json.loads(msg["text"])
                    except Exception:
                        continue
                    if not isinstance(ctrl, dict):
                        continue
                    if ctrl.get("type") == "resize":
                        # Set the PTY winsize and nudge the dtach client with SIGWINCH
                        # so it forwards the new size to the program (clean resize, no
                        # tmux reflow grid).
                        self._set_winsize(master, int(ctrl.get("rows") or rows), int(ctrl.get("cols") or cols))
                        try:
                            os.kill(proc.pid, signal.SIGWINCH)
                        except Exception:
                            pass
                    elif ctrl.get("type") == "input" and ctrl.get("data") is not None:
                        payload = str(ctrl["data"]).encode("utf-8")
                        os.write(master, payload)
                        _observe_input(payload)

        out_task = asyncio.create_task(_pump_out())
        in_task = asyncio.create_task(_pump_in())
        try:
            _done, pending = await asyncio.wait({out_task, in_task}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
        finally:
            loop.remove_reader(master)
            # Detaching (terminating our dtach client) leaves the SESSION alive —
            # the program keeps running for reconnects / remote attach.
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                os.close(master)
            except OSError:
                pass
