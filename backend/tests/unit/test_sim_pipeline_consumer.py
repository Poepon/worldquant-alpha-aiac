"""Unit tests for the pipeline consumer stages + sim/eval sub-graphs
(Sub-phase 0 / Unit 2b)."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.agents.graph.state import AlphaCandidate
from backend.agents.graph.workflow import MiningWorkflow
from backend.agents.pipeline import Candidate, SimResult, build_consumer_stages


# --- sub-graph topology -----------------------------------------------------

def test_sim_and_eval_subgraphs_are_single_node():
    wf = MiningWorkflow(db=MagicMock(), brain=MagicMock(), llm_service=MagicMock())
    assert set(wf._sim_graph.nodes.keys()) == {"simulate"}
    assert set(wf._eval_graph.nodes.keys()) == {"evaluate"}
    # They compile.
    assert wf._sim_graph.compile() is not None
    assert wf._eval_graph.compile() is not None
    # The full graph and generation graph are untouched.
    assert "simulate" in wf._graph.nodes
    assert "simulate" not in wf._gen_graph.nodes


# --- consumer stages --------------------------------------------------------

class _FakeWorkflow:
    def __init__(self, sim_state, eval_state):
        self._sim, self._eval = sim_state, eval_state
        self.sim_input = None
        self.eval_input = None

    async def run_simulate(self, state, config=None):
        self.sim_input = state
        return self._sim

    async def run_evaluate(self, state, config=None):
        self.eval_input = state
        return self._eval


def _candidate():
    # payload stands in for the sim-ready MiningState the producer emits.
    return Candidate(expression="close/open", context={"region": "USA"},
                     payload=SimpleNamespace(tag="sim-ready-state"))


@pytest.mark.asyncio
async def test_consumer_passes_payload_to_simulate_then_eval_state_through():
    sim_state = SimpleNamespace(tag="post-sim")
    eval_state = SimpleNamespace(
        pending_alphas=[AlphaCandidate(
            expression="close/open", is_valid=True, simulation_success=True,
            quality_status="PASS", metrics={"sharpe": 1.5, "fitness": 1.1},
        )],
        trace_steps=[{"step": "SIMULATE"}, {"step": "EVALUATE"}],
    )
    wf = _FakeWorkflow(sim_state, eval_state)
    simulate, evaluate = build_consumer_stages(wf)
    cand = _candidate()

    out_sim = await simulate(cand)
    # simulate runs the sim sub-graph on the candidate's sim-ready payload.
    assert wf.sim_input is cand.payload
    assert out_sim is sim_state

    result = await evaluate(cand, out_sim)
    # evaluate runs the eval sub-graph on the post-sim state.
    assert wf.eval_input is sim_state
    assert isinstance(result, SimResult)
    assert result.ok is True
    assert result.verdict == "PASS"
    assert result.metrics == {"sharpe": 1.5, "fitness": 1.1}
    assert result.trace_records == [{"step": "SIMULATE"}, {"step": "EVALUATE"}]
    assert result.state is eval_state
    assert result.error is None


@pytest.mark.asyncio
async def test_consumer_failed_sim_sets_error_not_ok():
    eval_state = SimpleNamespace(
        pending_alphas=[AlphaCandidate(
            expression="bad", is_valid=True, simulation_success=False,
            quality_status="FAIL", simulation_error="BRAIN 400",
        )],
        trace_steps=[],
    )
    wf = _FakeWorkflow(SimpleNamespace(), eval_state)
    simulate, evaluate = build_consumer_stages(wf)
    cand = _candidate()

    result = await evaluate(cand, await simulate(cand))
    assert result.ok is False
    assert result.verdict == "FAIL"
    assert result.error == "BRAIN 400"
    assert result.state is eval_state


@pytest.mark.asyncio
async def test_consumer_empty_pending_is_safe():
    eval_state = SimpleNamespace(pending_alphas=[], trace_steps=[])
    wf = _FakeWorkflow(SimpleNamespace(), eval_state)
    simulate, evaluate = build_consumer_stages(wf)
    cand = _candidate()

    result = await evaluate(cand, await simulate(cand))
    assert result.ok is False
    assert result.verdict is None
    assert result.metrics == {}


@pytest.mark.asyncio
async def test_consumer_handles_dict_state():
    """LangGraph ainvoke may return a dict rather than the Pydantic state."""
    eval_state = {
        "pending_alphas": [AlphaCandidate(
            expression="x", is_valid=True, simulation_success=True,
            quality_status="PASS_PROVISIONAL", metrics={"sharpe": 1.2})],
        "trace_steps": [{"step": "EVALUATE"}],
    }
    wf = _FakeWorkflow({}, eval_state)
    simulate, evaluate = build_consumer_stages(wf)
    cand = _candidate()

    result = await evaluate(cand, await simulate(cand))
    assert result.ok is True
    assert result.verdict == "PASS_PROVISIONAL"
    assert result.trace_records == [{"step": "EVALUATE"}]
