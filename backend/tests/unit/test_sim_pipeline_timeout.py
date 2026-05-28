"""Pipeline per-operation timeout backstop (2026-05-27, task 3735 hang).

A hung network await (BRAIN sim / self_corr, or an LLM gen / feedback call) must
fail that ONE operation cleanly under op_timeout instead of parking the asyncio
loop in select forever. op_timeout=None (default) → no bound → existing behaviour.
"""

import asyncio

import pytest

from backend.agents.pipeline.producer import build_producer
from backend.agents.pipeline.runner import run_pipeline_session
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
async def test_consumer_sim_timeout_fails_cleanly():
    async def produce(push, should_stop):
        await push(Candidate("c0", {}))

    async def hung_sim(cand):
        await asyncio.sleep(30)        # would hang the loop forever
        return {}

    async def evaluate(cand, sim):
        return SimResult(candidate=cand, ok=True)

    persisted = []

    async def persist(session, results):
        persisted.extend(results)
        return 0

    stats = await asyncio.wait_for(run_pipeline_session(
        produce=produce, simulate=hung_sim, evaluate=evaluate, persist=persist,
        session_factory=_sf, num_consumers=1, acquire_slot=_acq, release_slot=_rel,
        op_timeout=0.1,
    ), timeout=10)

    assert stats["errors"] == 1            # sim timed out → recorded as a failure
    assert stats["simulated"] == 0         # never counted as a spent sim
    assert len(persisted) == 1 and persisted[0].ok is False


@pytest.mark.asyncio
async def test_consumer_eval_timeout_fails_cleanly():
    async def produce(push, should_stop):
        await push(Candidate("c0", {}))

    async def simulate(cand):
        return {"ok": True}

    async def hung_eval(cand, sim):
        await asyncio.sleep(30)            # e.g. a hung self_corr BRAIN call
        return SimResult(candidate=cand, ok=True)

    persisted = []

    async def persist(session, results):
        persisted.extend(results)
        return 0

    stats = await asyncio.wait_for(run_pipeline_session(
        produce=produce, simulate=simulate, evaluate=hung_eval, persist=persist,
        session_factory=_sf, num_consumers=1, acquire_slot=_acq, release_slot=_rel,
        op_timeout=0.1,
    ), timeout=10)

    assert stats["simulated"] == 1         # the sim succeeded
    assert stats["errors"] == 1            # the evaluate timed out
    assert len(persisted) == 1 and persisted[0].ok is False


@pytest.mark.asyncio
async def test_producer_gen_timeout_ends_generation():
    """A hung generation round must END generation (not `continue` reusing the
    producer's possibly-poisoned shared asyncpg session): the next round is NOT
    attempted, and the producer returns without hanging."""
    class _HungWF:
        def __init__(self):
            self.run_calls = 0
            self._hyp_graph = "built"      # skip the split producer's pre-build
            self._codegen_graph = "built"

        async def run(self, **kwargs):     # stage-1 hypothesis call hangs
            assert kwargs.get("stop_after_hypothesis")
            self.run_calls += 1
            await asyncio.sleep(30)        # hung distill/hypothesis LLM
            return {"state": None}

        async def run_codegen(self, state, config=None):  # never reached (stage-1 hangs)
            return {"pending_alphas": [], "trace_steps": []}

    wf = _HungWF()
    rounds = {"n": 0}

    async def nri(db):                     # offers 3 rounds; break must stop at 1
        if rounds["n"] >= 3:
            return None
        rounds["n"] += 1
        return {"task": object(), "dataset_id": "pv1", "fields": [], "operators": []}

    pushed = []

    async def push(c):
        pushed.append(c)

    produce = build_producer(
        session_factory=_sf, workflow_factory=lambda db: wf,
        next_round_inputs=nri, num_alphas=4, op_timeout=0.1,
    )
    await asyncio.wait_for(produce(push, lambda: False), timeout=10)
    assert pushed == []                    # no candidates, no hang
    assert wf.run_calls == 1               # ended after the 1st timeout (break, not continue)


@pytest.mark.asyncio
async def test_producer_next_round_inputs_timeout_ends_cleanly():
    """A hung next_round_inputs (DB op on the producer's shared, possibly-poisoned
    asyncpg session) must NOT park the loop forever: op_timeout fires, the outer
    handler runs the finally (sentinels sent), the code-producers drain off the
    empty hyp_q, and produce() RETURNS instead of deadlocking (task 3737).

    The proof is that ``await asyncio.wait_for(produce(...), 10)`` returns at all —
    if the sentinels weren't sent, the internal code-producer would block on
    ``hyp_q.get()`` forever and this would raise TimeoutError."""
    class _WF:
        def __init__(self):
            self._hyp_graph = "built"
            self._codegen_graph = "built"

        async def run(self, **kwargs):
            return {"state": object()}

        async def run_codegen(self, state, config=None):
            return {"pending_alphas": [], "trace_steps": []}

    wf = _WF()
    calls = {"n": 0}

    async def nri(db):                     # hangs on the very first probe
        calls["n"] += 1
        await asyncio.sleep(30)            # hung DB op (poisoned session / lock)
        return {"task": object(), "dataset_id": "pv1", "fields": [], "operators": []}

    pushed = []

    async def push(c):
        pushed.append(c)

    produce = build_producer(
        session_factory=_sf, workflow_factory=lambda db: wf,
        next_round_inputs=nri, num_alphas=4, code_producer_count=2, op_timeout=0.1,
    )
    await asyncio.wait_for(produce(push, lambda: False), timeout=10)
    assert calls["n"] == 1                  # timed out on the 1st probe; no retry
    assert pushed == []                     # nothing produced, no hang


@pytest.mark.asyncio
async def test_drain_feedback_handler_timeout_breaks_drain():
    """A hung F2 feedback handler (R1B mutate / G5 crossover) must END the drain
    on op_timeout — NOT log+continue. The previous log+continue path caused a
    permanent freeze on task 3738, 2026-05-28: wait_for cancelled the mutate
    handler mid asyncpg query, poisoned the producer's SHARED db session
    (dc7c8e5 class), and the next drain iteration hung on that same session with
    no timer (loop parked in select forever). Mirrors the gen-op break-on-timeout.

    Proof: when 2 events are queued and the FIRST handler hangs, the SECOND
    event is NOT consumed (drain broke), and produce() returns cleanly within
    op_timeout instead of freezing."""
    class _FakeCtx:
        def __init__(self, events):
            self._events = list(events)
            self.primary_done = False
            self.events_done = 0
        def mark_primary_done(self):
            self.primary_done = True
        async def next_event(self):
            return self._events.pop(0) if self._events else None
        def event_done(self):
            self.events_done += 1

    class _WF:
        def __init__(self):
            self._hyp_graph = "built"
            self._codegen_graph = "built"

    async def nri(db):                     # no main rounds; drain starts immediately
        return None

    handled_log = []

    async def hung_handler(event, push, db, wf):
        handled_log.append(("started", event))
        await asyncio.sleep(30)            # hang past op_timeout
        handled_log.append(("done", event))  # must NOT reach this

    ctx = _FakeCtx(events=["ev_first", "ev_second"])
    produce = build_producer(
        session_factory=_sf, workflow_factory=lambda db: _WF(),
        next_round_inputs=nri, num_alphas=4,
        handle_feedback=hung_handler, op_timeout=0.1,
    )
    # Critical: produce() must RETURN (drain broke after the first timeout),
    # not hang waiting on the second event. If the bug were still present,
    # asyncio.wait_for here would itself time out.
    await asyncio.wait_for(produce(lambda c: None, lambda: False, ctx), timeout=10)
    assert handled_log == [("started", "ev_first")]   # only first handler started
    assert ctx.events_done == 1                       # event_done() called once (finally)
    assert ctx._events == ["ev_second"]               # second event NOT consumed (break worked)


@pytest.mark.asyncio
async def test_no_timeout_when_op_timeout_none():
    """op_timeout=None → operations run unbounded (existing behaviour); a fast
    sim completes normally."""
    async def produce(push, should_stop):
        await push(Candidate("c0", {}))

    async def simulate(cand):
        return {"ok": True}

    async def evaluate(cand, sim):
        return SimResult(candidate=cand, ok=True)

    persisted = []

    async def persist(session, results):
        persisted.extend(results)
        return 0

    stats = await asyncio.wait_for(run_pipeline_session(
        produce=produce, simulate=simulate, evaluate=evaluate, persist=persist,
        session_factory=_sf, num_consumers=1, acquire_slot=_acq, release_slot=_rel,
        # op_timeout omitted → None
    ), timeout=10)
    assert stats["simulated"] == 1 and stats["errors"] == 0
    assert len(persisted) == 1 and persisted[0].ok is True
