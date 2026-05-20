"""node_simulate must record a SIMULATE trace step on its silent early-return
paths (2026-05-20).

Previously node_simulate returned early (no-valid-alphas / all-deduped / all
pre-sim-filtered) WITHOUT recording a trace step, and the real BRAIN-sim phase
isn't traced either — so during the multi-minute sim gap the trace/UI looked
"frozen at VALIDATE". Each exit now emits a SIMULATE step (persisted
immediately) explaining why 0 alphas reached BRAIN.
"""
from __future__ import annotations

import pytest


class _CaptureTrace:
    def __init__(self):
        self.records = []

    async def persist_record(self, record):
        self.records.append(record)


@pytest.mark.asyncio
async def test_simulate_records_trace_when_no_valid_alphas():
    from backend.agents.graph.nodes.evaluation import node_simulate
    from backend.agents.graph.state import MiningState, AlphaCandidate

    state = MiningState(task_id=1, region="USA")
    state.pending_alphas = [
        AlphaCandidate(expression="close", is_valid=False),
        AlphaCandidate(expression="open", is_valid=False),
    ]
    cap = _CaptureTrace()
    config = {"configurable": {"trace_service": cap}}

    result = await node_simulate(state, brain=None, config=config)

    sim_recs = [r for r in cap.records if getattr(r, "step_type", None) == "SIMULATE"]
    assert sim_recs, "expected a SIMULATE trace step on the no-valid-alphas exit"
    assert sim_recs[0].output_data.get("skip_reason") == "no_valid_alphas_after_validation"
    assert sim_recs[0].output_data.get("simulated") == 0
    # Inputs carry the candidate accounting so the UI can show context.
    assert sim_recs[0].input_data.get("valid_to_simulate") == 0
    assert sim_recs[0].input_data.get("pending") == 2
    # The returned state-update is non-empty (carries trace_steps/step_order),
    # not the old bare `{}`.
    assert result


@pytest.mark.asyncio
async def test_simulate_no_trace_service_does_not_crash():
    """trace_service None (some dispatch paths) must still return cleanly."""
    from backend.agents.graph.nodes.evaluation import node_simulate
    from backend.agents.graph.state import MiningState, AlphaCandidate

    state = MiningState(task_id=1, region="USA")
    state.pending_alphas = [AlphaCandidate(expression="close", is_valid=False)]
    # config without trace_service
    result = await node_simulate(state, brain=None, config={"configurable": {}})
    assert isinstance(result, dict)
