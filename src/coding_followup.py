"""Wake-on-coding-done: surface finished Code Station runs back into the chat
session that started them.

Mirrors src/bg_jobs.py's follow-up contract, but the durable source of truth is
the CodingRun row itself (status + finished_at) rather than an on-disk job file.
A run is "pending follow-up" when it has FINISHED, its thread is linked to a chat
session (CodingThread.session_id, auto-set when a coding thread is created from
chat — see do_manage_coding), and it has not yet been reported.

The reported flag lives in CodingRun.metadata_json so no schema migration is
needed and it survives restarts. The per-tick scan is bounded by finished_at so
it stays cheap as finished runs accumulate.

The actual agent re-invocation lives in src/bg_monitor.py (which already owns the
headless agent loop and the per-session is_active race guard); this module is the
data layer it drains.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Only look back this far so the per-tick scan stays cheap as finished runs
# accumulate. A run that finishes while the monitor is down still gets reported
# as long as the monitor comes back within this window.
_LOOKBACK = timedelta(hours=24)
_REPORTED_KEY = "reported_to_chat"
_MAX_LOG_TAIL = 4000


def _load_meta(run) -> dict:
    if not getattr(run, "metadata_json", None):
        return {}
    try:
        value = json.loads(run.metadata_json)
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def pending_coding_followups() -> list[dict]:
    """Finished, chat-initiated, not-yet-reported coding runs (recent window).

    A run is reported back to chat only if it carries a ``report_to_session``
    marker in its metadata — set by do_manage_coding's run_thread from the
    TRUSTED invoking session. This means:
      * UI-initiated runs (no marker) never inject unsolicited turns into a chat;
      * the report always goes to the session that actually started the run,
        independent of the thread's primary session link;
      * the target can never be forged by the agent (the marker is the trusted
        caller session, not agent JSON).

    Returns lightweight dicts (no raw run_dir/tmux paths) so callers never leak
    filesystem internals into chat. The terminal log tail is read on demand in
    build_followup_inject, server-side only.
    """
    from core.database import CodingRun, CodingThread, SessionLocal
    from src.coding_runtime import FINISHED_STATUSES

    cutoff = datetime.utcnow() - _LOOKBACK
    out: list[dict] = []
    db = SessionLocal()
    try:
        rows = (
            db.query(CodingRun, CodingThread)
            .join(CodingThread, CodingThread.id == CodingRun.thread_id)
            .filter(CodingRun.status.in_(tuple(FINISHED_STATUSES)))
            .filter(CodingRun.finished_at.isnot(None))
            .filter(CodingRun.finished_at >= cutoff)
            .order_by(CodingRun.finished_at.asc())
            .all()
        )
        for run, thread in rows:
            meta = _load_meta(run)
            report_session = str(meta.get("report_to_session") or "").strip()
            if not report_session:
                continue  # not a chat-initiated run — don't surface in chat
            if meta.get(_REPORTED_KEY):
                continue
            out.append(
                {
                    "run_id": run.id,
                    "session_id": report_session,
                    "thread_id": thread.id,
                    "thread_title": thread.title,
                    "status": run.status,
                    "exit_code": run.exit_code,
                    "error": run.error,
                    "log_path": run.log_path,
                    "harness_id": run.harness_id,
                }
            )
    except Exception as exc:  # defensive — a bad row must not wedge the monitor
        logger.warning("pending_coding_followups query failed: %s", exc)
    finally:
        db.close()
    return out


def mark_coding_followed_up(run_id: str) -> None:
    """Persist the reported flag so a run is reported into chat exactly once."""
    from core.database import CodingRun, SessionLocal

    db = SessionLocal()
    try:
        run = db.query(CodingRun).filter(CodingRun.id == run_id).first()
        if not run:
            return
        meta = _load_meta(run)
        meta[_REPORTED_KEY] = True
        meta["reported_at"] = datetime.utcnow().isoformat()
        run.metadata_json = json.dumps(meta)
        db.commit()
    except Exception as exc:
        logger.warning("mark_coding_followed_up(%s) failed: %s", run_id, exc)
        db.rollback()
    finally:
        db.close()


def _log_tail(log_path: str, limit: int = _MAX_LOG_TAIL) -> str:
    if not log_path:
        return ""
    try:
        from pathlib import Path

        p = Path(log_path)
        if not p.exists():
            return ""
        data = p.read_bytes()
        if len(data) > limit:
            data = data[-limit:]
        return data.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def build_followup_inject(rec: dict) -> str:
    """The synthetic user turn the chat agent continues from when a run finishes."""
    status = rec.get("status") or "finished"
    rid = rec.get("run_id") or ""
    title = rec.get("thread_title") or rec.get("thread_id") or "(untitled)"
    exit_code = rec.get("exit_code")
    err = rec.get("error")
    tail = _log_tail(rec.get("log_path") or "")

    head = f'[Coding run {rid} on thread "{title}" {status}'
    if exit_code is not None:
        head += f", exit {exit_code}"
    head += "]"

    parts = [head]
    if err:
        parts.append(f"Error: {err}")
    if tail:
        parts.append("Final terminal output (tail):\n" + tail)
    parts.append(
        "A coding agent you started has finished. Summarize for the user what it "
        "did and the outcome, concisely. If it failed or seems to need input, say "
        "what's needed and offer to help. Call manage_coding (read_run with this "
        "run_id) if you need more detail before answering."
    )
    return "\n\n".join(parts)
