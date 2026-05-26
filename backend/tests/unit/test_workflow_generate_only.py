"""Unit tests for MiningWorkflow.run(generate_only=True) — the pipeline
producer's generation-only path (Sub-phase 0 / Unit 2a).

Verifies the generation-only graph topology and the generate_only return
contract (validated pending_alphas + trace + state), without running the heavy
real generation nodes (that end-to-end path is exercised in the Unit 2c shadow
integration).
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.agents.graph.state import AlphaCandidate
from backend.agents.graph.workflow import MiningWorkflow


def _wf():
    return MiningWorkflow(db=MagicMock(), brain=MagicMock(), llm_service=MagicMock())


def _task():
    # config MUST be a real dict — run() does (task.config or {}).get(...) and
    # isinstance(task.config, dict) checks during initial-state construction.
    return SimpleNamespace(id=1, region="USA", universe="TOP3000", config={})


class _FakeApp:
    def __init__(self, final_state):
        self._fs = final_state
        self.invoked_with = None

    async def ainvoke(self, initial_state, config=None):
        self.invoked_with = initial_state
        return self._fs


class _FakeGraph:
    """Stands in for the compiled generation graph so we test run()'s
    generate_only branch without executing real LLM/RAG nodes."""

    def __init__(self, final_state):
        self.app = _FakeApp(final_state)

    def compile(self, **kwargs):
        return self.app


def test_generation_graph_has_gen_nodes_only():
    wf = _wf()
    nodes = set(wf._gen_graph.nodes.keys())
    assert {"rag_query", "distill_context", "hypothesis", "code_gen",
            "validate", "self_correct"} <= nodes
    # Sim/evaluate/persist must NOT be in the producer's graph.
    assert "simulate" not in nodes
    assert "evaluate" not in nodes
    assert "save_results" not in nodes


def test_full_graph_still_intact():
    """generate_only must not have disturbed the live full graph."""
    wf = _wf()
    nodes = set(wf._graph.nodes.keys())
    assert {"rag_query", "code_gen", "validate", "simulate", "evaluate",
            "save_results"} <= nodes


def test_gen_graph_compiles():
    wf = _wf()
    assert wf._gen_graph.compile() is not None


@pytest.mark.asyncio
async def test_generate_only_returns_validated_candidates_only():
    wf = _wf()
    final_state = SimpleNamespace(
        pending_alphas=[
            AlphaCandidate(expression="close/open", is_valid=True),
            AlphaCandidate(expression="garbage(", is_valid=False),
            AlphaCandidate(expression="rank(volume)", is_valid=True),
            AlphaCandidate(expression="not_checked"),  # is_valid None → excluded
        ],
        trace_steps=[{"step": "CODE_GEN"}, {"step": "VALIDATE"}],
    )
    wf._gen_graph = _FakeGraph(final_state)

    out = await wf.run(
        task=_task(), dataset_id="ds1", fields=[], operators=[],
        num_alphas=10, generate_only=True,
    )

    assert set(out.keys()) == {"pending_alphas", "trace_steps", "state"}
    exprs = [a.expression for a in out["pending_alphas"]]
    assert exprs == ["close/open", "rank(volume)"]  # only is_valid True
    assert out["trace_steps"] == [{"step": "CODE_GEN"}, {"step": "VALIDATE"}]
    assert out["state"] is final_state
    # The producer asked for 10 candidates → num_alphas_target threaded into state.
    assert wf._gen_graph.app.invoked_with.num_alphas_target == 10


@pytest.mark.asyncio
async def test_generate_only_empty_pending_is_safe():
    wf = _wf()
    wf._gen_graph = _FakeGraph(SimpleNamespace(pending_alphas=[], trace_steps=[]))
    out = await wf.run(
        task=_task(), dataset_id="ds1", fields=[], operators=[],
        num_alphas=4, generate_only=True,
    )
    assert out["pending_alphas"] == []
    assert out["trace_steps"] == []
