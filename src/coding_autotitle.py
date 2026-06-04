"""Auto-title coding threads from the first task, so users don't name them manually.

Mirrors the chat session auto-title idea (src/chat_handler.update_session_name_if_needed)
but for coding threads and with an LLM summary: when a thread still has a default
placeholder title and the user/agent delivers a first task, we summarise it into a
short title using the configured *utility* model (resolve_endpoint("utility")). If no
utility model is available — or the call fails — we fall back to the same cheap
first-few-words heuristic the chat path uses.

The trigger fires fire-and-forget from the runtime (first ``send_stdin`` payload and
the first interactive WS input line), so titling never blocks a run. This module does
NOT import the runtime (avoids an import cycle); it persists the title and returns it,
and the runtime broadcasts the ``thread_titled`` event.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from core.database import CodingProject, CodingThread, SessionLocal

logger = logging.getLogger(__name__)

# Titles we treat as "still a placeholder" (safe to overwrite). Includes the static
# defaults and, dynamically, the owning project's name (create_thread falls back to it).
_DEFAULT_TITLES = {"", "untitled thread", "coding thread"}

# Minimum meaningful task length before we bother titling (skip a lone Enter, etc.).
_MIN_TASK_CHARS = 4
_MAX_TITLE_CHARS = 60


def _is_placeholder_title(thread: CodingThread, project_name: str | None) -> bool:
    current = (thread.title or "").strip().lower()
    if current in _DEFAULT_TITLES:
        return True
    return bool(project_name) and current == (project_name or "").strip().lower()


def _clean_title(raw: str) -> str:
    title = (raw or "").strip().splitlines()[0] if raw and raw.strip() else ""
    title = title.strip().strip('"').strip("'").strip()
    # Drop a leading "Title:" the model sometimes echoes.
    if title.lower().startswith("title:"):
        title = title[6:].strip()
    if len(title) > _MAX_TITLE_CHARS:
        title = title[:_MAX_TITLE_CHARS].rstrip() + "…"
    return title


def heuristic_title(text: str) -> str:
    """Cheap, model-free title: first few words of the task (cf. chat_handler)."""
    words = (text or "").split()
    derived = " ".join(words[:6]).strip()
    return _clean_title(derived)


async def _llm_title(text: str, owner: str | None) -> str:
    try:
        from src.endpoint_resolver import resolve_endpoint
        from src.llm_core import llm_call_async
    except Exception:
        return ""
    try:
        url, model, headers = resolve_endpoint("utility", owner=owner or "")
    except Exception:
        return ""
    if not url or not model:
        return ""
    messages = [
        {
            "role": "system",
            "content": (
                "You write a terse, specific 3-6 word title (Title Case) for a coding "
                "task. Output ONLY the title — no quotes, no surrounding punctuation, no "
                "trailing period, no explanation."
            ),
        },
        {"role": "user", "content": f"Coding task:\n{text[:2000]}\n\nTitle:"},
    ]
    try:
        out = await llm_call_async(
            url, model, messages, temperature=0.3, max_tokens=24, headers=headers, timeout=20
        )
    except Exception:
        logger.debug("auto-title LLM call failed", exc_info=True)
        return ""
    return _clean_title(out)


async def maybe_autotitle_thread(text: str, thread_id: str, owner: str | None) -> str | None:
    """If ``thread`` still has a placeholder title, derive one from ``text`` and persist
    it. Returns the new title (so the caller can broadcast an event) or None.

    Safe to call repeatedly: it no-ops once a real title exists (guarded by a
    ``title_autoset`` metadata flag + the placeholder check)."""
    task = (text or "").strip()
    if len(task) < _MIN_TASK_CHARS or not thread_id:
        return None
    db = SessionLocal()
    try:
        query = db.query(CodingThread).filter(CodingThread.id == thread_id)
        if owner:
            query = query.filter(CodingThread.owner == owner)
        thread = query.first()
        if thread is None:
            return None
        try:
            meta = json.loads(thread.metadata_json) if thread.metadata_json else {}
        except Exception:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        if meta.get("title_autoset") or meta.get("title_locked"):
            return None
        project = (
            db.query(CodingProject).filter(CodingProject.id == thread.project_id).first()
        )
        if not _is_placeholder_title(thread, project.name if project else None):
            # User (or a prior auto-title) already named it — leave it alone.
            return None

        title = await _llm_title(task, owner) or heuristic_title(task)
        if not title:
            return None

        # Re-load inside the same session right before write (cheap optimistic guard).
        thread.title = title
        meta["title_autoset"] = True
        thread.metadata_json = json.dumps(meta)
        thread.updated_at = datetime.utcnow()
        db.commit()
        return title
    except Exception:
        logger.debug("auto-title failed for thread %s", thread_id, exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
        return None
    finally:
        db.close()
