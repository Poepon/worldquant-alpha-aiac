"""Pipeline per-coroutine LIVENESS watchdog (2026-06-03 redesign).

REPLACES the old progress-signal heartbeat. The old design tracked one global
"last progress" stamp (push / persist / drain) and aborted if it went stale —
which mis-fired on legitimate 0-output rounds (task 3930: a producer busy with
LLM retries got killed because it hadn't pushed yet). The new watchdog tracks
PER-COROUTINE liveness: each monitored coroutine stamps a timestamp every time
it RETURNS from a `_with_timeout` await (via _LIVENESS.touch(owner)). A
coroutine PARKED on a bare await stops stamping → stale → abort. IDLE (blocked
on an empty/full queue, via _LIVENESS.enter_idle/exit_idle) is a SEPARATE exempt
state. So "slow but alive" and "legitimately waiting" are never killed; only a
truly frozen (never-returning) coroutine is.

docs/heartbeat_liveness_redesign_2026-06-03.md.
"""

import asyncio
import time

import pytest

from backend.agents.pipeline.runner import (
    PipelineHeartbeatExpired,
    _LIVENESS,
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


async def _simulate(cand):
    return {"ok": True}


async def _evaluate(cand, sim):
    return SimResult(candidate=cand, ok=True)


async def _persist(session, results):
    return len(results)


@pytest.mark.asyncio
async def test_liveness_freeze_caught_when_coroutine_parks():
    """An INSTRUMENTED producer that registers + touches once, then parks on a
    bare await (never touches again) → its stamp goes stale → the watchdog
    aborts, naming the frozen owner. This is the real freeze CLASS."""
    started = asyncio.Event()

    async def frozen_produce(push, should_stop):
        lv = _LIVENESS.get()
        assert lv is not None, "liveness must be published when heartbeat active"
        lv.touch("hyp")          # baseline stamp = alive
        started.set()
        # Park forever on a BARE await — never touch again (= true freeze).
        await asyncio.sleep(60)

    t0 = time.monotonic()
    with pytest.raises(PipelineHeartbeatExpired) as ei:
        await asyncio.wait_for(run_pipeline_session(
            produce=frozen_produce, simulate=_simulate, evaluate=_evaluate,
            persist=_persist, session_factory=_sf, num_consumers=1,
            acquire_slot=_acq, release_slot=_rel,
            heartbeat_timeout_sec=1.0,        # 1s liveness window
        ), timeout=12)
    elapsed = time.monotonic() - t0
    assert started.is_set()
    # Aborted within a few scan intervals + debounce — definitely not 60s.
    assert elapsed < 10, f"abort took {elapsed:.1f}s — watchdog too slow"
    msg = str(ei.value)
    assert "liveness freeze" in msg
    assert "owner=hyp" in msg


@pytest.mark.asyncio
async def test_slow_but_alive_producer_not_killed():
    """task-3930 REGRESSION: a producer doing legitimate slow work (0 output
    for a while, but keeps RETURNING from bounded ops → keeps touching) must
    NOT be killed. It touches every 0.3s under a 1s window for ~2.4s, then
    pushes nothing and returns cleanly."""
    async def slow_alive_produce(push, should_stop):
        lv = _LIVENESS.get()
        lv.touch("hyp")
        # 8 × 0.3s = 2.4s of "working but 0 output" — would trip a 1s GLOBAL
        # progress heartbeat (the old bug), but each touch keeps liveness fresh.
        for _ in range(8):
            await asyncio.sleep(0.3)
            lv.touch("hyp")        # simulates returning from a bounded wf.run op
        # done — no candidates this session (e.g. all hypotheses invalid)

    stats = await asyncio.wait_for(run_pipeline_session(
        produce=slow_alive_produce, simulate=_simulate, evaluate=_evaluate,
        persist=_persist, session_factory=_sf, num_consumers=1,
        acquire_slot=_acq, release_slot=_rel,
        heartbeat_timeout_sec=1.0,
    ), timeout=12)
    # Survived the whole 2.4s despite zero output — no false abort.
    assert stats["produced"] == 0
    assert stats["persisted"] == 0


@pytest.mark.asyncio
async def test_idle_producer_not_killed():
    """A producer that registers then sits IDLE (e.g. blocked waiting for work
    it will legitimately wait on) for longer than the window must NOT be killed —
    IDLE is exempt. Here it enters idle, waits 2.5s > 1s window, exits, returns."""
    async def idle_produce(push, should_stop):
        lv = _LIVENESS.get()
        lv.touch("hyp")
        lv.enter_idle("hyp")       # legitimate wait, exempt from the watchdog
        await asyncio.sleep(2.5)   # 2.5s > 1s window — but IDLE → no abort
        lv.exit_idle("hyp")

    stats = await asyncio.wait_for(run_pipeline_session(
        produce=idle_produce, simulate=_simulate, evaluate=_evaluate,
        persist=_persist, session_factory=_sf, num_consumers=1,
        acquire_slot=_acq, release_slot=_rel,
        heartbeat_timeout_sec=1.0,
    ), timeout=12)
    assert stats["produced"] == 0


@pytest.mark.asyncio
async def test_productive_session_not_killed():
    """A steady producer + active consumers run to completion — all candidates
    produced & persisted, no abort."""
    async def steady_produce(push, should_stop):
        lv = _LIVENESS.get()
        lv.touch("hyp")
        for i in range(30):
            await push(Candidate(f"c{i}", {}))
            lv.touch("hyp")
            await asyncio.sleep(0.02)

    persisted = []

    async def persist(session, results):
        persisted.extend(results)
        return len(results)

    stats = await asyncio.wait_for(run_pipeline_session(
        produce=steady_produce, simulate=_simulate, evaluate=_evaluate,
        persist=persist, session_factory=_sf, num_consumers=2,
        acquire_slot=_acq, release_slot=_rel,
        heartbeat_timeout_sec=1.0,
    ), timeout=15)
    assert stats["produced"] == 30
    assert stats["persisted"] == 30
    assert len(persisted) == 30


@pytest.mark.asyncio
async def test_watchdog_disabled_when_timeout_is_none():
    """heartbeat_timeout_sec=None → no supervisor, no liveness publish → backward
    compatible. A long unstamped gap does NOT abort."""
    async def slow_produce(push, should_stop):
        await asyncio.sleep(2)     # no stamping at all — would trip if active
        await push(Candidate("c0", {}))

    stats = await asyncio.wait_for(run_pipeline_session(
        produce=slow_produce, simulate=_simulate, evaluate=_evaluate,
        persist=_persist, session_factory=_sf, num_consumers=1,
        acquire_slot=_acq, release_slot=_rel,
        heartbeat_timeout_sec=None,
    ), timeout=10)
    assert stats["produced"] == 1
    assert stats["persisted"] == 1


@pytest.mark.asyncio
async def test_watchdog_zero_also_disables():
    """heartbeat_timeout_sec=0 → also disabled (same path as None)."""
    async def slow_produce(push, should_stop):
        await asyncio.sleep(1.5)
        await push(Candidate("c0", {}))

    stats = await asyncio.wait_for(run_pipeline_session(
        produce=slow_produce, simulate=_simulate, evaluate=_evaluate,
        persist=_persist, session_factory=_sf, num_consumers=1,
        acquire_slot=_acq, release_slot=_rel,
        heartbeat_timeout_sec=0,
    ), timeout=10)
    assert stats["produced"] == 1
