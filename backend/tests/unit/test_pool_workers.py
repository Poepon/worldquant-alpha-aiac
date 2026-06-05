"""Phase 1b B3 — S/E per-candidate processor tests (mock workflow, no brain)."""
import pytest

from backend.agents.graph.state import MiningState, AlphaCandidate
from backend.models import CandidateQueue
from backend.pool import workers


class _FakeWorkflow:
    def __init__(self, sim_state=None, eval_state=None):
        self._sim = sim_state
        self._eval = eval_state

    async def run_simulate(self, state, config=None):
        return self._sim

    async def run_evaluate(self, state, config=None):
        return self._eval


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _state_with(cand, trace=None):
    return MiningState(task_id=1, region="USA",
                       pending_alphas=[AlphaCandidate(**cand)],
                       trace_steps=trace or [])


@pytest.mark.asyncio
async def test_s_process_one_extracts_structured_sim_result():
    row = CandidateQueue(task_id=1, region="USA", expression="x")
    sim_state = _state_with({
        "expression": "x", "is_valid": True,
        "metrics": {"sharpe": 1.4, "fitness": 0.9},
        "simulation_success": True, "alpha_id": "AID1",
    })
    out = await workers.s_process_one(_FakeWorkflow(sim_state=sim_state), row, {}, {})
    assert out["sim_result"]["metrics"] == {"sharpe": 1.4, "fitness": 0.9}
    assert out["sim_result"]["simulation_success"] is True
    assert out["sim_result"]["alpha_id"] == "AID1"
    assert out["sim_result"]["simulation_error"] is None


@pytest.mark.asyncio
async def test_s_process_one_failed_sim():
    row = CandidateQueue(task_id=1, region="USA", expression="x")
    sim_state = _state_with({
        "expression": "x", "is_valid": True, "metrics": {},
        "simulation_success": False, "simulation_error": "BRAIN 500",
    })
    out = await workers.s_process_one(_FakeWorkflow(sim_state=sim_state), row, {}, {})
    assert out["sim_result"]["simulation_success"] is False
    assert out["sim_result"]["simulation_error"] == "BRAIN 500"
    assert out["sim_result"]["metrics"] == {}


@pytest.mark.asyncio
async def test_e_process_one_builds_simresult():
    row = CandidateQueue(
        task_id=1, region="USA", universe="TOP3000", dataset_id="pv1", expression="x",
        sim_result={"metrics": {"sharpe": 1.6}, "simulation_success": True, "alpha_id": "AID1"},
    )
    eval_state = _state_with({
        "expression": "x", "is_valid": True,
        "metrics": {"sharpe": 1.6, "score": 80},
        "simulation_success": True, "quality_status": "PASS", "alpha_id": "AID1",
    })
    result = await workers.e_process_one(_FakeWorkflow(eval_state=eval_state), row, {}, {})
    assert result.verdict == "PASS"
    assert result.ok is True
    assert result.metrics == {"sharpe": 1.6, "score": 80}
    assert result.state is eval_state
    assert result.candidate.expression == "x"


@pytest.mark.asyncio
async def test_e_process_one_fail_verdict_carries_error():
    row = CandidateQueue(task_id=1, region="USA", expression="x",
                         sim_result={"metrics": {}, "simulation_success": False})
    eval_state = _state_with({
        "expression": "x", "is_valid": True, "metrics": {},
        "simulation_success": False, "simulation_error": "sim failed",
        "quality_status": "FAIL",
    })
    result = await workers.e_process_one(_FakeWorkflow(eval_state=eval_state), row, {}, {})
    assert result.verdict == "FAIL"
    assert result.ok is False
    assert result.error == "sim failed"


@pytest.mark.asyncio
async def test_persist_eval_invokes_persister_with_result():
    eval_state = _state_with({"expression": "x", "is_valid": True,
                              "quality_status": "PASS", "simulation_success": True})
    from backend.agents.pipeline.types import Candidate, SimResult
    result = SimResult(candidate=Candidate(expression="x", context={}, trace_records=[], payload=eval_state),
                       ok=True, metrics={"sharpe": 1.6}, verdict="PASS", trace_records=[],
                       error=None, state=eval_state)
    captured = []

    async def fake_persister(session, results):
        captured.append(results)
        return len(results)

    n = await workers.persist_eval(result, persister=fake_persister, session_factory=_FakeSession)
    assert n == 1
    assert captured == [[result]]
