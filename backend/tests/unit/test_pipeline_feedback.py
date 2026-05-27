"""F2-1: runner feedback-loop infrastructure + quiescence termination.

These tests exercise the HARD part of the F2 feedback channel — the cyclic
producer⇄persister dependency and its quiescence-based termination — with pure
fakes (no DB / BRAIN / LangGraph). The real R1b/G5 classify+handle wiring lands
in F2-2..4; here we prove the runner machinery can't deadlock or hang.

Accounting under test (runner ``outstanding`` work-units): +1 per pushed
candidate, +1 per queued feedback event, -1 when a result is persister-processed
/ an event is producer-handled. Quiescence = outstanding 0 after primary gen.
"""

import asyncio

import pytest

from backend.agents.pipeline.runner import run_pipeline_session
from backend.agents.pipeline.types import (
    FEEDBACK_RETRY,
    Candidate,
    FeedbackEvent,
    SimResult,
)


# --------------------------------------------------------------------------- #
# Fakes                                                                        #
# --------------------------------------------------------------------------- #
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
    return {"expr": cand.expression}


async def _evaluate(cand, sim):
    # Carry the candidate's retry budget onto the result metrics so the
    # persister-side classifier can decide whether to feed back a retry.
    rl = (cand.context or {}).get("retry_left", 0)
    return SimResult(candidate=cand, ok=True, metrics={"retry_left": rl}, state=None)


def _make_persist(sink):
    async def _persist(session, results):
        for r in results:
            sink["n"] += 1
            sink["exprs"].append(r.candidate.expression if r.candidate else None)
        return len(results)

    return _persist


def _classify_retry_while_budget(result):
    """Emit a RETRY event while the result still has retry budget."""
    rl = (result.metrics or {}).get("retry_left", 0)
    if rl and rl > 0:
        return FeedbackEvent(kind=FEEDBACK_RETRY, result=result)
    return None


def _make_produce(candidates, *, handle=None):
    """A produce fake mirroring build_producer's protocol: push primary
    candidates, then (if a feedback_ctx is supplied) run the drain loop."""

    async def produce(push, should_stop, feedback_ctx=None):
        for c in candidates:
            if should_stop():
                break
            await push(c)
        if feedback_ctx is not None and handle is not None:
            feedback_ctx.mark_primary_done()
            while not should_stop():
                ev = await feedback_ctx.next_event()
                if ev is None:
                    break
                try:
                    await handle(ev, push)
                finally:
                    feedback_ctx.event_done()

    return produce


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_feedback_cycle_terminates_and_regenerates():
    """THE core test: a FAIL→retry cycle (persister feeds back, producer
    regenerates) must terminate at quiescence and the derived candidates must
    be simulated+persisted. Each primary candidate retries exactly once."""
    sink = {"n": 0, "exprs": []}

    async def handle(ev, push):
        parent = ev.result.candidate
        # Retried child has no further budget → no infinite loop.
        await push(Candidate(parent.expression + "_r", {"retry_left": 0}))

    produce = _make_produce(
        [Candidate("c0", {"retry_left": 1}), Candidate("c1", {"retry_left": 1})],
        handle=handle,
    )

    stats = await asyncio.wait_for(
        run_pipeline_session(
            produce=produce,
            simulate=_simulate,
            evaluate=_evaluate,
            persist=_make_persist(sink),
            session_factory=_sf,
            num_consumers=2,
            acquire_slot=_acq,
            release_slot=_rel,
            classify_feedback=_classify_retry_while_budget,
        ),
        timeout=10.0,
    )

    assert stats["produced"] == 4          # 2 primary + 2 retries
    assert stats["simulated"] == 4
    assert stats["persisted"] == 4
    assert stats["feedback_events"] == 2
    assert sink["n"] == 4
    assert sorted(sink["exprs"]) == ["c0", "c0_r", "c1", "c1_r"]


@pytest.mark.asyncio
async def test_feedback_active_but_no_events_quiesces():
    """Feedback active but every result is clean (classify→None) → the drain
    phase quiesces immediately (mark_primary_done with outstanding draining to
    0), no hang, feedback_events == 0."""
    sink = {"n": 0, "exprs": []}

    async def handle(ev, push):  # never called
        raise AssertionError("no event expected")

    produce = _make_produce(
        [Candidate("a", {"retry_left": 0}), Candidate("b", {"retry_left": 0})],
        handle=handle,
    )

    stats = await asyncio.wait_for(
        run_pipeline_session(
            produce=produce,
            simulate=_simulate,
            evaluate=_evaluate,
            persist=_make_persist(sink),
            session_factory=_sf,
            num_consumers=2,
            acquire_slot=_acq,
            release_slot=_rel,
            classify_feedback=_classify_retry_while_budget,
        ),
        timeout=10.0,
    )
    assert stats["produced"] == 2
    assert stats["simulated"] == 2
    assert stats["feedback_events"] == 0


@pytest.mark.asyncio
async def test_feedback_multi_hop_bounded_fanout_terminates():
    """A candidate that retries 3 times (budget decrements each hop) must
    terminate — proves bounded multi-level feedback quiesces, not just 1 hop."""
    sink = {"n": 0, "exprs": []}

    async def handle(ev, push):
        parent = ev.result.candidate
        rl = (parent.context or {}).get("retry_left", 0)
        await push(Candidate(parent.expression + "_r", {"retry_left": rl - 1}))

    produce = _make_produce([Candidate("c", {"retry_left": 3})], handle=handle)

    stats = await asyncio.wait_for(
        run_pipeline_session(
            produce=produce,
            simulate=_simulate,
            evaluate=_evaluate,
            persist=_make_persist(sink),
            session_factory=_sf,
            num_consumers=1,
            acquire_slot=_acq,
            release_slot=_rel,
            classify_feedback=_classify_retry_while_budget,
        ),
        timeout=10.0,
    )
    # 1 primary + 3 retries (budget 3→2→1→0); the rl==0 result emits no event.
    assert stats["produced"] == 4
    assert stats["simulated"] == 4
    assert stats["feedback_events"] == 3
    assert sink["n"] == 4


@pytest.mark.asyncio
async def test_feedback_zero_candidates_no_hang():
    """Producer emits nothing → primary_done with outstanding 0 → immediate
    quiescence sentinel, no block on the empty feedback queue."""
    sink = {"n": 0, "exprs": []}

    async def handle(ev, push):
        raise AssertionError("no event expected")

    produce = _make_produce([], handle=handle)

    stats = await asyncio.wait_for(
        run_pipeline_session(
            produce=produce,
            simulate=_simulate,
            evaluate=_evaluate,
            persist=_make_persist(sink),
            session_factory=_sf,
            num_consumers=2,
            acquire_slot=_acq,
            release_slot=_rel,
            classify_feedback=_classify_retry_while_budget,
        ),
        timeout=10.0,
    )
    assert stats["produced"] == 0
    assert stats["feedback_events"] == 0


@pytest.mark.asyncio
async def test_feedback_inactive_path_unchanged():
    """classify_feedback=None → the pre-F2 path: produce called with the
    2-arg signature, no feedback_* stats keys, no feedback machinery."""
    sink = {"n": 0, "exprs": []}
    seen_args = {}

    async def produce(push, should_stop):  # 2-arg legacy signature
        seen_args["argc"] = 2
        await push(Candidate("x", {}))
        await push(Candidate("y", {}))

    stats = await asyncio.wait_for(
        run_pipeline_session(
            produce=produce,
            simulate=_simulate,
            evaluate=_evaluate,
            persist=_make_persist(sink),
            session_factory=_sf,
            num_consumers=2,
            acquire_slot=_acq,
            release_slot=_rel,
            # classify_feedback omitted → inactive
        ),
        timeout=10.0,
    )
    assert seen_args["argc"] == 2
    assert stats["produced"] == 2
    assert stats["simulated"] == 2
    assert "feedback_events" not in stats
    assert "feedback_handled" not in stats


@pytest.mark.asyncio
async def test_feedback_stop_during_drain_no_hang():
    """A cooperative stop during the drain phase must exit promptly (not block
    forever on the queue waiting for a quiescence sentinel that won't come)."""
    sink = {"n": 0, "exprs": []}
    stop = asyncio.Event()

    async def handle(ev, push):
        stop.set()          # request stop; push nothing further
        # do not push — the drain loop should exit on should_stop next check

    produce = _make_produce([Candidate("c", {"retry_left": 1})], handle=handle)

    stats = await asyncio.wait_for(
        run_pipeline_session(
            produce=produce,
            simulate=_simulate,
            evaluate=_evaluate,
            persist=_make_persist(sink),
            session_factory=_sf,
            num_consumers=1,
            acquire_slot=_acq,
            release_slot=_rel,
            stop_event=stop,
            classify_feedback=_classify_retry_while_budget,
        ),
        timeout=10.0,
    )
    # The single primary candidate was simulated; its retry event was handled
    # (which set stop), and the drain exited without hanging.
    assert stats["produced"] >= 1
    assert stats["feedback_events"] == 1


@pytest.mark.asyncio
async def test_async_classify_feedback_rejected():
    """An async classify_feedback would return a never-None coroutine (queued as
    a bogus event) and break the await-free atomicity → fail loud at activation."""

    async def _async_classify(result):  # wrong: must be sync
        return None

    with pytest.raises(TypeError, match="SYNC callable"):
        await run_pipeline_session(
            produce=_make_produce([]),
            simulate=_simulate,
            evaluate=_evaluate,
            persist=_make_persist({"n": 0, "exprs": []}),
            session_factory=_sf,
            num_consumers=1,
            acquire_slot=_acq,
            release_slot=_rel,
            classify_feedback=_async_classify,
        )


@pytest.mark.asyncio
async def test_run_flat_pipeline_session_both_or_neither_feedback():
    """run_flat_pipeline_session rejects a one-sided feedback wiring (classify
    without handle, or vice versa) — the persister would queue events the
    producer never drains, hanging on quiescence."""
    from backend.agents.pipeline.producer import run_flat_pipeline_session

    async def _nri(db):
        return None

    with pytest.raises(ValueError, match="provided together"):
        await run_flat_pipeline_session(
            session_factory=_sf,
            producer_workflow_factory=lambda db: object(),
            consumer_workflow=object(),
            next_round_inputs=_nri,
            run_id=1,
            num_alphas=1,
            num_consumers=1,
            classify_feedback=lambda r: None,   # only one side
        )
