"""Unit tests for the pipeline producer + FLAT assembly (Sub-phase 0 / Unit 2c)."""

import contextlib
from types import SimpleNamespace

import pytest

from backend.agents.graph.state import AlphaCandidate, MiningState
from backend.agents.pipeline import (
    Candidate, SimResult, build_producer, run_flat_pipeline_session,
)


@contextlib.asynccontextmanager
async def _session_factory():
    yield SimpleNamespace(tag="db")


class _FakeGenWorkflow:
    """workflow.run(generate_only=True) → {pending_alphas, state}."""

    def __init__(self, per_round):
        # per_round: list of lists of expressions to emit each round
        self._per_round = per_round
        self._i = 0
        self.run_calls = []

    async def run(self, *, task, dataset_id, fields, operators, num_alphas, config, generate_only):
        assert generate_only is True
        self.run_calls.append({"dataset_id": dataset_id, "num_alphas": num_alphas})
        exprs = self._per_round[self._i] if self._i < len(self._per_round) else []
        self._i += 1
        state = MiningState(
            task_id=getattr(task, "id", 1), region="USA", universe="TOP3000",
            dataset_id=dataset_id,
            pending_alphas=[AlphaCandidate(expression=e, is_valid=True) for e in exprs],
        )
        return {"pending_alphas": list(state.pending_alphas), "state": state, "trace_steps": []}


def _inputs_feeder(datasets):
    """Returns an async next_round_inputs that yields one round per dataset, then None."""
    seq = list(datasets)

    async def next_round_inputs(db):
        if not seq:
            return None
        ds = seq.pop(0)
        return {"task": SimpleNamespace(id=1), "dataset_id": ds, "fields": [], "operators": [], "config": None}

    return next_round_inputs


@pytest.mark.asyncio
async def test_producer_pushes_sim_ready_candidates_per_round():
    wf = _FakeGenWorkflow(per_round=[["a", "b"], ["c"]])
    pushed = []

    async def push(c):
        pushed.append(c)

    produce = build_producer(
        session_factory=_session_factory,
        workflow_factory=lambda db: wf,
        next_round_inputs=_inputs_feeder(["pv1", "anl4"]),
        num_alphas=10,
    )
    await produce(push, lambda: False)

    # 2 rounds → 2 + 1 = 3 candidates.
    assert len(pushed) == 3
    assert [c.expression for c in pushed] == ["a", "b", "c"]
    # Each candidate's payload is a sim-ready MiningState with ONE pending alpha.
    for c in pushed:
        assert isinstance(c, Candidate)
        assert len(c.payload.pending_alphas) == 1
        assert c.payload.pending_alphas[0].expression == c.expression
        assert c.payload.trace_steps == []
    # dataset_id threaded through context + the workflow saw num_alphas=10.
    assert pushed[0].context["dataset_id"] == "pv1"
    assert pushed[2].context["dataset_id"] == "anl4"
    assert wf.run_calls[0]["num_alphas"] == 10


@pytest.mark.asyncio
async def test_producer_stops_on_should_stop():
    wf = _FakeGenWorkflow(per_round=[["a"], ["b"], ["c"]])
    pushed = []
    calls = {"n": 0}

    async def push(c):
        pushed.append(c)

    def should_stop():
        calls["n"] += 1
        return calls["n"] > 1  # stop after the first loop check

    produce = build_producer(
        session_factory=_session_factory, workflow_factory=lambda db: wf,
        next_round_inputs=_inputs_feeder(["d1", "d2", "d3"]), num_alphas=4,
    )
    await produce(push, should_stop)
    assert len(pushed) <= 1  # stopped early


@pytest.mark.asyncio
async def test_producer_stops_when_should_continue_false():
    wf = _FakeGenWorkflow(per_round=[["a"], ["b"], ["c"]])
    pushed = []

    async def push(c):
        pushed.append(c)

    produce = build_producer(
        session_factory=_session_factory, workflow_factory=lambda db: wf,
        next_round_inputs=_inputs_feeder(["d1", "d2", "d3"]), num_alphas=4,
        should_continue=lambda: False,  # daily goal already met
    )
    await produce(push, lambda: False)
    assert pushed == []  # never generated a round


@pytest.mark.asyncio
async def test_producer_stops_when_inputs_exhausted():
    wf = _FakeGenWorkflow(per_round=[["a"], ["b"]])
    pushed = []

    async def push(c):
        pushed.append(c)

    produce = build_producer(
        session_factory=_session_factory, workflow_factory=lambda db: wf,
        next_round_inputs=_inputs_feeder(["only1"]), num_alphas=4,
    )
    await produce(push, lambda: False)
    assert len(pushed) == 1  # one dataset → one round


# --- full FLAT assembly end-to-end (fakes) ----------------------------------

class _FakeConsumerWorkflow:
    async def run_simulate(self, state, config=None):
        return state  # passthrough

    async def run_evaluate(self, state, config=None):
        # Mark the single candidate PASS.
        pa = state.pending_alphas if hasattr(state, "pending_alphas") else state["pending_alphas"]
        for a in pa:
            a.simulation_success = True
            a.quality_status = "PASS"
            a.metrics = {"sharpe": 1.4}
        return state


@pytest.mark.asyncio
async def test_run_flat_assembly_wiring():
    """run_flat_pipeline_session wires producer→consumer→persister end-to-end.
    Slot primitives + persist are injected so no Redis/BRAIN/DB is touched."""

    async def _acq():
        return True

    async def _rel():
        return None

    gen_wf = _FakeGenWorkflow(per_round=[["a", "b"], ["c", "d"]])
    persisted = []

    async def fake_persist(session, results):
        persisted.extend(results)
        return len([r for r in results if r.ok])

    stats = await run_flat_pipeline_session(
        session_factory=_session_factory,
        producer_workflow_factory=lambda db: gen_wf,
        consumer_workflow=_FakeConsumerWorkflow(),
        next_round_inputs=_inputs_feeder(["pv1", "anl4"]),
        run_id=7,
        num_alphas=4,
        num_consumers=2,
        persist_fn=fake_persist,
        acquire_slot=_acq,
        release_slot=_rel,
    )

    assert stats["produced"] == 4     # 2 + 2 candidates
    assert stats["simulated"] == 4    # all simulated
    assert stats["persisted"] == 4    # all PASS → persisted
    assert all(isinstance(r, SimResult) and r.ok for r in persisted)
    assert all(r.verdict == "PASS" for r in persisted)
