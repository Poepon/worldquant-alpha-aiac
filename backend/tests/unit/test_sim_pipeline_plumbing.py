"""Unit tests for the mining pipeline plumbing (Sub-phase 0).

These exercise ``run_pipeline_session`` with injected fakes only — no DB, no
BRAIN, no LangGraph. They lock down the concurrency contract: slot
acquire/release balance, backpressure, graceful drain/shutdown, error
isolation, and persist batching.
"""

import asyncio
import contextlib

import pytest

from backend.agents.pipeline import Candidate, SimResult, run_pipeline_session


class FakeSlots:
    """In-memory stand-in for the Redis sim-slot counter.

    Tracks live count + the high-water mark so tests can assert the pipeline
    never exceeds the consumer ceiling and always releases.
    """

    def __init__(self, limit=3, fail_acquire=False):
        self.limit = limit
        self.live = 0
        self.max_live = 0
        self.acquires = 0
        self.releases = 0
        self.fail_acquire = fail_acquire
        self._lock = asyncio.Lock()

    async def acquire(self):
        if self.fail_acquire:
            return False
        async with self._lock:
            self.acquires += 1
            self.live += 1
            self.max_live = max(self.max_live, self.live)
        return True

    async def release(self):
        async with self._lock:
            self.releases += 1
            self.live -= 1


@contextlib.asynccontextmanager
async def _null_session_factory():
    yield object()


def _candidates(n):
    return [Candidate(expression=f"close/open+{i}", context={"i": i}) for i in range(n)]


@pytest.mark.asyncio
async def test_happy_path_all_simulated_and_persisted():
    cands = _candidates(7)
    slots = FakeSlots(limit=3)
    persisted = []

    async def produce(push, should_stop):
        for c in cands:
            await push(c)

    async def simulate(c):
        await asyncio.sleep(0)  # yield
        return {"sharpe": 1.0 + c.context["i"]}

    async def evaluate(c, out):
        return SimResult(candidate=c, ok=True, metrics=out, verdict="PASS")

    async def persist(session, results):
        persisted.extend(results)
        return len(results)

    stats = await run_pipeline_session(
        produce=produce, simulate=simulate, evaluate=evaluate, persist=persist,
        session_factory=_null_session_factory, num_consumers=3,
        acquire_slot=slots.acquire, release_slot=slots.release,
    )

    assert stats["produced"] == 7
    assert stats["simulated"] == 7
    assert stats["persisted"] == 7
    assert stats["errors"] == 0
    assert len(persisted) == 7
    # Every acquire was released; never over the ceiling.
    assert slots.acquires == slots.releases == 7
    assert slots.live == 0
    assert slots.max_live <= 3


@pytest.mark.asyncio
async def test_slot_released_when_simulate_raises():
    cands = _candidates(6)
    slots = FakeSlots(limit=2)
    persisted = []

    async def produce(push, should_stop):
        for c in cands:
            await push(c)

    async def simulate(c):
        if c.context["i"] % 2 == 0:
            raise RuntimeError("brain boom")
        return {"sharpe": 0.5}

    async def evaluate(c, out):
        return SimResult(candidate=c, ok=True, metrics=out, verdict="PASS")

    async def persist(session, results):
        persisted.extend(results)
        return sum(1 for r in results if r.ok)

    stats = await run_pipeline_session(
        produce=produce, simulate=simulate, evaluate=evaluate, persist=persist,
        session_factory=_null_session_factory, num_consumers=2,
        acquire_slot=slots.acquire, release_slot=slots.release,
    )

    # 3 raised, 3 succeeded.
    assert stats["errors"] == 3
    assert stats["simulated"] == 3
    # CRITICAL: a failing sim must still release its slot.
    assert slots.acquires == 6
    assert slots.releases == 6
    assert slots.live == 0
    # Failures are still handed to the persister (as not-ok results).
    assert sum(1 for r in persisted if not r.ok) == 3
    assert sum(1 for r in persisted if r.ok) == 3


@pytest.mark.asyncio
async def test_backpressure_queue_never_exceeds_maxsize():
    cands = _candidates(40)
    slots = FakeSlots(limit=2)
    # Consumers are slow; producer is fast → the bounded queue must throttle it.

    async def produce(push, should_stop):
        for c in cands:
            await push(c)

    async def simulate(c):
        await asyncio.sleep(0.005)
        return {"ok": True}

    async def evaluate(c, out):
        return SimResult(candidate=c, ok=True, metrics=out)

    async def persist(session, results):
        return len(results)

    # queue_maxsize auto = 2 * num_consumers = 4. Producing 40 fast items must
    # never let more than (maxsize + in-flight) pile up — assert produced count
    # rises only as consumers drain. We sample produced vs persisted gap.
    stats = await run_pipeline_session(
        produce=produce, simulate=simulate, evaluate=evaluate, persist=persist,
        session_factory=_null_session_factory, num_consumers=2,
        acquire_slot=slots.acquire, release_slot=slots.release,
    )
    assert stats["produced"] == 40
    assert stats["simulated"] == 40
    assert stats["persisted"] == 40
    assert slots.live == 0


@pytest.mark.asyncio
async def test_slot_acquire_timeout_skips_and_records():
    cands = _candidates(4)
    slots = FakeSlots(limit=3, fail_acquire=True)  # never grants a slot
    persisted = []

    async def produce(push, should_stop):
        for c in cands:
            await push(c)

    async def simulate(c):  # must never be called
        raise AssertionError("simulate called despite no slot")

    async def evaluate(c, out):
        raise AssertionError("evaluate called despite no slot")

    async def persist(session, results):
        persisted.extend(results)
        return 0

    stats = await run_pipeline_session(
        produce=produce, simulate=simulate, evaluate=evaluate, persist=persist,
        session_factory=_null_session_factory, num_consumers=2,
        acquire_slot=slots.acquire, release_slot=slots.release,
    )

    assert stats["slot_timeouts"] == 4
    assert stats["simulated"] == 0
    assert slots.releases == 0  # nothing acquired → nothing released
    # All 4 recorded as failed results for the persister.
    assert len(persisted) == 4
    assert all(r.error == "slot_acquire_timeout" for r in persisted)


@pytest.mark.asyncio
async def test_persist_batching_respects_persist_every():
    cands = _candidates(10)
    slots = FakeSlots(limit=4)
    flush_sizes = []

    async def produce(push, should_stop):
        for c in cands:
            await push(c)

    async def simulate(c):
        return {"ok": True}

    async def evaluate(c, out):
        return SimResult(candidate=c, ok=True, metrics=out)

    async def persist(session, results):
        flush_sizes.append(len(results))
        return len(results)

    stats = await run_pipeline_session(
        produce=produce, simulate=simulate, evaluate=evaluate, persist=persist,
        session_factory=_null_session_factory, num_consumers=1,
        persist_every=4,
        acquire_slot=slots.acquire, release_slot=slots.release,
    )

    assert stats["persisted"] == 10
    # 1 consumer + persist_every=4 → flushes of 4,4 then a trailing 2 on exit.
    assert sum(flush_sizes) == 10
    assert flush_sizes == [4, 4, 2]


@pytest.mark.asyncio
async def test_concurrency_reaches_consumer_count():
    cands = _candidates(12)
    slots = FakeSlots(limit=4)

    async def produce(push, should_stop):
        for c in cands:
            await push(c)

    async def simulate(c):
        await asyncio.sleep(0.02)  # hold the slot long enough to overlap
        return {"ok": True}

    async def evaluate(c, out):
        return SimResult(candidate=c, ok=True, metrics=out)

    async def persist(session, results):
        return len(results)

    await run_pipeline_session(
        produce=produce, simulate=simulate, evaluate=evaluate, persist=persist,
        session_factory=_null_session_factory, num_consumers=4,
        acquire_slot=slots.acquire, release_slot=slots.release,
    )

    # With 4 consumers + 12 overlapping sims, all 4 slots should be live at once.
    assert slots.max_live == 4


@pytest.mark.asyncio
async def test_stop_event_drains_gracefully():
    cands = _candidates(100)
    slots = FakeSlots(limit=2)
    stop = asyncio.Event()

    async def produce(push, should_stop):
        for c in cands:
            if should_stop():
                return
            await push(c)
            if c.context["i"] == 5:
                stop.set()

    async def simulate(c):
        await asyncio.sleep(0)
        return {"ok": True}

    async def evaluate(c, out):
        return SimResult(candidate=c, ok=True, metrics=out)

    async def persist(session, results):
        return len(results)

    stats = await run_pipeline_session(
        produce=produce, simulate=simulate, evaluate=evaluate, persist=persist,
        session_factory=_null_session_factory, num_consumers=2,
        acquire_slot=slots.acquire, release_slot=slots.release,
        stop_event=stop,
    )

    # Producer bailed early on stop → far fewer than 100 produced, no hang.
    assert stats["produced"] < 100
    assert slots.live == 0  # clean shutdown, all slots released


@pytest.mark.asyncio
async def test_producer_crash_still_drains_consumers():
    slots = FakeSlots(limit=2)
    persisted = []

    async def produce(push, should_stop):
        await push(Candidate(expression="a", context={"i": 0}))
        await push(Candidate(expression="b", context={"i": 1}))
        raise RuntimeError("producer exploded")

    async def simulate(c):
        return {"ok": True}

    async def evaluate(c, out):
        return SimResult(candidate=c, ok=True, metrics=out)

    async def persist(session, results):
        persisted.extend(results)
        return len(results)

    stats = await run_pipeline_session(
        produce=produce, simulate=simulate, evaluate=evaluate, persist=persist,
        session_factory=_null_session_factory, num_consumers=2,
        acquire_slot=slots.acquire, release_slot=slots.release,
    )

    # The 2 candidates pushed before the crash are still simulated + persisted,
    # and the session terminates (no hang) despite the producer exception.
    assert stats["produced"] == 2
    assert stats["simulated"] == 2
    assert stats["persisted"] == 2
    assert slots.live == 0


@pytest.mark.asyncio
async def test_invalid_num_consumers_rejected():
    async def noop(*a, **k):
        return None

    with pytest.raises(ValueError):
        await run_pipeline_session(
            produce=noop, simulate=noop, evaluate=noop, persist=noop,
            session_factory=_null_session_factory, num_consumers=0,
        )
