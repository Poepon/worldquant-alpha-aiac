"""Producer-consumer orchestration for the mining pipeline.

``run_pipeline_session`` is intentionally pure plumbing: it owns the two queues,
the consumer pool, and the single persister, and wires shutdown/draining
correctly — but it takes the four work stages (produce / simulate / evaluate /
persist) and the slot + session primitives as callables. That keeps the
concurrency mechanics unit-testable with fakes (no DB / BRAIN / LangGraph) and
lets the real node-backed wiring be injected by the caller in a later sub-phase.

Concurrency contract (the whole point — see design doc F1):
  - Exactly the producer(s) and the single persister may touch the database,
    each through its OWN session (never shared). The N consumers are DB-free —
    they only do BRAIN I/O (under a slot) + pure evaluate compute.
  - Slots are acquired/released per simulated candidate via the role-aware
    Redis counter (BrainAdapter._acquire_sim_slot/_release_sim_slot), so N
    auto-tracks the USER(3)/CONSULTANT(80) ceiling.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, List, Optional

from backend.agents.pipeline.types import Candidate, SimResult

logger = logging.getLogger(__name__)

# Sentinel signalling "no more work" through a queue. A distinct object per
# run avoids any chance of a real payload comparing equal to it.
_SENTINEL = object()

# Type aliases for the injected stages.
PushFn = Callable[[Candidate], Awaitable[None]]
ProduceFn = Callable[[PushFn, "Callable[[], bool]"], Awaitable[None]]
SimulateFn = Callable[[Candidate], Awaitable[Any]]
EvaluateFn = Callable[[Candidate, Any], Awaitable[SimResult]]
PersistFn = Callable[[Any, List[SimResult]], Awaitable[int]]


async def run_pipeline_session(
    *,
    produce: ProduceFn,
    simulate: SimulateFn,
    evaluate: EvaluateFn,
    persist: PersistFn,
    session_factory: Callable[[], Any],
    num_consumers: int,
    queue_maxsize: int = 0,
    persist_every: int = 1,
    acquire_slot: Optional[Callable[[], Awaitable[bool]]] = None,
    release_slot: Optional[Callable[[], Awaitable[None]]] = None,
    stop_event: Optional[asyncio.Event] = None,
) -> dict:
    """Run one pipeline session to completion and return run stats.

    Args:
        produce: ``async (push, should_stop) -> None``. Generates validated
            candidates and ``await push(candidate)`` for each (push blocks when
            the work queue is full = backpressure). Returns when the dataset
            cursor is exhausted or ``should_stop()`` is True. Owns its own DB
            session internally.
        simulate: ``async (candidate) -> sim_outcome``. Runs ONE BRAIN sim; the
            runner holds a slot around this call. DB-free.
        evaluate: ``async (candidate, sim_outcome) -> SimResult``. Pure verdict
            compute. DB-free.
        persist: ``async (session, results) -> persisted_count``. Writes a batch
            of SimResults (trace + alpha + bandit) through ``session``.
        session_factory: ``() -> async context manager`` yielding the
            persister's single-owner session (e.g. ``AsyncSessionLocal``).
        num_consumers: number of concurrent sim consumers (= sim-slot ceiling).
        queue_maxsize: work-queue capacity. <=0 → auto = 2 × num_consumers.
        persist_every: flush the persist batch every N results (1 = each).
        acquire_slot / release_slot: slot primitives; default to BrainAdapter's.
        stop_event: optional cooperative stop; checked by ``should_stop``.

    Returns:
        stats dict: produced, simulated, persisted, errors, slot_timeouts.
    """
    if num_consumers < 1:
        raise ValueError("num_consumers must be >= 1")
    if persist_every < 1:
        persist_every = 1
    if queue_maxsize <= 0:
        queue_maxsize = max(1, 2 * num_consumers)

    # Both-or-neither: a mismatched pair (e.g. a test fake acquire + the real
    # BrainAdapter release) would decrement the shared global Redis slot counter
    # that the fake never incremented, corrupting the ceiling for every worker.
    if (acquire_slot is None) != (release_slot is None):
        raise ValueError("acquire_slot and release_slot must be provided together")
    if acquire_slot is None:
        # Imported lazily so unit tests that inject fakes don't pull BRAIN/Redis.
        from backend.adapters.brain_adapter import BrainAdapter

        acquire_slot = BrainAdapter._acquire_sim_slot
        release_slot = BrainAdapter._release_sim_slot

    work_q: asyncio.Queue = asyncio.Queue(maxsize=queue_maxsize)
    # Bounded too: if the single persister falls behind N consumers, putting a
    # result blocks the consumer (back-pressure) instead of growing RAM without
    # limit over a multi-hour / 80-consumer session.
    persist_q: asyncio.Queue = asyncio.Queue(maxsize=max(4, 2 * queue_maxsize))
    stats = {
        "produced": 0,
        "simulated": 0,
        "persisted": 0,
        "errors": 0,
        "slot_timeouts": 0,
        "dropped_on_stop": 0,
        "persist_failures": 0,
    }

    def _should_stop() -> bool:
        return stop_event is not None and stop_event.is_set()

    async def _push(candidate: Candidate) -> None:
        await work_q.put(candidate)
        stats["produced"] += 1

    async def _producer() -> None:
        try:
            await produce(_push, _should_stop)
        except Exception:  # noqa: BLE001 — a producer crash must still drain
            logger.exception("[pipeline] producer crashed; draining consumers")
        finally:
            # One sentinel per consumer guarantees every consumer wakes and exits
            # even if the queue is otherwise empty.
            for _ in range(num_consumers):
                await work_q.put(_SENTINEL)

    async def _consumer(cid: int) -> None:
        while True:
            item = await work_q.get()
            try:
                if item is _SENTINEL:
                    return
                if _should_stop():
                    # Drop remaining queued work fast on stop. Counted (not
                    # silent) so produced == simulated + slot_timeouts + errors
                    # + dropped_on_stop stays reconcilable.
                    stats["dropped_on_stop"] += 1
                    continue
                acquired = False
                try:
                    acquired = await acquire_slot()
                    if not acquired:
                        stats["slot_timeouts"] += 1
                        await persist_q.put(
                            SimResult(candidate=item, ok=False, error="slot_acquire_timeout")
                        )
                        continue
                    sim_outcome = await simulate(item)
                    # Count the sim the moment it returns — BRAIN quota is spent
                    # here, regardless of whether evaluate/persist later fail.
                    stats["simulated"] += 1
                finally:
                    if acquired:
                        await release_slot()
                # Evaluate happens OUTSIDE the slot — the sim is done, no need to
                # hold the slot during pure compute.
                result = await evaluate(item, sim_outcome)
                await persist_q.put(result)
            except Exception as exc:  # noqa: BLE001 — one bad candidate ≠ dead consumer
                stats["errors"] += 1
                logger.exception("[pipeline] consumer %s failed a candidate", cid)
                try:
                    await persist_q.put(
                        SimResult(candidate=item if isinstance(item, Candidate) else None,
                                  ok=False, error=str(exc) or type(exc).__name__)
                    )
                except Exception:
                    pass
            finally:
                work_q.task_done()

    async def _persister() -> None:
        batch: List[SimResult] = []

        async def _flush() -> None:
            if not batch:
                return
            try:
                async with session_factory() as session:
                    n = await persist(session, list(batch))
                stats["persisted"] += int(n or 0)
            except Exception:  # noqa: BLE001 — never let a persist error kill the loop
                # The batch is dropped (results already simulated → wasted BRAIN
                # quota), so make the loss observable rather than silent. A
                # retry / dead-letter path is a Sub-phase 1 follow-up.
                stats["persist_failures"] += len(batch)
                logger.exception("[pipeline] persist flush DROPPED %d results", len(batch))
            finally:
                batch.clear()

        while True:
            item = await persist_q.get()
            try:
                if item is _SENTINEL:
                    # Flush the partial trailing batch before exiting so nothing
                    # already-simulated is dropped (would be wasted BRAIN quota).
                    await _flush()
                    return
                batch.append(item)
                if len(batch) >= persist_every:
                    await _flush()
            finally:
                persist_q.task_done()

    producer_task = asyncio.create_task(_producer(), name="pipeline-producer")
    consumer_tasks = [
        asyncio.create_task(_consumer(i), name=f"pipeline-consumer-{i}")
        for i in range(num_consumers)
    ]
    persister_task = asyncio.create_task(_persister(), name="pipeline-persister")
    all_tasks = [producer_task, *consumer_tasks, persister_task]

    try:
        # Producer finishes (or crashes) → it has queued one sentinel per consumer.
        await producer_task
        # All consumers drain the queue + their sentinel, then exit.
        await asyncio.gather(*consumer_tasks)
        # No more results will be produced → tell the persister to drain & exit.
        # Every result is already on persist_q ahead of this sentinel (consumers
        # finished), so the persister flushes on the sentinel.
        await persist_q.put(_SENTINEL)
        await persister_task
    finally:
        # Never leak child coroutines. On the happy path every task is already
        # done (this is a no-op). On cancellation (e.g. the caller's per-round
        # wait_for deadline) or a child BaseException escaping the gather, cancel
        # whatever is still pending and drain it — otherwise orphaned consumers
        # keep burning BRAIN slots and the persister keeps writing after the
        # session is over (the exact zombie class this project fights).
        for t in all_tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*all_tasks, return_exceptions=True)

    return stats
