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
async def test_producer_gen_timeout_skips_round():
    class _HungWF:
        async def run(self, **kwargs):
            await asyncio.sleep(30)        # hung distill/hypothesis/code_gen LLM
            return {"pending_alphas": [], "state": None, "trace_steps": []}

    rounds = {"n": 0}

    async def nri(db):
        if rounds["n"] >= 1:
            return None
        rounds["n"] += 1
        return {"task": object(), "dataset_id": "pv1", "fields": [], "operators": []}

    pushed = []

    async def push(c):
        pushed.append(c)

    produce = build_producer(
        session_factory=_sf, workflow_factory=lambda db: _HungWF(),
        next_round_inputs=nri, num_alphas=4, op_timeout=0.1,
    )
    await asyncio.wait_for(produce(push, lambda: False), timeout=10)
    assert pushed == []                    # the round timed out → skipped, no hang


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
