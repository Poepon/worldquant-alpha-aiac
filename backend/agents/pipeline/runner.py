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

from backend.agents.pipeline.types import Candidate, FeedbackEvent, SimResult

logger = logging.getLogger(__name__)

# Sentinel signalling "no more work" through a queue. A distinct object per
# run avoids any chance of a real payload comparing equal to it.
_SENTINEL = object()

# Type aliases for the injected stages.
PushFn = Callable[[Candidate], Awaitable[None]]
# produce(push, should_stop) — or produce(push, should_stop, feedback_ctx) when
# the F2 feedback loop is active. Arity varies, so the alias is left open.
ProduceFn = Callable[..., Awaitable[None]]
SimulateFn = Callable[[Candidate], Awaitable[Any]]
EvaluateFn = Callable[[Candidate, Any], Awaitable[SimResult]]
PersistFn = Callable[[Any, List[SimResult]], Awaitable[int]]
# F2: persister classifies a SimResult into 0/1 feedback event (DB-free).
ClassifyFeedbackFn = Callable[[SimResult], Optional[FeedbackEvent]]


class _FeedbackCtx:
    """Handle the runner hands to the producer's drain phase (F2).

    ``next_event`` blocks until the next feedback event (or None = quiescence,
    stop). ``mark_primary_done`` tells the runner primary generation finished
    (so the quiescence sentinel can fire once outstanding work drains).
    ``event_done`` releases one event's work unit after the producer handled it.
    """

    __slots__ = ("next_event", "mark_primary_done", "event_done")

    def __init__(self, next_event, mark_primary_done, event_done):
        self.next_event = next_event
        self.mark_primary_done = mark_primary_done
        self.event_done = event_done


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
    classify_feedback: Optional[ClassifyFeedbackFn] = None,
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
        classify_feedback: optional (F2) ``(SimResult) -> FeedbackEvent | None``.
            When set, the feedback loop is ACTIVE: per persisted result the
            persister classifies a feedback event onto an internal feedback
            queue, and the producer's drain phase handles it (closing the
            CoSTEER loop). The producer callable MUST accept the optional 3rd
            ``feedback_ctx`` arg and drain it (build_producer does this when its
            own ``handle_feedback`` is set — the two MUST be wired together).
            When None (default) the loop is INACTIVE and the path is
            byte-identical to the pre-F2 runner.

    Returns:
        stats dict: produced, simulated, persisted, errors, slot_timeouts,
        and (feedback active) feedback_events / feedback_handled.
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

    # --- F2 feedback loop (inactive unless classify_feedback is provided) ------
    _feedback_active = classify_feedback is not None
    # The persister calls classify_feedback await-free, BETWEEN its +1-event and
    # -1-result, so that block stays atomic (single-threaded asyncio → no
    # interleave → no premature quiescence). An async classify would (a) break
    # that atomicity and (b) return a never-None coroutine that gets queued as a
    # bogus "event". Fail loud rather than silently mis-fire the loop.
    if _feedback_active and asyncio.iscoroutinefunction(classify_feedback):
        raise TypeError(
            "classify_feedback must be a SYNC callable (it is invoked await-free "
            "inside the persister's atomic +1-event/-1-result accounting)"
        )
    # Unbounded ON PURPOSE: it must never block the persister's put, otherwise
    # producer(push→work_q)→consumer(→persist_q)→persister(→feedback_q)→producer
    # would form a cycle of bounded-blocking queues that can deadlock. Fan-out
    # is capped by the handlers (retry≤3/alpha, mutate≤2, offspring≤2), so an
    # unbounded feedback queue cannot grow without bound.
    feedback_q: Optional[asyncio.Queue] = asyncio.Queue() if _feedback_active else None
    if _feedback_active:
        stats["feedback_events"] = 0
        stats["feedback_handled"] = 0
        # Persist each result immediately so a PASS is COMMITTED before its
        # PASS_LANDED event reaches the producer's crossover DB query.
        persist_every = 1
    # Live "work units": +1 per pushed candidate and per queued feedback event;
    # -1 when a result is persister-processed / an event is producer-handled.
    # Single-threaded asyncio → a plain int needs no lock. Quiescence (and thus
    # the terminating sentinel) is outstanding == 0 after primary gen finished.
    wstate = {"outstanding": 0, "primary_done": False, "done_signalled": False}

    def _maybe_signal_done() -> None:
        if (
            _feedback_active
            and wstate["primary_done"]
            and wstate["outstanding"] == 0
            and not wstate["done_signalled"]
        ):
            wstate["done_signalled"] = True
            feedback_q.put_nowait(_SENTINEL)  # unbounded → never blocks

    def _should_stop() -> bool:
        return stop_event is not None and stop_event.is_set()

    async def _push(candidate: Candidate) -> None:
        if _feedback_active:
            wstate["outstanding"] += 1  # before the (maybe-blocking) put
        await work_q.put(candidate)
        stats["produced"] += 1

    async def _next_feedback() -> Optional[FeedbackEvent]:
        # Block for the next event, but wake every few seconds to re-check the
        # cooperative stop: on an ABRUPT stop the quiescence sentinel may never
        # fire (consumers drop work without producing a result → outstanding
        # never returns to 0), and the producer must not strand here. A
        # cancelled empty get() loses nothing (asyncio.Queue re-wakes the next
        # waiter), so polling is safe.
        while True:
            if _should_stop():
                return None
            try:
                item = await asyncio.wait_for(feedback_q.get(), timeout=2.0)
            except asyncio.TimeoutError:
                continue
            try:
                return None if item is _SENTINEL else item
            finally:
                feedback_q.task_done()

    def _mark_primary_done() -> None:
        # Generation is done. If no work is outstanding (0 candidates, or all
        # already drained) this fires the terminating sentinel immediately so
        # the producer's drain loop doesn't block forever.
        wstate["primary_done"] = True
        _maybe_signal_done()

    def _event_done() -> None:
        wstate["outstanding"] -= 1
        _maybe_signal_done()

    _feedback_ctx = (
        _FeedbackCtx(_next_feedback, _mark_primary_done, _event_done)
        if _feedback_active
        else None
    )

    async def _producer() -> None:
        try:
            # Inactive path: call produce with the original 2-arg signature so
            # existing produce callables / fakes are untouched (byte-identical).
            if _feedback_active:
                await produce(_push, _should_stop, _feedback_ctx)
            else:
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
                # F2: this result is now COMMITTED (persist_every == 1 when the
                # feedback loop is active). Classify it into 0/1 feedback event
                # and release its work unit. Emit (+1) BEFORE release (-1) so
                # ``outstanding`` never transiently hits 0 while an event is
                # still pending (which would fire a premature done-sentinel).
                if _feedback_active and isinstance(item, SimResult):
                    ev = None
                    try:
                        ev = classify_feedback(item)
                    except Exception:  # noqa: BLE001 — classification never fatal
                        logger.exception("[pipeline] classify_feedback failed (skipped)")
                    if ev is not None:
                        wstate["outstanding"] += 1
                        stats["feedback_events"] += 1
                        feedback_q.put_nowait(ev)
                    wstate["outstanding"] -= 1
                    _maybe_signal_done()
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
