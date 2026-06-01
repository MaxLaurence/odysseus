"""Per-endpoint task-slot limiter for Code Station harness LLM calls.

The Code Station no longer caps the number of terminals. Instead it caps the
number of concurrent *tasks* — actual LLM calls — per model endpoint. Harnesses
acquire a slot (via a hook) right before an LLM call and release it after; when
an endpoint's slots are full, further acquires queue (FIFO) until one frees.

Concurrency model: every method runs on uvicorn's single asyncio event loop, so
synchronous methods (release/heartbeat/snapshot/reclaim) are atomic with respect
to each other and need no lock — only ``acquire`` awaits (on the waiter future).
That also lets the runtime release a run's slots from synchronous teardown code
(``_revoke_run_provider_credentials``) without an await.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from src.settings import get_setting


# Lease safety net: a held slot whose holder never releases (crashed terminal,
# aborted request, missing release hook) is reclaimed after this many seconds.
# Long LLM calls should heartbeat to extend the lease past this.
TASK_SLOT_TTL_SECONDS = 180.0
DEFAULT_TASK_LIMIT = 2
# How long an acquire request blocks server-side before telling the hook to
# re-acquire. Kept under the tightest harness hook timeout (Claude
# UserPromptSubmit = 30s) so a long wait survives as repeated acquires.
DEFAULT_ACQUIRE_WAIT_SECONDS = 20.0


def _now() -> float:
    return time.monotonic()


@dataclass
class _Holder:
    slot_id: str
    endpoint_key: str
    run_id: str
    owner: str
    expires_at: float


@dataclass
class _Waiter:
    future: "asyncio.Future[str]"
    endpoint_key: str
    run_id: str
    owner: str
    created_at: float = field(default_factory=_now)


class TaskSlotService:
    """Process-singleton per-endpoint concurrency limiter for LLM calls."""

    def __init__(self) -> None:
        self._holders: dict[str, _Holder] = {}
        self._waiters: dict[str, deque[_Waiter]] = {}
        # Optional per-endpoint overrides; falls back to the global default.
        self._limits: dict[str, int] = {}

    # ----- configuration ---------------------------------------------------
    def default_limit(self) -> int:
        try:
            value = int(get_setting("coding_max_concurrent_tasks", DEFAULT_TASK_LIMIT) or DEFAULT_TASK_LIMIT)
        except Exception:
            value = DEFAULT_TASK_LIMIT
        return max(1, value)

    def limit_for(self, endpoint_key: str) -> int:
        override = self._limits.get(endpoint_key)
        if override is not None:
            return max(1, int(override))
        return self.default_limit()

    def set_limit(self, endpoint_key: str, limit: int | None) -> None:
        if limit is None:
            self._limits.pop(endpoint_key, None)
        else:
            self._limits[endpoint_key] = max(1, int(limit))

    # ----- internals -------------------------------------------------------
    @staticmethod
    def _key(endpoint_key: str | None) -> str:
        return (endpoint_key or "").strip() or "default"

    def _active_count(self, endpoint_key: str) -> int:
        return sum(1 for h in self._holders.values() if h.endpoint_key == endpoint_key)

    def _mint(self, endpoint_key: str, run_id: str, owner: str) -> _Holder:
        slot_id = secrets.token_hex(8)
        holder = _Holder(
            slot_id=slot_id,
            endpoint_key=endpoint_key,
            run_id=run_id,
            owner=owner,
            expires_at=_now() + TASK_SLOT_TTL_SECONDS,
        )
        self._holders[slot_id] = holder
        return holder

    def _drain(self, endpoint_key: str) -> None:
        """Hand freed capacity for an endpoint to the FIFO waiters."""
        queue = self._waiters.get(endpoint_key)
        if not queue:
            return
        while queue and self._active_count(endpoint_key) < self.limit_for(endpoint_key):
            waiter = queue.popleft()
            if waiter.future.cancelled() or waiter.future.done():
                continue  # acquirer timed out / gave up
            holder = self._mint(endpoint_key, waiter.run_id, waiter.owner)
            waiter.future.set_result(holder.slot_id)
        if not queue:
            self._waiters.pop(endpoint_key, None)

    def _discard_waiter(self, endpoint_key: str, waiter: _Waiter) -> None:
        queue = self._waiters.get(endpoint_key)
        if not queue:
            return
        try:
            queue.remove(waiter)
        except ValueError:
            pass
        if not queue:
            self._waiters.pop(endpoint_key, None)

    def reclaim_expired(self) -> None:
        now = _now()
        expired = [slot_id for slot_id, h in self._holders.items() if h.expires_at <= now]
        affected: set[str] = set()
        for slot_id in expired:
            holder = self._holders.pop(slot_id, None)
            if holder:
                affected.add(holder.endpoint_key)
        for key in affected:
            self._drain(key)

    # ----- public API ------------------------------------------------------
    async def acquire(
        self,
        *,
        owner: str,
        run_id: str,
        endpoint_key: str | None,
        wait_seconds: float = DEFAULT_ACQUIRE_WAIT_SECONDS,
    ) -> dict[str, Any]:
        """Acquire a slot for an endpoint, blocking up to ``wait_seconds``.

        Returns ``{"granted": True, "slot_id": ...}`` when a slot is held, or
        ``{"granted": False, "status": "queued", ...}`` on timeout so the caller
        re-acquires (bounded long-poll).
        """
        key = self._key(endpoint_key)
        self.reclaim_expired()

        if self._active_count(key) < self.limit_for(key):
            holder = self._mint(key, run_id, owner)
            return {"granted": True, "slot_id": holder.slot_id, "endpoint": key}

        loop = asyncio.get_event_loop()
        future: "asyncio.Future[str]" = loop.create_future()
        waiter = _Waiter(future=future, endpoint_key=key, run_id=run_id, owner=owner)
        self._waiters.setdefault(key, deque()).append(waiter)
        try:
            slot_id = await asyncio.wait_for(future, max(0.5, float(wait_seconds)))
            return {"granted": True, "slot_id": slot_id, "endpoint": key}
        except asyncio.TimeoutError:
            # Race: a release may have resolved us right at the deadline.
            if future.done() and not future.cancelled():
                return {"granted": True, "slot_id": future.result(), "endpoint": key}
            self._discard_waiter(key, waiter)
            return {
                "granted": False,
                "status": "queued",
                "endpoint": key,
                "active": self._active_count(key),
                "limit": self.limit_for(key),
            }

    def release(self, slot_id: str) -> dict[str, Any]:
        holder = self._holders.pop((slot_id or "").strip(), None)
        if not holder:
            return {"released": False}
        self.reclaim_expired()
        self._drain(holder.endpoint_key)
        return {"released": True, "endpoint": holder.endpoint_key}

    def heartbeat(self, slot_id: str) -> dict[str, Any]:
        holder = self._holders.get((slot_id or "").strip())
        if not holder:
            return {"ok": False}
        holder.expires_at = _now() + TASK_SLOT_TTL_SECONDS
        return {"ok": True, "expires_in": TASK_SLOT_TTL_SECONDS}

    def release_run(self, run_id: str) -> None:
        """Release every slot/waiter belonging to a run (called on run teardown)."""
        run_id = (run_id or "").strip()
        if not run_id:
            return
        affected: set[str] = set()
        for slot_id in [sid for sid, h in self._holders.items() if h.run_id == run_id]:
            holder = self._holders.pop(slot_id, None)
            if holder:
                affected.add(holder.endpoint_key)
        for key, queue in list(self._waiters.items()):
            for waiter in [w for w in queue if w.run_id == run_id]:
                self._discard_waiter(key, waiter)
                if not waiter.future.done():
                    waiter.future.cancel()
        for key in affected:
            self._drain(key)

    def snapshot(self, owner: str | None = None) -> dict[str, Any]:
        self.reclaim_expired()
        owner = (owner or "").strip()
        endpoints: dict[str, dict[str, Any]] = {}

        def bucket(key: str) -> dict[str, Any]:
            return endpoints.setdefault(
                key,
                {
                    "endpoint": key,
                    "limit": self.limit_for(key),
                    "active": 0,
                    "waiting": 0,
                    "active_runs": [],
                    "waiting_runs": [],
                },
            )

        for holder in self._holders.values():
            if owner and holder.owner != owner:
                continue
            entry = bucket(holder.endpoint_key)
            entry["active"] += 1
            entry["active_runs"].append(holder.run_id)
        for key, queue in self._waiters.items():
            for waiter in queue:
                if owner and waiter.owner != owner:
                    continue
                entry = bucket(key)
                entry["waiting"] += 1
                entry["waiting_runs"].append(waiter.run_id)

        active_total = sum(e["active"] for e in endpoints.values())
        waiting_total = sum(e["waiting"] for e in endpoints.values())
        return {
            "default_limit": self.default_limit(),
            "active_total": active_total,
            "waiting_total": waiting_total,
            "endpoints": sorted(endpoints.values(), key=lambda e: e["endpoint"]),
            # run_id -> "running" | "waiting" for quick UI lookup
            "run_states": {
                **{rid: "running" for e in endpoints.values() for rid in e["active_runs"]},
                **{rid: "waiting" for e in endpoints.values() for rid in e["waiting_runs"]},
            },
        }


_SERVICE: TaskSlotService | None = None


def get_task_slot_service() -> TaskSlotService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = TaskSlotService()
    return _SERVICE
