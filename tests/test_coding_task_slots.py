"""Unit tests for the per-endpoint task-slot limiter (src/coding_task_slots.py)."""

from __future__ import annotations

import asyncio

import pytest

from src.coding_task_slots import TaskSlotService


@pytest.mark.asyncio
async def test_acquire_grants_up_to_limit_then_queues():
    svc = TaskSlotService()
    svc.set_limit("ep", 2)

    a = await svc.acquire(owner="o", run_id="r1", endpoint_key="ep", wait_seconds=0.5)
    b = await svc.acquire(owner="o", run_id="r2", endpoint_key="ep", wait_seconds=0.5)
    assert a["granted"] and b["granted"]
    assert a["slot_id"] != b["slot_id"]

    # Pool is full → a third acquire blocks then times out to "queued".
    c = await svc.acquire(owner="o", run_id="r3", endpoint_key="ep", wait_seconds=0.5)
    assert c["granted"] is False
    assert c["status"] == "queued"
    assert c["active"] == 2 and c["limit"] == 2


@pytest.mark.asyncio
async def test_release_wakes_fifo_waiter():
    svc = TaskSlotService()
    svc.set_limit("ep", 1)

    a = await svc.acquire(owner="o", run_id="r1", endpoint_key="ep", wait_seconds=0.5)
    assert a["granted"]

    t2 = asyncio.create_task(svc.acquire(owner="o", run_id="r2", endpoint_key="ep", wait_seconds=5))
    t3 = asyncio.create_task(svc.acquire(owner="o", run_id="r3", endpoint_key="ep", wait_seconds=5))
    await asyncio.sleep(0.05)  # let both enqueue as waiters, in order

    svc.release(a["slot_id"])
    r2 = await asyncio.wait_for(t2, 1)
    assert r2["granted"]            # FIFO head proceeds
    assert not t3.done()           # second waiter still blocked (limit 1)

    svc.release(r2["slot_id"])
    r3 = await asyncio.wait_for(t3, 1)
    assert r3["granted"]


@pytest.mark.asyncio
async def test_per_endpoint_isolation():
    svc = TaskSlotService()
    svc.set_limit("A", 1)
    svc.set_limit("B", 1)

    a = await svc.acquire(owner="o", run_id="r1", endpoint_key="A", wait_seconds=0.5)
    assert a["granted"]
    # Endpoint A is full, but endpoint B is independent and still grants.
    b = await svc.acquire(owner="o", run_id="r2", endpoint_key="B", wait_seconds=0.5)
    assert b["granted"]
    # A second A acquire queues.
    a2 = await svc.acquire(owner="o", run_id="r3", endpoint_key="A", wait_seconds=0.3)
    assert a2["granted"] is False


@pytest.mark.asyncio
async def test_lease_reclaim_frees_dead_holder(monkeypatch):
    import src.coding_task_slots as mod

    monkeypatch.setattr(mod, "TASK_SLOT_TTL_SECONDS", 0.05)
    svc = TaskSlotService()
    svc.set_limit("ep", 1)

    a = await svc.acquire(owner="o", run_id="r1", endpoint_key="ep", wait_seconds=0.5)
    assert a["granted"]
    await asyncio.sleep(0.1)  # lease expires with no heartbeat / release

    # The next acquire reclaims the expired holder and grants.
    b = await svc.acquire(owner="o", run_id="r2", endpoint_key="ep", wait_seconds=0.5)
    assert b["granted"]


@pytest.mark.asyncio
async def test_heartbeat_extends_and_reports():
    svc = TaskSlotService()
    svc.set_limit("ep", 1)
    a = await svc.acquire(owner="o", run_id="r1", endpoint_key="ep", wait_seconds=0.5)
    assert svc.heartbeat(a["slot_id"])["ok"] is True
    assert svc.heartbeat("does-not-exist")["ok"] is False


@pytest.mark.asyncio
async def test_release_run_frees_slots_and_unblocks_waiter():
    svc = TaskSlotService()
    svc.set_limit("ep", 1)

    a = await svc.acquire(owner="o", run_id="r1", endpoint_key="ep", wait_seconds=0.5)
    assert a["granted"]
    t2 = asyncio.create_task(svc.acquire(owner="o", run_id="r2", endpoint_key="ep", wait_seconds=5))
    await asyncio.sleep(0.05)

    # Simulate r1's terminal dying: teardown releases all of its slots.
    svc.release_run("r1")
    r2 = await asyncio.wait_for(t2, 1)
    assert r2["granted"]

    snap = svc.snapshot("o")
    assert snap["active_total"] == 1
    assert snap["waiting_total"] == 0
    assert snap["run_states"].get("r2") == "running"
    assert "r1" not in snap["run_states"]


@pytest.mark.asyncio
async def test_snapshot_reports_waiting_runs():
    svc = TaskSlotService()
    svc.set_limit("ep", 1)
    a = await svc.acquire(owner="o", run_id="r1", endpoint_key="ep", wait_seconds=0.5)
    assert a["granted"]
    t2 = asyncio.create_task(svc.acquire(owner="o", run_id="r2", endpoint_key="ep", wait_seconds=5))
    await asyncio.sleep(0.05)

    snap = svc.snapshot("o")
    assert snap["active_total"] == 1
    assert snap["waiting_total"] == 1
    assert snap["run_states"].get("r1") == "running"
    assert snap["run_states"].get("r2") == "waiting"
    ep = next(e for e in snap["endpoints"] if e["endpoint"] == "ep")
    assert ep["limit"] == 1 and ep["active"] == 1 and ep["waiting"] == 1

    # Cleanup: releasing r1 hands its freed slot to the waiting r2 (FIFO).
    svc.release_run("r1")
    r2 = await asyncio.wait_for(t2, 1)
    assert r2["granted"]
    svc.release_run("r2")
