"""Runtime and queue manager for coding station terminal runs."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
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
from src.coding_model_config import build_launch_env, thread_model_config
from src.coding_provider_launch import build_coding_agent_launch_plan
from src.coding_provider_bridge import (
    PROVIDER_BRIDGE_ENV_KEYS,
    ProviderBridgeScope,
    get_provider_bridge_service,
    scripts_dir,
    with_beads_on_path,
    with_scripts_on_path,
)
from src.coding_pty_bridge import CodingPtyBridge, dtach_bin
from src.coding_task_slots import get_task_slot_service
from src.settings import get_setting

logger = logging.getLogger(__name__)

RUN_ROOT = Path(DATA_DIR) / "coding_runs"
ACTIVE_STATUSES = {"starting", "running", "stopping"}
BLOCKING_STATUSES = {"queued", "starting", "running", "stopping"}
FINISHED_STATUSES = {"exited", "failed", "cancelled"}
# The provider-bridge keys plus the ody control-plane keys (socket + owner) that
# create_run_env injects so an `ody` run inside a pane targets the right socket.
# These must survive _drop_private_odysseus_env (which keeps only ODYSSEUS_* keys
# in this allowlist). Kept separate from PROVIDER_BRIDGE_ENV_KEYS so the
# odysseus-tool `env` status command's reported key set is unchanged.
PROVIDER_ODYSSEUS_ENV_KEYS = frozenset(PROVIDER_BRIDGE_ENV_KEYS) | {
    "ODYSSEUS_ODY_SOCKET",
    "ODYSSEUS_OWNER",
}


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


def _sanitize_base_launch_env(env: dict[str, str]) -> dict[str, str]:
    return {key: value for key, value in env.items() if not key.startswith("ODYSSEUS_")}


def _drop_private_odysseus_env(env: dict[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in env.items()
        if not key.startswith("ODYSSEUS_") or key in PROVIDER_ODYSSEUS_ENV_KEYS
    }


class CodingRuntimeService:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._active_tasks: dict[str, asyncio.Task] = {}
        self._active_processes: dict[str, asyncio.subprocess.Process] = {}
        self._deleting_threads: set[tuple[str, str]] = set()
        self._launching_runs: dict[str, tuple[str, str]] = {}
        # Per-run capture-logger tasks (dtach clients that tee output to raw.log).
        self._capture_tasks: dict[str, asyncio.Task] = {}
        self._provider_bridge = get_provider_bridge_service()
        self._pty_bridge = CodingPtyBridge(
            backend_available=self.dtach_available,
            get_owned_run=self._get_owned_run,
            finished_statuses=FINISHED_STATUSES,
            logger=logger,
        )

    def max_concurrent_runs(self) -> int:
        try:
            value = int(get_setting("coding_max_concurrent_threads", 2) or 2)
        except Exception:
            value = 2
        return max(1, value)

    def dtach_available(self) -> bool:
        return dtach_bin() is not None

    def run_dir(self, run_id: str) -> Path:
        return RUN_ROOT / run_id

    def _session_name(self, run_id: str) -> str:
        return f"ody-code-{run_id.replace('-', '')[:16]}"

    def _issue_provider_bridge_env(self, run: CodingRun, thread: CodingThread | None) -> dict[str, str]:
        scope = ProviderBridgeScope(
            owner=run.owner or "",
            project_id=(thread.project_id if thread else "") or "",
            thread_id=run.thread_id or "",
            run_id=run.id or "",
        )
        return self._provider_bridge.create_run_env(scope)

    def _thread_delete_key(self, owner: str | None, thread_id: str | None) -> tuple[str, str]:
        return ((owner or "").strip(), (thread_id or "").strip())

    def _build_launch_environment(
        self,
        db,
        run: CodingRun,
        thread: CodingThread | None,
    ) -> tuple[dict[str, str], dict[str, Any]]:
        env = _sanitize_base_launch_env(os.environ.copy())
        if thread:
            env.update(build_launch_env(db, run.owner, thread.model_endpoint_id, thread.model))
        provider_env = self._issue_provider_bridge_env(run, thread)
        env.update(provider_env)
        env = _drop_private_odysseus_env(env)
        env = with_scripts_on_path(env)
        env = with_beads_on_path(env)
        return env, self._provider_bridge.metadata_for_env(provider_env)

    def _revoke_run_provider_credentials(self, run_id: str, db=None) -> None:
        try:
            self._provider_bridge.revoke_run_token(run_id=run_id)
        except Exception:
            logger.debug("Failed to revoke provider bridge token for coding run %s", run_id, exc_info=True)
        # A finished/failed run must never strand its task slots (the harness may
        # die mid-call before its release hook fires). release_run is synchronous
        # and safe to call from this teardown path on the event loop.
        try:
            get_task_slot_service().release_run(run_id)
        except Exception:
            logger.debug("Failed to release task slots for coding run %s", run_id, exc_info=True)
        # The run is being torn down — no harness hook will fire its terminal state.
        # Best-effort mark the thread's agent_state "done" and broadcast it on the
        # event stream (the event row IS the broadcast; event_stream polls by seq).
        self._emit_done_agent_state(run_id, db)

    def _emit_done_agent_state(self, run_id: str, db=None) -> None:
        """Persist a terminal ``done`` agent_state + broadcast event for a run.

        When the caller already holds an open session (the ``*_db`` teardown
        helpers, which may have appended other event rows on it), reuse it so the
        ``done`` event's seq is allocated from the same in-session view and the
        caller's later ``commit`` stays consistent (avoids a ``(thread_id, seq)``
        UNIQUE collision). Otherwise open a short-lived session and commit here.
        """
        try:
            owns_session = db is None
            session = db if db is not None else SessionLocal()
            try:
                run = session.query(CodingRun).filter(CodingRun.id == run_id).first()
                thread_id = run.thread_id if run else None
                if thread_id:
                    thread = (
                        session.query(CodingThread).filter(CodingThread.id == thread_id).first()
                    )
                    if thread is not None:
                        thread.agent_state = "done"
                        thread.state_changed_at = _now()
                    self._append_event_db(
                        session, thread_id, run_id, "agent_state_changed", {"state": "done"}
                    )
                    if owns_session:
                        session.commit()
            finally:
                if owns_session:
                    session.close()
        except Exception:
            logger.debug("Failed to emit done agent_state for coding run %s", run_id, exc_info=True)

    def _append_event_db(
        self,
        db,
        thread_id: str,
        run_id: str | None,
        kind: str,
        payload: dict[str, Any] | None = None,
    ) -> CodingThreadEvent:
        # Sessions are autoflush=False, so flush any pending (not-yet-committed)
        # event rows before computing the next seq. Without this, two appends to
        # the same session before a commit would both read the same max(seq) and
        # collide on the (thread_id, seq) UNIQUE constraint.
        try:
            db.flush()
        except Exception:
            pass
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
        session_id: str | None = None,
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
                project_id = project.id
                if self._thread_delete_key(owner, thread.id) in self._deleting_threads:
                    raise CodingRuntimeError(409, "Thread deletion is in progress")

                # Back-fill the originating chat session link if this thread
                # has none yet, so a run started against a pre-existing
                # unlinked thread still reports back to the right conversation.
                if session_id and not (thread.session_id or "").strip():
                    thread.session_id = session_id.strip()

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

                requested_command = (command or "").strip()
                metadata_command = str(metadata.get("command") or "").strip()
                default_harness_command = not requested_command and not metadata_command
                base_command = build_harness_command(selected_harness, command, metadata)
                run_cwd = _safe_cwd(cwd, thread.cwd or project.root_path)
                model_config = thread_model_config(db, thread)
                launch_plan = build_coding_agent_launch_plan(
                    harness_id=selected_harness,
                    base_command=base_command,
                    model=model_config.get("model") or thread.model,
                    endpoint_url=model_config.get("endpoint_url") or "",
                    run_dir=run_dir,
                    default_harness_command=default_harness_command,
                )
                built_command = launch_plan.command
                run_metadata = {
                    "requested_command": command or "",
                    "harness_metadata": metadata,
                    "agent_launch": launch_plan.metadata,
                    "model_config": {
                        "endpoint_id": model_config.get("endpoint_id") or "",
                        "endpoint_url": model_config.get("endpoint_url") or "",
                        "model": model_config.get("model") or "",
                        "source": model_config.get("source") or "",
                    },
                    "state_path": str(state_path),
                    # Launch the pty at the UI pane's real size so the harness renders at the
                    # right width from the first frame (no 80-col wrap / full-redraw churn).
                    "cols": max(20, min(int(cols), 400)) if cols else 120,
                    "rows": max(5, min(int(rows), 120)) if rows else 40,
                }
                # Per-RUN wake-on-done target: which chat session to report this
                # run's completion into. Set ONLY when the run is started by the
                # chat tool (do_manage_coding passes the trusted caller session);
                # UI-initiated runs leave it unset, so they never inject
                # unsolicited "your coding agent finished" turns into a chat.
                if session_id and session_id.strip():
                    run_metadata["report_to_session"] = session_id.strip()
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
                        "model": model_config.get("model") or "",
                        "model_endpoint_id": model_config.get("endpoint_id") or "",
                        "agent_launch": launch_plan.metadata,
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

        await self.pump_queue(owner=owner, project_id=project_id)
        return detached

    def _cancel_run_db(self, db, run: CodingRun, reason: str) -> None:
        if run.status in FINISHED_STATUSES:
            self._revoke_run_provider_credentials(run.id, db)
            return
        run.status = "cancelled"
        run.finished_at = _now()
        run.error = reason
        thread = db.query(CodingThread).filter(CodingThread.id == run.thread_id).first()
        if thread and thread.last_run_id == run.id:
            thread.status = "idle"
            thread.updated_at = _now()
        self._append_event_db(db, run.thread_id, run.id, "cancelled", {"reason": reason})
        self._revoke_run_provider_credentials(run.id, db)

    async def _stop_run_db(self, db, run: CodingRun, reason: str = "stopped") -> None:
        if run.status in FINISHED_STATUSES:
            self._revoke_run_provider_credentials(run.id, db)
            return
        if run.status == "queued":
            self._cancel_run_db(db, run, reason)
            return
        if run.status == "starting" and not run.tmux_session and run.id not in self._active_processes:
            self._cancel_run_db(db, run, reason)
            return
        run.status = "stopping"
        run.error = reason
        self._append_event_db(db, run.thread_id, run.id, "stopping", {"reason": reason})
        await self._signal_stop(run)

    async def _cleanup_run_for_thread_delete_db(self, db, run: CodingRun, reason: str) -> None:
        if run.status in FINISHED_STATUSES:
            self._revoke_run_provider_credentials(run.id, db)
            return
        if run.id in self._launching_runs:
            raise CodingRuntimeError(409, f"Run {run.id} is still launching; retry deletion")
        if run.status == "queued":
            self._cancel_run_db(db, run, reason)
            return
        if run.status == "starting" and not run.tmux_session and run.id not in self._active_processes:
            await self._signal_stop(run, strict=True, include_unpersisted_tmux=True)
            self._cancel_run_db(db, run, reason)
            return
        await self._signal_stop(run, strict=True, include_unpersisted_tmux=True)
        self._cancel_run_db(db, run, reason)

    async def delete_thread(self, thread_id: str, owner: str) -> None:
        key = self._thread_delete_key(owner, thread_id)
        async with self._lock:
            if key in self._deleting_threads:
                raise CodingRuntimeError(409, "Thread deletion is already in progress")
            if key in self._launching_runs.values():
                raise CodingRuntimeError(409, "Thread has a run launching; retry deletion")
            self._deleting_threads.add(key)
            db = SessionLocal()
            try:
                thread = (
                    db.query(CodingThread)
                    .filter(CodingThread.id == thread_id, CodingThread.owner == owner)
                    .first()
                )
                if not thread:
                    raise CodingRuntimeError(404, "Thread not found")
                thread.status = "stopping"
                thread.updated_at = _now()
                runs = (
                    db.query(CodingRun)
                    .filter(
                        CodingRun.thread_id == thread.id,
                        CodingRun.status.in_(tuple(BLOCKING_STATUSES)),
                    )
                    .order_by(CodingRun.queued_at.asc())
                    .all()
                )
                for run in runs:
                    await self._cleanup_run_for_thread_delete_db(db, run, "thread deleted")
                db.delete(thread)
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()
                self._deleting_threads.discard(key)
        await self.pump_queue(owner=owner)

    async def _begin_run_launch(self, run: CodingRun) -> bool:
        async with self._lock:
            db = SessionLocal()
            try:
                current = db.query(CodingRun).filter(CodingRun.id == run.id).first()
                if not current or current.status not in {"starting", "running"}:
                    return False
                key = self._thread_delete_key(current.owner, current.thread_id)
                if key in self._deleting_threads:
                    self._cancel_run_db(db, current, "thread deletion in progress")
                    db.commit()
                    return False
                self._launching_runs[current.id] = key
                return True
            finally:
                db.close()

    async def _end_run_launch(self, run_id: str) -> None:
        async with self._lock:
            self._launching_runs.pop(run_id, None)

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
        await self.pump_queue(owner=owner)
        return detached

    def active_run_count(self, *, owner: str | None = None) -> int:
        """Runs holding (or about to hold) a live session: starting/running/stopping.

        Backs the desktop quit prompt — "would quitting orphan work?" Queued runs
        are excluded (no process yet; they resurvive a restart untouched) and so are
        finished runs. ``owner=None`` counts across all owners (the desktop is a
        single-machine, typically single-user deployment).
        """
        db = SessionLocal()
        try:
            query = db.query(CodingRun).filter(CodingRun.status.in_(tuple(ACTIVE_STATUSES)))
            if owner is not None:
                query = query.join(CodingThread, CodingThread.id == CodingRun.thread_id).filter(
                    CodingRun.owner == owner, CodingThread.owner == owner
                )
            return query.count()
        finally:
            db.close()

    async def stop_all_runs(self, *, owner: str | None = None, reason: str = "app shutdown") -> int:
        """Stop every active or queued run (optionally scoped to one owner).

        Backs the desktop "Quit Everything" path: a machine-level wind-down that
        should leave nothing orphaned. Each run is stopped exactly like ``stop_run``
        (graceful SIGINT then kill of its dtach session), which leaves a running run
        in ``stopping``; if the backend exits before the monitor task finalizes it,
        ``startup_reconcile`` reconciles ``stopping`` → ``cancelled`` on next launch.
        Queued / not-yet-attached runs are cancelled outright. Returns the number of
        runs that were active/queued when the stop was issued. ``owner=None`` stops
        across all owners. Deliberately does NOT pump the queue afterwards — the whole
        point is to wind down, not relaunch.
        """
        stopped = 0
        async with self._lock:
            db = SessionLocal()
            try:
                query = db.query(CodingRun).filter(CodingRun.status.in_(tuple(BLOCKING_STATUSES)))
                if owner is not None:
                    query = query.join(CodingThread, CodingThread.id == CodingRun.thread_id).filter(
                        CodingRun.owner == owner, CodingThread.owner == owner
                    )
                runs = query.order_by(CodingRun.queued_at.asc()).all()
                for run in runs:
                    try:
                        await self._stop_run_db(db, run, reason=reason)
                        stopped += 1
                    except Exception:
                        logger.exception("stop_all_runs: failed to stop run %s", run.id)
                db.commit()
            finally:
                db.close()
        return stopped

    async def send_stdin(self, run_id: str, owner: str, data: str) -> CodingRun:
        run = await self._get_owned_run(run_id, owner)
        metadata = _json_loads(run.metadata_json, {})
        if not run.tmux_session or metadata.get("stdin_supported") is False:
            raise CodingRuntimeError(409, "stdin is unsupported for this run")
        if run.status not in ACTIVE_STATUSES:
            raise CodingRuntimeError(409, "Run is not active")
        # Inject the bytes into the session without a full attach (dtach -p). Newlines
        # map to CR (terminal Enter). The live keystroke path is the WebSocket bridge;
        # this HTTP route is a fallback for programmatic input.
        await self._pty_bridge.send_input(run.tmux_session, data.replace("\n", "\r").encode("utf-8"))
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
        # The authoritative resize is the WebSocket bridge: the browser sets its PTY
        # winsize and dtach forwards a clean SIGWINCH to the program. There is no
        # off-client dtach resize, so this HTTP route only records the intent.
        await self.append_event(run.thread_id, run.id, "resize", {"cols": cols, "rows": rows})
        return await self._get_owned_run(run_id, owner)

    def _scoped_run_query(self, db, *, owner: str | None = None, project_id: str | None = None):
        query = db.query(CodingRun).join(CodingThread, CodingThread.id == CodingRun.thread_id)
        if owner is not None:
            query = query.filter(CodingRun.owner == owner, CodingThread.owner == owner)
        if project_id is not None:
            query = query.filter(CodingThread.project_id == project_id)
        return query

    def _queued_owners_db(self, db, *, owner: str | None = None) -> list[str]:
        # Owners that have queued work, oldest queue entry first. Concurrency is a
        # single per-owner pool shared across ALL of that owner's projects, so we
        # budget by owner (not by project): a queued run in any project competes for
        # the same slots, and global FIFO order decides which one starts next.
        query = (
            db.query(CodingRun.owner)
            .join(CodingThread, CodingThread.id == CodingRun.thread_id)
            .filter(CodingRun.status == "queued")
        )
        if owner is not None:
            query = query.filter(CodingRun.owner == owner, CodingThread.owner == owner)
        rows = (
            query.group_by(CodingRun.owner)
            .order_by(func.min(CodingRun.queued_at).asc())
            .all()
        )
        return [row[0] for row in rows if row[0] is not None]

    async def pump_queue(self, *, owner: str | None = None, project_id: str | None = None) -> None:
        launch_ids: list[str] = []
        async with self._lock:
            db = SessionLocal()
            try:
                await self._reconcile_stale_active_runs_db(db, owner=owner, project_id=project_id)
                db.flush()
                # Terminals are no longer capped — concurrency is enforced per LLM
                # *task* via per-endpoint task slots (src/coding_task_slots.py), not by
                # limiting how many terminals run. So launch every queued run immediately.
                for scope_owner in self._queued_owners_db(db, owner=owner):
                    queued = (
                        self._scoped_run_query(db, owner=scope_owner)
                        .filter(CodingRun.status == "queued")
                        .order_by(CodingRun.queued_at.asc())
                        .all()
                    )
                    for run in queued:
                        thread = db.query(CodingThread).filter(CodingThread.id == run.thread_id).first()
                        if thread and self._thread_delete_key(run.owner, thread.id) in self._deleting_threads:
                            continue
                        run.status = "starting"
                        run.started_at = _now()
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
                    if run.tmux_session and run.run_dir and await self._pty_bridge.has_session(run.tmux_session):
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
                        if not run.tmux_session:
                            derived_session = self._session_name(run.id)
                            if await self._pty_bridge.has_session(derived_session):
                                await self._pty_bridge.kill_session(derived_session)
                        run.status = "cancelled" if run.status == "stopping" else "failed"
                        run.finished_at = _now()
                        run.error = "Run was active during startup and no recoverable session was found"
                        self._revoke_run_provider_credentials(run.id, db)
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

    async def reconcile_stale_active_runs(
        self,
        owner: str | None = None,
        project_id: str | None = None,
    ) -> list[str]:
        async with self._lock:
            db = SessionLocal()
            try:
                stale_ids = await self._reconcile_stale_active_runs_db(db, owner=owner, project_id=project_id)
                db.commit()
                return stale_ids
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

    async def _reconcile_stale_active_runs_db(
        self,
        db,
        owner: str | None = None,
        project_id: str | None = None,
    ) -> list[str]:
        query = self._scoped_run_query(db, owner=owner, project_id=project_id).filter(
            CodingRun.status.in_(tuple(ACTIVE_STATUSES))
        )
        active_runs = query.all()
        stale_ids: list[str] = []
        for run in active_runs:
            if run.id in self._launching_runs or run.id in self._active_tasks or run.id in self._active_processes:
                continue

            has_runtime_resource = False
            if run.tmux_session:
                has_runtime_resource = self.dtach_available() and await self._pty_bridge.has_session(run.tmux_session)
            if has_runtime_resource:
                continue

            final_status = "cancelled" if run.status == "stopping" else "failed"
            run.status = final_status
            run.finished_at = _now()
            run.error = (
                "Run was marked active but no live runtime resource was found"
                if not run.tmux_session
                else f"Run was marked active but tmux session {run.tmux_session} no longer exists"
            )
            self._revoke_run_provider_credentials(run.id, db)
            thread = db.query(CodingThread).filter(CodingThread.id == run.thread_id).first()
            if thread and thread.last_run_id == run.id:
                thread.status = "idle"
                thread.updated_at = _now()
            self._append_event_db(
                db,
                run.thread_id,
                run.id,
                final_status,
                {"error": run.error, "reconciled": True},
            )
            stale_ids.append(run.id)
        return stale_ids

    async def shutdown(self) -> None:
        for task in list(self._active_tasks.values()):
            task.cancel()
        for task in list(self._capture_tasks.values()):
            task.cancel()
        for proc in list(self._active_processes.values()):
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        for run_id in set(self._active_tasks) | set(self._active_processes):
            self._revoke_run_provider_credentials(run_id)
        self._active_tasks.clear()
        self._capture_tasks.clear()
        self._active_processes.clear()

    async def _launch_run(self, run_id: str) -> None:
        run = await self._get_run(run_id)
        if not run:
            return
        if self.dtach_available():
            await self._launch_dtach_run(run)
        else:
            await self._launch_subprocess_run(run)

    async def _launch_dtach_run(self, run: CodingRun) -> None:
        session = self._session_name(run.id)
        run_dir = Path(run.run_dir or self.run_dir(run.id))
        log_path = Path(run.log_path or (run_dir / "raw.log"))
        exit_path = run_dir / "exit_code"
        script_path = run_dir / "run.sh"
        run_dir.mkdir(parents=True, exist_ok=True)
        # Run the harness DIRECTLY on the dtach session's pty so its stdout/stderr stay a
        # real TTY (isatty() == True). Interactive agent CLIs (pi, codex, claude, a
        # login shell, …) need that or they detect "not a terminal" and refuse to render
        # their UI / exit immediately. Output is captured out-of-band by the capture
        # logger (a persistent dtach client teeing to raw.log) — NOT by piping the command
        # through `tee`, which would make stdout a pipe.
        #
        # We invoke the command through the user's INTERACTIVE LOGIN shell (`-ilc`) so it
        # inherits exactly the PATH/env the user has in their own terminal. Otherwise a
        # server launched outside a shell (GUI app / launchd) lacks rc-added dirs like
        # ~/.bun/bin, and `pi` (etc.) fails with "command not found".
        #
        # The short sleep lets the capture logger attach before the program emits its
        # first byte (and gives the 0x0 dtach pty its initial size).
        user_shell = _user_login_shell()
        script_dir = str(scripts_dir())
        launch_command = f"export PATH={shlex.quote(script_dir)}:$PATH; {run.command}"
        script_path.write_text(
            "#!/bin/bash\n"
            "set +e\n"
            f"cd {shlex.quote(run.cwd)}\n"
            "export TERM=\"${TERM:-xterm-256color}\"\n"
            f"export PATH={shlex.quote(script_dir)}:\"$PATH\"\n"
            "sleep 0.2\n"
            f"{shlex.quote(user_shell)} -ilc {shlex.quote(launch_command)}\n"
            "EC=$?\n"
            f"printf '%s' \"$EC\" > {shlex.quote(str(exit_path))}\n"
            "exit \"$EC\"\n",
            encoding="utf-8",
        )
        script_path.chmod(0o700)

        if not await self._begin_run_launch(run):
            return
        try:
            db = SessionLocal()
            try:
                current = db.query(CodingRun).filter(CodingRun.id == run.id).first()
                thread = db.query(CodingThread).filter(CodingThread.id == run.thread_id).first()
                if not current or current.status not in {"starting", "running"}:
                    return
                env, provider_metadata = self._build_launch_environment(db, current, thread)
                run_meta = _json_loads(current.metadata_json, {})
                run_meta["provider_bridge"] = provider_metadata
                current.metadata_json = _json_dumps(run_meta)
                db.commit()
                logger.info(
                    "Coding run %s launch configured: harness=%s model=%s tools=%s",
                    current.id,
                    current.harness_id,
                    (run_meta.get("agent_launch") or {}).get("configured_model") or "",
                    ((run_meta.get("agent_launch") or {}).get("provider_tools") or {}).get("mode") or "",
                )
                init_cols = int(run_meta.get("cols") or 120)
                init_rows = int(run_meta.get("rows") or 40)
            finally:
                db.close()

            # Create the detached dtach session running the harness. dtach passes the
            # program's raw PTY straight through (no emulation/scrollback/reflow of its
            # own), so the browser terminal owns scrollback and resize is a clean
            # SIGWINCH — see src/coding_pty_bridge.py for why tmux was removed. The full
            # (already-sanitized) launch env is handed to dtach directly, which the
            # program inherits — no per-var allow-list / `-e` plumbing needed.
            result = await self._pty_bridge.create_session(session, script_path, env, cwd=run.cwd)
            if result.returncode != 0:
                stderr = (result.stderr or b"").decode(errors="replace").strip()
                await self._mark_failed(run.id, f"Failed to start dtach session: {stderr or result.returncode}")
                await self.pump_queue()
                return

            # Persistent capture logger: attaches at the initial pane size (sizing the
            # freshly-created 0x0 program) and tees ALL output to raw.log for the run's
            # lifetime — so output is captured even with no browser attached. Replaces
            # the old `tmux pipe-pane`.
            self._capture_tasks[run.id] = self._pty_bridge.start_capture(
                session, log_path, init_cols=init_cols, init_rows=init_rows
            )

            async with self._lock:
                db = SessionLocal()
                try:
                    current = db.query(CodingRun).filter(CodingRun.id == run.id).first()
                    thread = db.query(CodingThread).filter(CodingThread.id == run.thread_id).first()
                    if not current:
                        raise RuntimeError("Run disappeared during dtach launch")
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
                            "provider_bridge": metadata.get("provider_bridge") or {},
                            "agent_launch": metadata.get("agent_launch") or {},
                        },
                    )
                    db.commit()
                finally:
                    db.close()
        except Exception as exc:
            self._cancel_capture(run.id)
            try:
                if await self._pty_bridge.has_session(session):
                    await self._pty_bridge.kill_session(session)
            except Exception:
                logger.debug("Failed to clean up dtach session after launch error", exc_info=True)
            await self._mark_failed(run.id, f"Failed to start dtach session: {exc}")
            await self.pump_queue()
            return
        finally:
            await self._end_run_launch(run.id)

        await self._tail_run(run.id, session, log_path, exit_path)

    def _cancel_capture(self, run_id: str) -> None:
        task = self._capture_tasks.pop(run_id, None)
        if task and not task.done():
            task.cancel()

    async def _tail_recovered_tmux_run(self, run_id: str) -> None:
        run = await self._get_run(run_id)
        if not run or not run.tmux_session:
            return
        metadata = _json_loads(run.metadata_json, {})
        exit_path = Path(metadata.get("exit_path") or Path(run.run_dir or self.run_dir(run.id)) / "exit_code")
        # Re-establish output capture: the original capture logger died when the server
        # restarted, so without this a recovered run would stream nothing to raw.log.
        init_cols = int(metadata.get("cols") or 120)
        init_rows = int(metadata.get("rows") or 40)
        self._capture_tasks[run_id] = self._pty_bridge.start_capture(
            run.tmux_session, Path(run.log_path), init_cols=init_cols, init_rows=init_rows
        )
        await self._tail_run(run.id, run.tmux_session, Path(run.log_path), exit_path)

    async def _tail_run(self, run_id: str, session: str, log_path: Path, exit_path: Path) -> None:
        # Pure exit watcher: live output streams over the PTY/WebSocket (attach_pty) and
        # history is captured to raw.log by the capture logger, so there's no log-draining
        # / per-chunk DB events here — just detect completion and finalize.
        try:
            while True:
                if exit_path.exists():
                    break
                if not await self._pty_bridge.has_session(session):
                    break
                await asyncio.sleep(0.2)
            exit_code = None
            if exit_path.exists():
                try:
                    exit_code = int(exit_path.read_text(encoding="utf-8").strip())
                except Exception:
                    exit_code = -1
            self._cancel_capture(run_id)
            await self._finalize_run(run_id, exit_code)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("dtach run watcher failed: %s", exc)
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

        if not await self._begin_run_launch(run):
            return
        try:
            db = SessionLocal()
            try:
                current = db.query(CodingRun).filter(CodingRun.id == run.id).first()
                thread = db.query(CodingThread).filter(CodingThread.id == run.thread_id).first()
                if not current or current.status not in {"starting", "running"}:
                    return
                env, provider_metadata = self._build_launch_environment(db, current, thread)
                metadata["provider_bridge"] = provider_metadata
                current.metadata_json = _json_dumps(metadata)
                db.commit()
                logger.info(
                    "Coding run %s launch configured: harness=%s model=%s tools=%s",
                    current.id,
                    current.harness_id,
                    (metadata.get("agent_launch") or {}).get("configured_model") or "",
                    ((metadata.get("agent_launch") or {}).get("provider_tools") or {}).get("mode") or "",
                )
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
                        raise RuntimeError("Run disappeared during subprocess launch")
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
                            "provider_bridge": metadata.get("provider_bridge") or {},
                            "agent_launch": metadata.get("agent_launch") or {},
                        },
                    )
                    db.commit()
                finally:
                    db.close()
        except Exception as exc:
            proc = self._active_processes.pop(run.id, None)
            if proc and proc.returncode is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
            await self._mark_failed(run.id, f"Failed to start subprocess: {exc}")
            await self.pump_queue()
            return
        finally:
            await self._end_run_launch(run.id)

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
        self._revoke_run_provider_credentials(run_id)
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
        self._revoke_run_provider_credentials(run_id)
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

    async def _signal_stop(
        self,
        run: CodingRun,
        *,
        strict: bool = False,
        include_unpersisted_tmux: bool = False,
    ) -> None:
        session = run.tmux_session
        derived_session = False
        if not session and include_unpersisted_tmux and run.id not in self._active_processes:
            session = self._session_name(run.id)
            derived_session = True

        if session:
            self._cancel_capture(run.id)
            if not self.dtach_available():
                if strict and not derived_session:
                    raise CodingRuntimeError(500, f"Cannot stop run {run.id}: dtach is unavailable")
            elif await self._pty_bridge.has_session(session):
                if not derived_session:
                    # Graceful: deliver Ctrl-C to the program (SIGINT), give it a beat,
                    # then kill the dtach master if it's still alive.
                    await self._pty_bridge.send_input(session, b"\x03")
                    await asyncio.sleep(0.5)
                if await self._pty_bridge.has_session(session):
                    await self._pty_bridge.kill_session(session)
                if strict and await self._pty_bridge.has_session(session):
                    raise CodingRuntimeError(500, f"Failed to stop session for run {run.id}")
            return
        proc = self._active_processes.get(run.id)
        if proc:
            if proc.returncode is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
                if strict:
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        try:
                            proc.kill()
                        except ProcessLookupError:
                            pass
                        try:
                            await asyncio.wait_for(proc.wait(), timeout=2.0)
                        except asyncio.TimeoutError as exc:
                            raise CodingRuntimeError(500, f"Failed to stop subprocess for run {run.id}") from exc
            if strict:
                self._active_processes.pop(run.id, None)
            return
        if strict and run.status == "running":
            raise CodingRuntimeError(409, f"Cannot clean up active run {run.id}: no runtime resource is tracked")

    async def attach_pty(self, websocket, run_id: str, owner: str, cols: int = 120, rows: int = 40) -> None:
        await self._pty_bridge.attach_pty(websocket, run_id, owner, cols, rows)

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
        loop = asyncio.get_event_loop()
        started = loop.time()
        # Hard wall-clock cap so a permanently-stuck run (status that never closes)
        # can't keep a subscriber's stream + its 50ms DB poll alive forever. SSE
        # clients (EventSource) auto-reconnect with after_seq; the `ody events`
        # CLI can re-subscribe — so a 1h cap is transparent in normal use.
        max_seconds = 3600
        while True:
            if request is not None and await request.is_disconnected():
                return
            if (loop.time() - started) > max_seconds:
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

    def queue_snapshot(self, owner: str, project_id: str | None = None) -> dict[str, Any]:
        db = SessionLocal()
        try:
            # Counts are owner-global: the concurrency budget is one pool shared across
            # all of the owner's projects. project_id is accepted for callers but does
            # not narrow the totals (per-project attribution lives in the queue route).
            queued = (
                self._scoped_run_query(db, owner=owner)
                .filter(CodingRun.status == "queued")
                .order_by(CodingRun.queued_at.asc())
                .all()
            )
            active = (
                self._scoped_run_query(db, owner=owner)
                .filter(CodingRun.status.in_(tuple(ACTIVE_STATUSES)))
                .order_by(CodingRun.started_at.asc())
                .all()
            )
            return {
                "max_concurrent": self.max_concurrent_runs(),
                "project_id": project_id,
                "tmux_available": self.dtach_available(),
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
