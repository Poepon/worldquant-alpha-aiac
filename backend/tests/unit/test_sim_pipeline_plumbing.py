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
async def test_backpressure_bounds_in_flight_work():
    """Producer is fast, consumers slow → the bounded work_q must throttle the
    producer. We OBSERVE the in-flight gap (produced − completed) and assert it
    stays bounded by ~queue capacity + consumer pool. An unbounded queue would
    let the gap reach the full 40 before anything drains."""
    cands = _candidates(40)
    slots = FakeSlots(limit=2)
    num_consumers = 2
    completed = 0
    max_gap = 0

    async def produce(push, should_stop):
        nonlocal max_gap
        for c in cands:
            await push(c)
            max_gap = max(max_gap, c.context["i"] + 1 - completed)

    async def simulate(c):
        await asyncio.sleep(0.005)  # slow consumer
        return {"ok": True}

    async def evaluate(c, out):
        nonlocal completed
        completed += 1
        return SimResult(candidate=c, ok=True, metrics=out)

    async def persist(session, results):
        return len(results)

    stats = await run_pipeline_session(
        produce=produce, simulate=simulate, evaluate=evaluate, persist=persist,
        session_factory=_null_session_factory, num_consumers=num_consumers,
        acquire_slot=slots.acquire, release_slot=slots.release,
    )
    assert stats["produced"] == 40
    assert stats["simulated"] == 40
    assert stats["persisted"] == 40
    assert slots.live == 0
    # queue_maxsize auto = 2*num_consumers = 4, plus up to num_consumers in
    # flight → gap must stay well under 40. (Unbounded queue → gap ≈ 40.)
    assert max_gap <= 4 + num_consumers + 2, f"backpressure failed: max_gap={max_gap}"


class BlockingSlots:
    """Slot fake that actually BLOCKS at a hard ceiling (like the real Redis
    counter), so the runner's reliance on acquire_slot to cap concurrency is
    exercised even when num_consumers > limit."""

    def __init__(self, limit):
        self.limit = limit
        self.live = 0
        self.max_live = 0

    async def acquire(self):
        while self.live >= self.limit:
            await asyncio.sleep(0.001)
        self.live += 1
        self.max_live = max(self.max_live, self.live)
        return True

    async def release(self):
        self.live -= 1


@pytest.mark.asyncio
async def test_slot_ceiling_caps_concurrency_below_consumer_count():
    """5 consumers but a slot ceiling of 2 → at most 2 sims run at once. This
    is the invariant slots EXIST for; the non-blocking FakeSlots never tested
    it (concurrency was trivially capped by the consumer-pool size)."""
    cands = _candidates(15)
    slots = BlockingSlots(limit=2)

    async def produce(push, should_stop):
        for c in cands:
            await push(c)

    async def simulate(c):
        await asyncio.sleep(0.01)  # hold the slot long enough to contend
        return {"ok": True}

    async def evaluate(c, out):
        return SimResult(candidate=c, ok=True, metrics=out)

    async def persist(session, results):
        return len(results)

    stats = await run_pipeline_session(
        produce=produce, simulate=simulate, evaluate=evaluate, persist=persist,
        session_factory=_null_session_factory, num_consumers=5,
        acquire_slot=slots.acquire, release_slot=slots.release,
    )
    assert stats["simulated"] == 15
    # Even with 5 consumers, the slot ceiling held concurrency to 2.
    assert slots.max_live == 2
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

    # Producer bailed right after pushing i==5 → at most ~7 produced (tight, not
    # the loose <100). No hang.
    assert stats["produced"] <= 8
    assert slots.live == 0  # clean shutdown, all slots released
    # Every produced candidate is reconcilable: simulated + dropped-on-stop +
    # slot_timeouts + errors == produced (nothing silently vanishes).
    assert (
        stats["simulated"] + stats["dropped_on_stop"]
        + stats["slot_timeouts"] + stats["errors"]
    ) == stats["produced"]


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


@pytest.mark.asyncio
async def test_mismatched_slot_pair_rejected():
    """Passing only one of acquire/release would mix a fake with the real
    BrainAdapter primitive and corrupt the global Redis slot counter."""
    slots = FakeSlots(limit=2)

    async def noop(*a, **k):
        return None

    with pytest.raises(ValueError):
        await run_pipeline_session(
            produce=noop, simulate=noop, evaluate=noop, persist=noop,
            session_factory=_null_session_factory, num_consumers=2,
            acquire_slot=slots.acquire,  # release_slot omitted → reject
        )


@pytest.mark.asyncio
async def test_persist_failure_is_counted_not_silent():
    cands = _candidates(3)
    slots = FakeSlots(limit=2)

    async def produce(push, should_stop):
        for c in cands:
            await push(c)

    async def simulate(c):
        return {"ok": True}

    async def evaluate(c, out):
        return SimResult(candidate=c, ok=True, metrics=out)

    async def persist(session, results):
        raise RuntimeError("DB down")

    stats = await run_pipeline_session(
        produce=produce, simulate=simulate, evaluate=evaluate, persist=persist,
        session_factory=_null_session_factory, num_consumers=2,
        acquire_slot=slots.acquire, release_slot=slots.release,
    )
    # Sims ran (quota burned) but persistence failed — the loss is surfaced in
    # stats rather than silently swallowed, and the session still terminates.
    assert stats["simulated"] == 3
    assert stats["persisted"] == 0
    assert stats["persist_failures"] == 3
    assert slots.live == 0


@pytest.mark.asyncio
async def test_cancellation_releases_slots_and_no_orphans():
    """Cancelling the session mid-flight (e.g. a per-round wait_for deadline)
    must cancel+drain the child tasks — no orphaned consumers left holding
    BRAIN slots, no detached persister still writing."""
    slots = BlockingSlots(limit=2)
    sims_started = 0
    sims_after_cancel = 0
    cancelled = False

    async def produce(push, should_stop):
        i = 0
        while not should_stop():
            await push(Candidate(expression=f"e{i}", context={"i": i}))
            i += 1
            await asyncio.sleep(0.001)

    async def simulate(c):
        nonlocal sims_started, sims_after_cancel
        sims_started += 1
        if cancelled:
            sims_after_cancel += 1
        await asyncio.sleep(0.02)
        return {"ok": True}

    async def evaluate(c, out):
        return SimResult(candidate=c, ok=True, metrics=out)

    async def persist(session, results):
        return len(results)

    task = asyncio.create_task(run_pipeline_session(
        produce=produce, simulate=simulate, evaluate=evaluate, persist=persist,
        session_factory=_null_session_factory, num_consumers=2,
        acquire_slot=slots.acquire, release_slot=slots.release,
    ))
    # Let the pipeline get going, then cancel.
    await asyncio.sleep(0.05)
    assert sims_started > 0
    cancelled = True
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Give the loop a few ticks; if consumers were orphaned they'd keep simming.
    started_at_cancel = sims_started
    await asyncio.sleep(0.05)
    assert sims_started == started_at_cancel, "orphaned consumer kept simulating"
    # All slots released by the cancelled consumers' finally.
    assert slots.live == 0
