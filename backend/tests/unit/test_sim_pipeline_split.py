"""Sub-phase 3: split producer (HYPOTHESIS seam) — build_split_producer.

Drives the two-stage internal pipeline (hyp-producer → hyp_q → N code-producers)
with a fake workflow (no DB / LLM / LangGraph), verifying the seam wiring,
candidate push, validity filter, termination, and the target cap.
"""

import asyncio
from types import SimpleNamespace

import pytest

from backend.agents.pipeline.producer import build_split_producer


class _NullSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _sf():
    return _NullSession()


class _FakeWF:
    """Fake MiningWorkflow: run(stop_after_hypothesis=True) returns a hyp-state;
    run_codegen(state) returns pending_alphas via the injected ``cg``."""

    def __init__(self, n_rounds, cg):
        self._n = n_rounds
        self._cg = cg
        self._hyp_graph = "built"      # pretend pre-built → skip lazy build
        self._codegen_graph = "built"
        self.run_calls = 0
        self.codegen_calls = 0

    async def run(self, *, task, dataset_id, fields, operators, num_alphas,
                  config, generate_only, stop_after_hypothesis):
        assert generate_only and stop_after_hypothesis
        self.run_calls += 1
        return {"state": {"dataset_id": dataset_id, "pending_alphas": []}}

    async def run_codegen(self, state, config=None):
        self.codegen_calls += 1
        return {"pending_alphas": self._cg(state), "trace_steps": [{"step_type": "CODE_GEN"}]}


def _make_nri(k):
    """next_round_inputs returning k rounds then None."""
    state = {"n": 0}

    async def nri(db):
        if state["n"] >= k:
            return None
        state["n"] += 1
        return {"task": SimpleNamespace(id=7), "dataset_id": f"ds{state['n']}",
                "fields": [], "operators": []}

    return nri


def _valid(*exprs):
    return [SimpleNamespace(expression=e, is_valid=True) for e in exprs]


@pytest.mark.asyncio
async def test_split_two_stage_pushes_validated_candidates():
    pushed = []

    async def push(c):
        pushed.append(c)

    wf = _FakeWF(3, lambda st: _valid("a", "b"))   # each hypothesis → 2 valid alphas
    produce = build_split_producer(
        session_factory=_sf, workflow_factory=lambda db: wf,
        next_round_inputs=_make_nri(3), num_alphas=4, code_producer_count=2,
    )
    await asyncio.wait_for(produce(push, lambda: False, None), timeout=10)

    assert wf.run_calls == 3          # 3 hypothesis rounds (4th nri → None → stop)
    assert wf.codegen_calls == 3      # each hypothesis expanded once (the seam)
    assert len(pushed) == 6           # 3 × 2 valid offspring candidates
    assert {c.context["dataset_id"] for c in pushed} == {"ds1", "ds2", "ds3"}
    assert all(c.trace_records for c in pushed)   # gen trace carried


@pytest.mark.asyncio
async def test_split_filters_invalid_codegen_output():
    pushed = []

    async def push(c):
        pushed.append(c)

    def cg(st):
        return [SimpleNamespace(expression="ok", is_valid=True),
                SimpleNamespace(expression="bad", is_valid=False)]

    wf = _FakeWF(2, cg)
    produce = build_split_producer(
        session_factory=_sf, workflow_factory=lambda db: wf,
        next_round_inputs=_make_nri(2), num_alphas=4, code_producer_count=1,
    )
    await asyncio.wait_for(produce(push, lambda: False, None), timeout=10)
    assert [c.expression for c in pushed] == ["ok", "ok"]   # 2 rounds, invalid dropped


@pytest.mark.asyncio
async def test_split_terminates_on_cursor_exhaustion():
    """next_round_inputs returning None promptly ends stage 1 → sentinels → stage
    2 drains → produce returns (no hang)."""
    pushed = []

    async def push(c):
        pushed.append(c)

    wf = _FakeWF(0, lambda st: _valid("x"))
    produce = build_split_producer(
        session_factory=_sf, workflow_factory=lambda db: wf,
        next_round_inputs=_make_nri(0), num_alphas=4, code_producer_count=3,
    )
    await asyncio.wait_for(produce(push, lambda: False, None), timeout=10)
    assert pushed == [] and wf.codegen_calls == 0


@pytest.mark.asyncio
async def test_split_respects_target_candidates_cap():
    pushed = []

    async def push(c):
        pushed.append(c)

    wf = _FakeWF(100, lambda st: _valid("a", "b"))   # plenty of rounds available
    produce = build_split_producer(
        session_factory=_sf, workflow_factory=lambda db: wf,
        next_round_inputs=_make_nri(100), num_alphas=4, code_producer_count=1,
        target_candidates=5,
    )
    await asyncio.wait_for(produce(push, lambda: False, None), timeout=10)
    # Stops near the target (may overshoot one batch of 2, like the legacy loop).
    assert 5 <= len(pushed) <= 6
    assert wf.run_calls < 100        # did NOT run all 100 rounds


@pytest.mark.asyncio
async def test_split_stops_on_should_stop():
    pushed = []

    async def push(c):
        pushed.append(c)

    wf = _FakeWF(100, lambda st: _valid("a"))
    produce = build_split_producer(
        session_factory=_sf, workflow_factory=lambda db: wf,
        next_round_inputs=_make_nri(100), num_alphas=4, code_producer_count=2,
    )
    await asyncio.wait_for(produce(push, lambda: True, None), timeout=10)  # stop immediately
    assert pushed == []
