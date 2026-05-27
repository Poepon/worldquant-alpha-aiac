"""End-to-end FLAT pipeline assembly (run_flat_pipeline_session, with fakes).

The producer-level behaviour (two-stage split push / termination / target cap /
should_stop) lives in test_sim_pipeline_split.py; this file exercises the full
wiring producer→consumer→persister through run_flat_pipeline_session. (The
pre-2026-05-28 single-stage producer was removed; the producer is now always the
HYPOTHESIS-split two-stage one.)
"""

import contextlib
from types import SimpleNamespace

import pytest

from backend.agents.graph.state import AlphaCandidate
from backend.agents.pipeline import Candidate, SimResult, run_flat_pipeline_session


@contextlib.asynccontextmanager
async def _session_factory():
    yield SimpleNamespace(tag="db")


class _FakeGenWorkflow:
    """Split-producer fake: stage-1 run(stop_after_hypothesis=True) → post-hyp
    state carrying the round's exprs; stage-2 run_codegen(state) → validated
    pending_alphas. Sub-graph attrs are pre-set so the split's pre-build is a
    no-op."""

    def __init__(self, per_round):
        self._per_round = per_round
        self._i = 0
        self.run_calls = []
        self._hyp_graph = "built"       # skip the producer's lazy pre-build
        self._codegen_graph = "built"

    async def run(self, *, task, dataset_id, fields, operators, num_alphas, config,
                  generate_only, stop_after_hypothesis=False):
        assert generate_only and stop_after_hypothesis   # stage 1 only
        self.run_calls.append({"dataset_id": dataset_id, "num_alphas": num_alphas})
        exprs = self._per_round[self._i] if self._i < len(self._per_round) else []
        self._i += 1
        return {"state": {"dataset_id": dataset_id, "_exprs": exprs}}

    async def run_codegen(self, state, config=None):
        exprs = state.get("_exprs", []) if isinstance(state, dict) else []
        return {"pending_alphas": [AlphaCandidate(expression=e, is_valid=True) for e in exprs],
                "trace_steps": []}


class _FakeConsumerWorkflow:
    async def run_simulate(self, state, config=None):
        return state  # passthrough

    async def run_evaluate(self, state, config=None):
        pa = state.pending_alphas if hasattr(state, "pending_alphas") else state["pending_alphas"]
        for a in pa:
            a.simulation_success = True
            a.quality_status = "PASS"
            a.metrics = {"sharpe": 1.4}
        return state


def _inputs_feeder(datasets):
    seq = list(datasets)

    async def next_round_inputs(db):
        if not seq:
            return None
        ds = seq.pop(0)
        return {"task": SimpleNamespace(id=1), "dataset_id": ds,
                "fields": [], "operators": [], "config": None}

    return next_round_inputs


@pytest.mark.asyncio
async def test_run_flat_assembly_wiring():
    """run_flat_pipeline_session wires the split producer → consumer → persister
    end-to-end. Slot primitives + persist are injected so no Redis/BRAIN/DB is
    touched."""
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

    assert stats["produced"] == 4      # 2 rounds × 2 candidates
    assert stats["simulated"] == 4
    assert stats["persisted"] == 4
    assert all(isinstance(r, SimResult) and r.ok for r in persisted)
    assert all(r.verdict == "PASS" for r in persisted)
    # stage 1 ran one hypothesis round per dataset
    assert [c["dataset_id"] for c in gen_wf.run_calls] == ["pv1", "anl4"]
