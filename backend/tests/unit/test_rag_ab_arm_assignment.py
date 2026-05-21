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
async def test_flag_on_deterministic_balanced(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_RAG_CATEGORY_AB", True, raising=False)

    # (task_id + round) % 2 == 0 → category ; else control
    cases = [(2, 0, "category"), (3, 0, "control"), (2, 1, "control"), (3, 1, "category"), (10, 4, "category")]
    for tid, rnd, expected in cases:
        rag = _mock_rag()
        out = await node_rag_query(_state(tid, rnd), rag, config=None)
        assert out["rag_ab_arm"] == expected, f"task={tid} round={rnd}"
        assert rag.query.call_args.kwargs["rag_ab_arm"] == expected


@pytest.mark.asyncio
async def test_within_task_alternates_across_rounds(monkeypatch):
    """Same task, consecutive rounds flip arm → within-task randomization."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_RAG_CATEGORY_AB", True, raising=False)
    rag = _mock_rag()
    arms = []
    for rnd in range(6):
        out = await node_rag_query(_state(7, rnd), rag, config=None)
        arms.append(out["rag_ab_arm"])
    # alternating pattern, both arms present
    assert set(arms) == {"control", "category"}
    assert all(arms[i] != arms[i + 1] for i in range(len(arms) - 1))
