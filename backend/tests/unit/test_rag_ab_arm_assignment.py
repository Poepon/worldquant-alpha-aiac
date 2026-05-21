"""RAG category-overlap A/B — per-round arm assignment in node_rag_query (2026-05-21).

Verifies: flag OFF → arm "" (no A/B, category always on); flag ON → deterministic
balanced split via (task_id + current_round) % 2, threaded to rag_service.query()
and returned for state-merge.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agents.graph.state import MiningState
from backend.agents.graph.nodes.generation import node_rag_query
from backend.agents.services.rag_service import RAGResult


def _state(task_id, rnd, dataset="pv1", region="USA"):
    return MiningState(task_id=task_id, current_round=rnd, dataset_id=dataset, region=region)


def _mock_rag():
    rag = SimpleNamespace()
    rag.query = AsyncMock(return_value=RAGResult(patterns=[], pitfalls=[], dataset_info={}))
    return rag


@pytest.mark.asyncio
async def test_flag_off_arm_empty(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_RAG_CATEGORY_AB", False, raising=False)
    rag = _mock_rag()
    out = await node_rag_query(_state(2, 0), rag, config=None)
    assert rag.query.call_args.kwargs["rag_ab_arm"] == ""
    assert out["rag_ab_arm"] == ""


@pytest.mark.asyncio
async def test_flag_on_assigns_valid_arm_and_threads(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_RAG_CATEGORY_AB", True, raising=False)
    rag = _mock_rag()
    out = await node_rag_query(_state(3, 0), rag, config=None)
    assert out["rag_ab_arm"] in ("control", "category")
    # the same arm is threaded into rag_service.query()
    assert rag.query.call_args.kwargs["rag_ab_arm"] == out["rag_ab_arm"]


@pytest.mark.asyncio
async def test_random_within_task_yields_both_arms(monkeypatch):
    """True per-round randomization → a single FLAT task accrues BOTH arms
    (fixes the per-task-fixed bug where current_round=0 stuck one arm)."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_RAG_CATEGORY_AB", True, raising=False)
    rag = _mock_rag()
    arms = set()
    for _ in range(40):  # P(miss an arm) = 2 * 0.5^40 ≈ 0
        out = await node_rag_query(_state(3, 0), rag, config=None)  # same task+round
        arms.add(out["rag_ab_arm"])
    assert arms == {"control", "category"}, f"expected both arms, got {arms}"
