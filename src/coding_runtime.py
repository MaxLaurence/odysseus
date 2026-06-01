"""Runtime and queue manager for coding station terminal runs."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator

from sqlalchemy import func

from core.constants import DATA_DIR
from core.database import (
    CodingProject,
    CodingRun,
    CodingThread,
    CodingThreadEvent,
    SessionLocal,
)
from src.coding_harnesses import build_harness_command, get_harness
from src.coding_model_config import build_launch_env
from src.settings import get_setting

logger = logging.getLogger(__name__)

RUN_ROOT = Path(DATA_DIR) / "coding_runs"
ACTIVE_STATUSES = {"starting", "running", "stopping"}
BLOCKING_STATUSES = {"queued", "starting", "running", "stopping"}
FINISHED_STATUSES = {"exited", "failed", "cancelled"}


class CodingRuntimeError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except Exception:
        return fallback


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _now() -> datetime:
    return datetime.utcnow()


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _safe_cwd(cwd: str | None, fallback: str | None) -> str:
    raw = (cwd or fallback or str(Path.home())).strip()
    return str(Path(raw).expanduser())


def _user_login_shell() -> str:
    """The user's real login shell, so harness runs inherit the same PATH/env the user
    has in their own terminal (e.g. ~/.bun/bin, /opt/homebrew/bin added by shell rc)."""
    shell = os.environ.get("SHELL")
    if not shell:
        try:
            import pwd

            shell = pwd.getpwuid(os.getuid()).pw_shell
        except Exception:
            shell = None
    return shell or "/bin/bash"


class CodingRuntimeService:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._active_tasks: dict[str, asyncio.Task] = {}
        self._active_processes: dict[str, asyncio.subprocess.Process] = {}

    def max_concurrent_runs(self) -> int:
        try:
            value = int(get_setting("coding_max_concurrent_threads", 2) or 2)
        except Exception:
            value = 2
        return max(1, value)

    def tmux_available(self) -> bool:
        return shutil.which("tmux") is not None

    def run_dir(self, run_id: str) -> Path:
        return RUN_ROOT / run_id

    def _append_event_db(
        self,
        db,
        thread_id: str,
        run_id: str | None,
        kind: str,
        payload: dict[str, Any] | None = None,
    ) -> CodingThreadEvent:
        next_seq = (
            db.query(func.max(CodingThreadEvent.seq))
            .filter(CodingThreadEvent.thread_id == thread_id)
            .scalar()
            or 0
        ) + 1
        event = CodingThreadEvent(
            thread_id=thread_id,
            run_id=run_id,
            seq=next_seq,
            kind=kind,
            payload_json=_json_dumps(payload or {}),
        )
        db.add(event)
        if run_id:
            run = db.query(CodingRun).filter(CodingRun.id == run_id).first()
            if run and run.run_dir:
                try:
                    path = Path(run.run_dir) / "events.jsonl"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open("a", encoding="utf-8") as f:
                        f.write(
                            _json_dumps(
                                {
                                    "thread_id": thread_id,
                                    "run_id": run_id,
                                    "seq": next_seq,
                                    "kind": kind,
                                    "payload": payload or {},
                                    "created_at": _iso(event.created_at),
                                }
                            )
                            + "\n"
                        )
                except Exception:
                    logger.debug("Failed to mirror coding event to run_dir", exc_info=True)
        return event

    async def append_event(
        self,
        thread_id: str,
        run_id: str | None,
        kind: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        async with self._lock:
            db = SessionLocal()
            try:
                self._append_event_db(db, thread_id, run_id, kind, payload)
                db.commit()
            finally:
                db.close()

    async def enqueue_run(
        self,
        *,
        thread_id: str,
        owner: str,
        command: str | None = None,
        cwd: str | None = None,
        harness_id: str | None = None,
        model_endpoint_id: str | None = None,
        model: str | None = None,
        replace: bool = False,
        idempotency_key: str | None = None,
        metadata: dict[str, Any] | None = None,
        cols: int | None = None,
        rows: int | None = None,
    ) -> CodingRun:
        metadata = metadata if isinstance(metadata, dict) else {}
        async with self._lock:
            db = SessionLocal()
            try:
                thread = (
                    db.query(CodingThread)
                    .filter(CodingThread.id == thread_id, CodingThread.owner == owner)
                    .first()
                )
                if not thread:
                    raise CodingRuntimeError(404, "Thread not found")
                project = (
                    db.query(CodingProject)
                    .filter(CodingProject.id == thread.project_id, CodingProject.owner == owner)
                    .first()
                )
                if not project:
                    raise CodingRuntimeError(404, "Project not found")

                if idempotency_key:
                    existing = (
                        db.query(CodingRun)
                        .filter(
                            CodingRun.thread_id == thread.id,
                            CodingRun.owner == owner,
                            CodingRun.idempotency_key == idempotency_key,
                        )
                        .order_by(CodingRun.queued_at.desc())
                        .first()
                    )
                    if existing:
                        return self._detach_run(db, existing.id)

                blocking = (
                    db.query(CodingRun)
                    .filter(
                        CodingRun.thread_id == thread.id,
                        CodingRun.status.in_(tuple(BLOCKING_STATUSES)),
                    )
                    .order_by(CodingRun.queued_at.desc())
                    .first()
                )
                if blocking and not replace:
                    raise CodingRuntimeError(409, f"Thread already has active run {blocking.id}")
                if blocking and replace:
                    await self._stop_run_db(db, blocking, reason="replaced")

                selected_harness = (harness_id or thread.harness_id or project.default_harness or "generic").strip()
                get_harness(selected_harness)
                if harness_id:
                    thread.harness_id = selected_harness
                if cwd:
                    thread.cwd = _safe_cwd(cwd, project.root_path)
                if model_endpoint_id is not None:
                    thread.model_endpoint_id = model_endpoint_id.strip()
                if model is not None:
                    thread.model = model.strip()

                run_id = str(uuid.uuid4())
                run_dir = self.run_dir(run_id)
                run_dir.mkdir(parents=True, exist_ok=True)
                log_path = run_dir / "raw.log"
                state_path = run_dir / "state.json"

                built_command = build_harness_command(selected_harness, command, metadata)
                run_cwd = _safe_cwd(cwd, thread.cwd or project.root_path)
                run_metadata = {
                    "requested_command": command or "",
                    "harness_metadata": metadata,
                    "state_path": str(state_path),
                    # Launch the pty at the UI pane's real size so the harness renders at the
                    # right width from the first frame (no 80-col wrap / full-redraw churn).
                    "cols": max(20, min(int(cols), 400)) if cols else 120,
                    "rows": max(5, min(int(rows), 120)) if rows else 40,
                }
                run = CodingRun(
                    id=run_id,
                    thread_id=thread.id,
                    owner=owner,
                    harness_id=selected_harness,
                    status="queued",
                    command=built_command,
                    cwd=run_cwd,
                    run_dir=str(run_dir),
                    log_path=str(log_path),
                    idempotency_key=(idempotency_key or "").strip() or None,
                    metadata_json=_json_dumps(run_metadata),
                )
                db.add(run)
                thread.status = "queued"
                thread.last_run_id = run.id
                thread.updated_at = _now()
                self._append_event_db(
                    db,
                    thread.id,
                    run.id,
                    "queued",
                    {
                        "run_id": run.id,
                        "harness_id": selected_harness,
                        "command": built_command,
                        "cwd": run_cwd,
                    },
                )
                state_path.write_text(
                    _json_dumps(
                        {
                            "run_id": run.id,
                            "thread_id": thread.id,
                            "status": "queued",
                            "queued_at": _iso(run.queued_at),
                        }
                    ),
                    encoding="utf-8",
                )
                db.commit()
                detached = self._detach_run(db, run.id)
            finally:
                db.close()

        await self.pump_queue()
        return detached

    async def _stop_run_db(self, db, run: CodingRun, reason: str = "stopped") -> None:
        if run.status in FINISHED_STATUSES:
            return
        if run.status == "queued":
            run.status = "cancelled"
            run.finished_at = _now()
            run.error = reason
            thread = db.query(CodingThread).filter(CodingThread.id == run.thread_id).first()
            if thread and thread.last_run_id == run.id:
                thread.status = "idle"
                thread.updated_at = _now()
            self._append_event_db(db, run.thread_id, run.id, "cancelled", {"reason": reason})
            return
        if run.status == "starting" and not run.tmux_session and run.id not in self._active_processes:
            run.status = "cancelled"
            run.finished_at = _now()
            run.error = reason
            thread = db.query(CodingThread).filter(CodingThread.id == run.thread_id).first()
            if thread and thread.last_run_id == run.id:
                thread.status = "idle"
                thread.updated_at = _now()
            self._append_event_db(db, run.thread_id, run.id, "cancelled", {"reason": reason})
            return
        run.status = "stopping"
        run.error = reason
        self._append_event_db(db, run.thread_id, run.id, "stopping", {"reason": reason})
        await self._signal_stop(run)

    async def stop_run(self, run_id: str, owner: str, reason: str = "stopped") -> CodingRun:
        async with self._lock:
            db = SessionLocal()
            try:
                run = (
                    db.query(CodingRun)
                    .filter(CodingRun.id == run_id, CodingRun.owner == owner)
                    .first()
                )
                if not run:
                    raise CodingRuntimeError(404, "Run not found")
                await self._stop_run_db(db, run, reason=reason)
                db.commit()
                detached = self._detach_run(db, run.id)
            finally:
                db.close()
        await self.pump_queue()
        return detached

    async def send_stdin(self, run_id: str, owner: str, data: str) -> CodingRun:
        run = await self._get_owned_run(run_id, owner)
        metadata = _json_loads(run.metadata_json, {})
        if not run.tmux_session or metadata.get("stdin_supported") is False:
            raise CodingRuntimeError(409, "stdin is unsupported for this run")
        if run.status not in ACTIVE_STATUSES:
            raise CodingRuntimeError(409, "Run is not active")
        parts = data.split("\n")
        for idx, part in enumerate(parts):
            if part:
                await self._tmux_exec("send-keys", "-t", run.tmux_session, "-l", part, check=False)
            if idx < len(parts) - 1:
                await self._tmux_exec("send-keys", "-t", run.tmux_session, "Enter", check=False)
        await self.append_event(run.thread_id, run.id, "stdin", {"bytes": len(data.encode("utf-8"))})
        return await self._get_owned_run(run_id, owner)

    async def resize(self, run_id: str, owner: str, cols: int, rows: int) -> CodingRun:
        run = await self._get_owned_run(run_id, owner)
        metadata = _json_loads(run.metadata_json, {})
        if not run.tmux_session or metadata.get("resize_supported") is False:
            raise CodingRuntimeError(409, "resize is unsupported for this run")
        if run.status not in ACTIVE_STATUSES:
            raise CodingRuntimeError(409, "Run is not active")
        cols = max(20, min(int(cols), 400))
        rows = max(5, min(int(rows), 120))
        # resize-WINDOW (not resize-pane): the session is detached + window-size manual, so
        # the window must be resized for the pty/SIGWINCH to follow the UI pane.
        await self._tmux_exec(
            "resize-window",
            "-t",
            run.tmux_session,
            "-x",
            str(cols),
            "-y",
            str(rows),
            check=False,
        )
        await self.append_event(run.thread_id, run.id, "resize", {"cols": cols, "rows": rows})
        return await self._get_owned_run(run_id, owner)

    async def pump_queue(self) -> None:
        launch_ids: list[str] = []
        async with self._lock:
            db = SessionLocal()
            try:
                active_count = (
                    db.query(CodingRun)
                    .filter(CodingRun.status.in_(tuple(ACTIVE_STATUSES)))
                    .count()
                )
                slots = max(0, self.max_concurrent_runs() - active_count)
                if slots <= 0:
                    return
                queued = (
                    db.query(CodingRun)
                    .filter(CodingRun.status == "queued")
                    .order_by(CodingRun.queued_at.asc())
                    .limit(slots)
                    .all()
                )
                for run in queued:
                    run.status = "starting"
                    run.started_at = _now()
                    thread = db.query(CodingThread).filter(CodingThread.id == run.thread_id).first()
                    if thread:
                        thread.status = "starting"
                        thread.last_run_id = run.id
                        thread.updated_at = _now()
                    self._append_event_db(db, run.thread_id, run.id, "starting", {"run_id": run.id})
                    launch_ids.append(run.id)
                db.commit()
            finally:
                db.close()

        for run_id in launch_ids:
            if run_id in self._active_tasks:
                continue
            task = asyncio.create_task(self._launch_run(run_id))
            self._active_tasks[run_id] = task
            task.add_done_callback(lambda _task, _rid=run_id: self._active_tasks.pop(_rid, None))

    async def startup_reconcile(self) -> None:
        recoverable_ids: list[str] = []
        async with self._lock:
            db = SessionLocal()
            try:
                stale = (
                    db.query(CodingRun)
                    .filter(CodingRun.status.in_(tuple(ACTIVE_STATUSES)))
                    .all()
                )
                for run in stale:
                    recoverable = False
                    if run.tmux_session and run.run_dir and await self._tmux_has_session(run.tmux_session):
                        recoverable = True
                    if recoverable:
                        run.status = "running"
                        if not run.started_at:
                            run.started_at = _now()
                        metadata = _json_loads(run.metadata_json, {})
                        metadata["recovered_after_startup"] = True
                        run.metadata_json = _json_dumps(metadata)
                        thread = db.query(CodingThread).filter(CodingThread.id == run.thread_id).first()
                        if thread:
                            thread.status = "running"
                            thread.last_run_id = run.id
                            thread.updated_at = _now()
                        self._append_event_db(db, run.thread_id, run.id, "recovered", {"run_id": run.id})
                        recoverable_ids.append(run.id)
                    else:
                        run.status = "cancelled" if run.status == "stopping" else "failed"
                        run.finished_at = _now()
                        run.error = "Run was active during startup and no recoverable tmux session was found"
                        thread = db.query(CodingThread).filter(CodingThread.id == run.thread_id).first()
                        if thread and thread.last_run_id == run.id:
                            thread.status = "idle"
                            thread.updated_at = _now()
                        self._append_event_db(
                            db,
                            run.thread_id,
                            run.id,
                            run.status,
                            {"error": run.error},
                        )
                db.commit()
            finally:
                db.close()

        for run_id in recoverable_ids:
            if run_id not in self._active_tasks:
                task = asyncio.create_task(self._tail_recovered_tmux_run(run_id))
                self._active_tasks[run_id] = task
                task.add_done_callback(lambda _task, _rid=run_id: self._active_tasks.pop(_rid, None))
        await self.pump_queue()

    async def shutdown(self) -> None:
        for task in list(self._active_tasks.values()):
            task.cancel()
        for proc in list(self._active_processes.values()):
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        self._active_tasks.clear()
        self._active_processes.clear()

    async def _launch_run(self, run_id: str) -> None:
        run = await self._get_run(run_id)
        if not run:
            return
        if self.tmux_available():
            await self._launch_tmux_run(run)
        else:
            await self._launch_subprocess_run(run)

    async def _launch_tmux_run(self, run: CodingRun) -> None:
        session = f"ody-code-{run.id.replace('-', '')[:16]}"
        run_dir = Path(run.run_dir or self.run_dir(run.id))
        log_path = Path(run.log_path or (run_dir / "raw.log"))
        exit_path = run_dir / "exit_code"
        script_path = run_dir / "run.sh"
        run_dir.mkdir(parents=True, exist_ok=True)
        # Run the harness DIRECTLY on the tmux pane's pty so its stdout/stderr stay a
        # real TTY (isatty() == True). Interactive agent CLIs (pi, codex, claude, a
        # login shell, …) need that or they detect "not a terminal" and refuse to render
        # their UI / exit immediately. Output is captured out-of-band via `tmux pipe-pane`
        # below — NOT by piping the command through `tee`, which would make stdout a pipe.
        #
        # We invoke the command through the user's INTERACTIVE LOGIN shell (`-ilc`) so it
        # inherits exactly the PATH/env the user has in their own terminal. Otherwise a
        # server launched outside a shell (GUI app / launchd) lacks rc-added dirs like
        # ~/.bun/bin, and `pi` (etc.) fails with "command not found".
        #
        # The short sleep lets pipe-pane attach before the program emits its first byte.
        user_shell = _user_login_shell()
        script_path.write_text(
            "#!/bin/bash\n"
            "set +e\n"
            f"cd {shlex.quote(run.cwd)}\n"
            "export TERM=\"${TERM:-xterm-256color}\"\n"
            "sleep 0.2\n"
            f"{shlex.quote(user_shell)} -ilc {shlex.quote(run.command)}\n"
            "EC=$?\n"
            f"printf '%s' \"$EC\" > {shlex.quote(str(exit_path))}\n"
            "exit \"$EC\"\n",
            encoding="utf-8",
        )
        script_path.chmod(0o700)

        db = SessionLocal()
        try:
            current = db.query(CodingRun).filter(CodingRun.id == run.id).first()
            thread = db.query(CodingThread).filter(CodingThread.id == run.thread_id).first()
            if not current or current.status not in {"starting", "running"}:
                return
            env = os.environ.copy()
            if thread:
                env.update(build_launch_env(db, current.owner, thread.model_endpoint_id, thread.model))
            run_meta = _json_loads(current.metadata_json, {})
            init_cols = int(run_meta.get("cols") or 120)
            init_rows = int(run_meta.get("rows") or 40)
            # Size the detached session to the UI pane up front.
            args = ["new-session", "-d", "-s", session, "-c", run.cwd, "-x", str(init_cols), "-y", str(init_rows)]
            for key, value in sorted(env.items()):
                if key.startswith(("OPENAI_", "OLLAMA_", "ANTHROPIC_")):
                    args.extend(["-e", f"{key}={value}"])
            # tmux runs the new-session command via `/bin/sh -c`, so the script path MUST be
            # shell-quoted — otherwise a space in the path (e.g. the packaged app's data dir
            # "~/Library/Application Support/Odysseus/…") word-splits and the pane dies
            # instantly with no output (run never executes).
            args.append(shlex.quote(str(script_path)))
        finally:
            db.close()

        proc = await self._tmux_exec(*args, check=False)
        if proc.returncode != 0:
            stderr = (proc.stderr or b"").decode(errors="replace").strip()
            await self._mark_failed(run.id, f"Failed to start tmux: {stderr or proc.returncode}")
            await self.pump_queue()
            return

        # Fix the window size to what we set (a detached session otherwise auto-sizes to
        # clients / a default) so resize-window from the UI takes effect.
        await self._tmux_exec("set-option", "-t", session, "window-size", "manual", check=False)

        # Capture the pane's raw output to the log (out-of-band, preserves the program's TTY).
        await self._start_pane_capture(session, log_path)

        async with self._lock:
            db = SessionLocal()
            try:
                current = db.query(CodingRun).filter(CodingRun.id == run.id).first()
                thread = db.query(CodingThread).filter(CodingThread.id == run.thread_id).first()
                if not current:
                    return
                metadata = _json_loads(current.metadata_json, {})
                metadata.update(
                    {
                        "tmux_available": True,
                        "stdin_supported": True,
                        "resize_supported": True,
                        "exit_path": str(exit_path),
                    }
                )
                current.status = "running"
                current.tmux_session = session
                current.metadata_json = _json_dumps(metadata)
                if thread:
                    thread.status = "running"
                    thread.last_run_id = current.id
                    thread.updated_at = _now()
                self._append_event_db(
                    db,
                    current.thread_id,
                    current.id,
                    "started",
                    {
                        "run_id": current.id,
                        "tmux_session": session,
                        "tmux_available": True,
                        "stdin_supported": True,
                        "resize_supported": True,
                    },
                )
                db.commit()
            finally:
                db.close()

        await self._tail_tmux_run(run.id, session, log_path, exit_path)

    async def _start_pane_capture(self, session: str, log_path: Path) -> None:
        """Mirror a tmux pane's raw output to ``log_path`` via ``pipe-pane``.

        This taps the pane's output stream (the program's stdout/stderr bytes, ANSI
        and all) without redirecting the program's own stdout — so the harness keeps a
        real TTY while we still stream everything to the UI.
        """
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        await self._tmux_exec(
            "pipe-pane",
            "-t",
            session,
            f"cat >> {shlex.quote(str(log_path))}",
            check=False,
        )

    async def _tail_recovered_tmux_run(self, run_id: str) -> None:
        run = await self._get_run(run_id)
        if not run or not run.tmux_session:
            return
        metadata = _json_loads(run.metadata_json, {})
        exit_path = Path(metadata.get("exit_path") or Path(run.run_dir or self.run_dir(run.id)) / "exit_code")
        # Re-establish output capture: the pipe-pane `cat` from the original launch died
        # when the server restarted, so without this a recovered run would stream nothing.
        await self._start_pane_capture(run.tmux_session, Path(run.log_path))
        await self._tail_tmux_run(run.id, run.tmux_session, Path(run.log_path), exit_path)

    async def _tail_tmux_run(self, run_id: str, session: str, log_path: Path, exit_path: Path) -> None:
        offset = 0
        try:
            while True:
                offset = await self._drain_log(run_id, log_path, offset)
                if exit_path.exists():
                    break
                if not await self._tmux_has_session(session):
                    break
                await asyncio.sleep(0.05)  # tight loop → low keystroke-echo latency
            offset = await self._drain_log(run_id, log_path, offset)
            exit_code = None
            if exit_path.exists():
                try:
                    exit_code = int(exit_path.read_text(encoding="utf-8").strip())
                except Exception:
                    exit_code = -1
            await self._finalize_run(run_id, exit_code)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("tmux run watcher failed: %s", exc)
            await self._mark_failed(run_id, str(exc))
        finally:
            await self.pump_queue()

    async def _drain_log(self, run_id: str, log_path: Path, offset: int) -> int:
        if not log_path.exists():
            return offset
        try:
            with log_path.open("rb") as f:
                f.seek(offset)
                chunk = f.read()
                new_offset = f.tell()
        except Exception:
            return offset
        if chunk:
            run = await self._get_run(run_id)
            if run:
                await self.append_event(
                    run.thread_id,
                    run.id,
                    "output",
                    {"stream": "stdout", "data": chunk.decode(errors="replace")},
                )
        return new_offset

    async def _launch_subprocess_run(self, run: CodingRun) -> None:
        metadata = _json_loads(run.metadata_json, {})
        metadata.update(
            {
                "tmux_available": False,
                "stdin_supported": False,
                "resize_supported": False,
                "runtime_note": "tmux unavailable; stdin and resize are unsupported for this run",
            }
        )
        db = SessionLocal()
        try:
            current = db.query(CodingRun).filter(CodingRun.id == run.id).first()
            thread = db.query(CodingThread).filter(CodingThread.id == run.thread_id).first()
            if not current or current.status not in {"starting", "running"}:
                return
            env = os.environ.copy()
            if thread:
                env.update(build_launch_env(db, current.owner, thread.model_endpoint_id, thread.model))
        finally:
            db.close()

        try:
            proc = await asyncio.create_subprocess_shell(
                run.command,
                cwd=run.cwd,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                executable="/bin/bash" if Path("/bin/bash").exists() else None,
            )
        except Exception as exc:
            await self._mark_failed(run.id, f"Failed to start subprocess: {exc}")
            await self.pump_queue()
            return

        self._active_processes[run.id] = proc
        async with self._lock:
            db = SessionLocal()
            try:
                current = db.query(CodingRun).filter(CodingRun.id == run.id).first()
                thread = db.query(CodingThread).filter(CodingThread.id == run.thread_id).first()
                if not current:
                    return
                current.status = "running"
                current.metadata_json = _json_dumps(metadata)
                if thread:
                    thread.status = "running"
                    thread.last_run_id = current.id
                    thread.updated_at = _now()
                self._append_event_db(
                    db,
                    current.thread_id,
                    current.id,
                    "started",
                    {
                        "run_id": current.id,
                        "tmux_available": False,
                        "stdin_supported": False,
                        "resize_supported": False,
                    },
                )
                db.commit()
            finally:
                db.close()

        async def _reader(stream: asyncio.StreamReader | None, name: str) -> None:
            if stream is None:
                return
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    return
                await self.append_event(
                    run.thread_id,
                    run.id,
                    "output",
                    {"stream": name, "data": chunk.decode(errors="replace")},
                )
                try:
                    if run.log_path:
                        with Path(run.log_path).open("ab") as f:
                            f.write(chunk)
                except Exception:
                    logger.debug("Failed to append subprocess output log", exc_info=True)

        try:
            readers = [
                asyncio.create_task(_reader(proc.stdout, "stdout")),
                asyncio.create_task(_reader(proc.stderr, "stderr")),
            ]
            exit_code = await proc.wait()
            await asyncio.gather(*readers, return_exceptions=True)
            await self._finalize_run(run.id, exit_code)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._mark_failed(run.id, str(exc))
        finally:
            self._active_processes.pop(run.id, None)
            await self.pump_queue()

    async def _finalize_run(self, run_id: str, exit_code: int | None) -> None:
        async with self._lock:
            db = SessionLocal()
            try:
                run = db.query(CodingRun).filter(CodingRun.id == run_id).first()
                if not run or run.status in FINISHED_STATUSES:
                    return
                final_status = "cancelled" if run.status == "stopping" else "exited"
                run.status = final_status
                run.exit_code = exit_code
                run.finished_at = _now()
                thread = db.query(CodingThread).filter(CodingThread.id == run.thread_id).first()
                if thread and thread.last_run_id == run.id:
                    thread.status = "idle"
                    thread.updated_at = _now()
                self._append_event_db(
                    db,
                    run.thread_id,
                    run.id,
                    final_status,
                    {"run_id": run.id, "exit_code": exit_code},
                )
                db.commit()
            finally:
                db.close()

    async def _mark_failed(self, run_id: str, error: str) -> None:
        async with self._lock:
            db = SessionLocal()
            try:
                run = db.query(CodingRun).filter(CodingRun.id == run_id).first()
                if not run or run.status in FINISHED_STATUSES:
                    return
                run.status = "failed"
                run.error = error
                run.finished_at = _now()
                thread = db.query(CodingThread).filter(CodingThread.id == run.thread_id).first()
                if thread and thread.last_run_id == run.id:
                    thread.status = "idle"
                    thread.updated_at = _now()
                self._append_event_db(db, run.thread_id, run.id, "failed", {"error": error})
                db.commit()
            finally:
                db.close()

    async def _signal_stop(self, run: CodingRun) -> None:
        if run.tmux_session:
            if not self.tmux_available():
                return
            await self._tmux_exec("send-keys", "-t", run.tmux_session, "C-c", check=False)
            await asyncio.sleep(0.5)
            if await self._tmux_has_session(run.tmux_session):
                await self._tmux_exec("kill-session", "-t", run.tmux_session, check=False)
            return
        proc = self._active_processes.get(run.id)
        if proc and proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass

    async def _tmux_has_session(self, session: str) -> bool:
        if not session or not self.tmux_available():
            return False
        result = await self._tmux_exec("has-session", "-t", session, check=False)
        return result.returncode == 0

    async def _tmux_exec(self, *args: str, check: bool = True):
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        result = type("TmuxResult", (), {"returncode": proc.returncode, "stdout": stdout, "stderr": stderr})()
        if check and proc.returncode != 0:
            raise RuntimeError(stderr.decode(errors="replace") or f"tmux exited {proc.returncode}")
        return result

    async def _get_run(self, run_id: str) -> CodingRun | None:
        db = SessionLocal()
        try:
            run = db.query(CodingRun).filter(CodingRun.id == run_id).first()
            return self._detach_run(db, run.id) if run else None
        finally:
            db.close()

    async def _get_owned_run(self, run_id: str, owner: str) -> CodingRun:
        db = SessionLocal()
        try:
            run = db.query(CodingRun).filter(CodingRun.id == run_id, CodingRun.owner == owner).first()
            if not run:
                raise CodingRuntimeError(404, "Run not found")
            return self._detach_run(db, run.id)
        finally:
            db.close()

    def _detach_run(self, db, run_id: str) -> CodingRun:
        run = db.query(CodingRun).filter(CodingRun.id == run_id).first()
        if not run:
            raise CodingRuntimeError(404, "Run not found")
        for attr in (
            "id",
            "thread_id",
            "owner",
            "harness_id",
            "status",
            "command",
            "cwd",
            "tmux_session",
            "run_dir",
            "log_path",
            "exit_code",
            "error",
            "queued_at",
            "started_at",
            "finished_at",
            "idempotency_key",
            "metadata_json",
        ):
            getattr(run, attr)
        db.expunge(run)
        return run

    def get_events(self, thread_id: str, owner: str, after_seq: int = 0) -> list[CodingThreadEvent]:
        db = SessionLocal()
        try:
            thread = db.query(CodingThread).filter(CodingThread.id == thread_id, CodingThread.owner == owner).first()
            if not thread:
                raise CodingRuntimeError(404, "Thread not found")
            rows = (
                db.query(CodingThreadEvent)
                .filter(CodingThreadEvent.thread_id == thread_id, CodingThreadEvent.seq > after_seq)
                .order_by(CodingThreadEvent.seq.asc())
                .all()
            )
            for row in rows:
                for attr in ("id", "thread_id", "run_id", "seq", "kind", "payload_json", "created_at"):
                    getattr(row, attr)
                db.expunge(row)
            return rows
        finally:
            db.close()

    def thread_has_open_runs(self, thread_id: str) -> bool:
        db = SessionLocal()
        try:
            return (
                db.query(CodingRun)
                .filter(CodingRun.thread_id == thread_id, CodingRun.status.in_(tuple(BLOCKING_STATUSES)))
                .count()
                > 0
            )
        finally:
            db.close()

    def run_is_open(self, run_id: str) -> bool:
        db = SessionLocal()
        try:
            run = db.query(CodingRun).filter(CodingRun.id == run_id).first()
            return bool(run and run.status in BLOCKING_STATUSES)
        finally:
            db.close()

    async def event_stream(
        self,
        *,
        thread_id: str,
        owner: str,
        after_seq: int = 0,
        request: Any = None,
        run_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        last_seq = after_seq
        idle_ticks = 0
        while True:
            if request is not None and await request.is_disconnected():
                return
            events = self.get_events(thread_id, owner, last_seq)
            if events:
                last_seq = max(last_seq, max(event.seq for event in events))
            if run_id:
                events = [event for event in events if event.run_id == run_id]
            for event in events:
                payload = _json_loads(event.payload_json, {})
                item = {
                    "id": event.id,
                    "thread_id": event.thread_id,
                    "run_id": event.run_id,
                    "seq": event.seq,
                    "kind": event.kind,
                    "payload": payload,
                    "created_at": _iso(event.created_at),
                }
                idle_ticks = 0
                yield item

            open_status = self.run_is_open(run_id) if run_id else self.thread_has_open_runs(thread_id)
            if not open_status:
                if idle_ticks >= 6:
                    return
                idle_ticks += 1
            await asyncio.sleep(0.05)  # tight SSE poll → low output latency for live typing

    def queue_snapshot(self, owner: str) -> dict[str, Any]:
        db = SessionLocal()
        try:
            queued = (
                db.query(CodingRun)
                .filter(CodingRun.owner == owner, CodingRun.status == "queued")
                .order_by(CodingRun.queued_at.asc())
                .all()
            )
            active = (
                db.query(CodingRun)
                .filter(CodingRun.owner == owner, CodingRun.status.in_(tuple(ACTIVE_STATUSES)))
                .order_by(CodingRun.started_at.asc())
                .all()
            )
            return {
                "max_concurrent": self.max_concurrent_runs(),
                "tmux_available": self.tmux_available(),
                "queued": [run.id for run in queued],
                "active": [run.id for run in active],
                "queued_count": len(queued),
                "active_count": len(active),
            }
        finally:
            db.close()


_SERVICE: CodingRuntimeService | None = None


def get_coding_runtime_service() -> CodingRuntimeService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = CodingRuntimeService()
    return _SERVICE
