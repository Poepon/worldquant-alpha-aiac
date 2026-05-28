"""Pipeline session-level heartbeat-abort (2026-05-28 structural fix).

The per-op `op_timeout` backstop in test_sim_pipeline_timeout.py is necessary
but not SUFFICIENT — validated 2026-05-28 across tasks 3737/3738/3739, where
3 different unwrapped DB-op points each caused a permanent freeze when a
wait_for cancel poisoned a shared asyncpg connection. The heartbeat supervisor
catches the freeze CLASS by tracking pipeline progress (push / persist /
feedback-drain event_done) and aborting the session if nothing moves for
`heartbeat_timeout_sec` seconds — regardless of WHICH await is hung.
"""

import asyncio
import time

import pytest

from backend.agents.pipeline.runner import (
    PipelineHeartbeatExpired,
    run_pipeline_session,
)
from backend.agents.pipeline.types import Candidate, SimResult


class _NullSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _sf():
    return _NullSession()


async def _acq():
    return True


async def _rel():
    return None


@pytest.mark.asyncio
async def test_heartbeat_aborts_session_when_no_progress():
    """When NOTHING moves for >heartbeat_timeout_sec, the supervisor cancels
    the pipeline and PipelineHeartbeatExpired propagates — even if no per-op
    timeout is set and the hung await is otherwise unbounded."""
    started = asyncio.Event()

    async def hung_produce(push, should_stop):
        started.set()
        # Wedge forever — no push, no progress at all.
        await asyncio.sleep(60)

    async def simulate(cand):
        return {"ok": True}

    async def evaluate(cand, sim):
        return SimResult(candidate=cand, ok=True)

    async def persist(session, results):
        return len(results)

    t0 = time.monotonic()
    with pytest.raises(PipelineHeartbeatExpired) as ei:
        await asyncio.wait_for(run_pipeline_session(
            produce=hung_produce, simulate=simulate, evaluate=evaluate,
            persist=persist, session_factory=_sf, num_consumers=1,
            acquire_slot=_acq, release_slot=_rel,
            heartbeat_timeout_sec=1.0,        # 1s heartbeat
            # op_timeout intentionally unset — proving heartbeat works alone
        ), timeout=10)
    elapsed = time.monotonic() - t0
    assert started.is_set()
    # Aborted within a couple of heartbeat intervals — definitely not 60s.
    assert elapsed < 8, f"abort took {elapsed:.1f}s — supervisor too slow"
    # Message should carry counters for the morning report.
    assert "no pipeline progress" in str(ei.value)
    assert "produced=0" in str(ei.value)


@pytest.mark.asyncio
async def test_heartbeat_does_not_fire_when_push_keeps_beating():
    """Regular push events reset the heartbeat — a productive session must
    NOT be killed. produce here pushes every 50ms for ~1.5s under a 1s
    heartbeat: the heartbeat would fire on no progress, but each push beats."""
    async def steady_produce(push, should_stop):
        for i in range(30):
            await push(Candidate(f"c{i}", {}))
            await asyncio.sleep(0.05)        # 50ms < 1s heartbeat

    async def simulate(cand):
        return {"ok": True}

    async def evaluate(cand, sim):
        return SimResult(candidate=cand, ok=True)

    persisted = []

    async def persist(session, results):
        persisted.extend(results)
        return len(results)

    stats = await asyncio.wait_for(run_pipeline_session(
        produce=steady_produce, simulate=simulate, evaluate=evaluate,
        persist=persist, session_factory=_sf, num_consumers=2,
        acquire_slot=_acq, release_slot=_rel,
        heartbeat_timeout_sec=1.0,
    ), timeout=15)
    # Did NOT abort — all 30 produced & persisted.
    assert stats["produced"] == 30
    assert stats["persisted"] == 30
    assert len(persisted) == 30


@pytest.mark.asyncio
async def test_heartbeat_disabled_when_timeout_is_none():
    """heartbeat_timeout_sec=None → no supervisor → backward compatible."""
    async def slow_produce(push, should_stop):
        # A 2s gap with no progress would trip a 1s heartbeat — proving the
        # supervisor is OFF when the param is None.
        await asyncio.sleep(2)
        await push(Candidate("c0", {}))

    async def simulate(cand):
        return {"ok": True}

    async def evaluate(cand, sim):
        return SimResult(candidate=cand, ok=True)

    async def persist(session, results):
        return len(results)

    stats = await asyncio.wait_for(run_pipeline_session(
        produce=slow_produce, simulate=simulate, evaluate=evaluate,
        persist=persist, session_factory=_sf, num_consumers=1,
        acquire_slot=_acq, release_slot=_rel,
        heartbeat_timeout_sec=None,          # OFF
    ), timeout=10)
    assert stats["produced"] == 1
    assert stats["persisted"] == 1


@pytest.mark.asyncio
async def test_heartbeat_zero_also_disables():
    """heartbeat_timeout_sec=0 → also disabled (same path as None)."""
    async def slow_produce(push, should_stop):
        await asyncio.sleep(1.5)             # would trip a 1s heartbeat
        await push(Candidate("c0", {}))

    async def simulate(cand):
        return {"ok": True}

    async def evaluate(cand, sim):
        return SimResult(candidate=cand, ok=True)

    async def persist(session, results):
        return len(results)

    stats = await asyncio.wait_for(run_pipeline_session(
        produce=slow_produce, simulate=simulate, evaluate=evaluate,
        persist=persist, session_factory=_sf, num_consumers=1,
        acquire_slot=_acq, release_slot=_rel,
        heartbeat_timeout_sec=0,
    ), timeout=10)
    assert stats["produced"] == 1
