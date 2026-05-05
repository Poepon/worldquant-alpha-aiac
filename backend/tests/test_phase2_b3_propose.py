"""Phase 2 B3 — node_hypothesis_propose persistence tests.

Verifies that when hypothesis_centric_level >= 2 the LLM-emitted hypothesis
dicts are persisted as Hypothesis ORM rows BEFORE downstream code_gen
runs, satisfying the time-ordering hard constraint (Plan §A post-hoc
defense): hypothesis.created_at < alpha.created_at.

Tests use a mock LLMService so we can control the hypothesis output, then
inspect the resulting state + DB rows. Postgres is required because the
persistence path uses JSONB.
"""
from __future__ import annotations

import os
import socket
import uuid
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from backend.agents.graph.state import MiningState
from backend.models import Hypothesis, HypothesisStatus


def _pg_reachable() -> bool:
    try:
        s = socket.create_connection(("localhost", 5433), timeout=1)
        s.close()
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_reachable(),
    reason="Postgres not reachable on localhost:5433",
)


_TAG = f"_b3_test_{uuid.uuid4().hex[:8]}_"


@pytest_asyncio.fixture(autouse=True)
async def _cleanup():
    yield
    engine = create_async_engine(
        "postgresql+asyncpg://postgres:postgres@localhost:5433/alpha_gpt",
        echo=False,
    )
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        try:
            await s.execute(
                delete(Hypothesis).where(Hypothesis.statement.like(f"{_TAG}%"))
            )
            await s.commit()
        except Exception:
            await s.rollback()
    await engine.dispose()


def _mock_llm_response(hypotheses: List[Dict[str, Any]]):
    """Build an LLMService mock that returns the given hypothesis list."""
    response = MagicMock()
    response.success = True
    response.parsed = {
        "hypotheses": hypotheses,
        "knowledge_transfer": {},
        "analysis": {},
    }
    response.error = None

    llm = MagicMock()
    llm.call = AsyncMock(return_value=response)
    return llm


def _state(**overrides) -> MiningState:
    base = dict(
        task_id=42,
        region="USA",
        universe="TOP3000",
        dataset_id="pv1",
        fields=[{"id": "close", "name": "close"}, {"id": "volume", "name": "volume"}],
        # MiningState.operators is List[Dict] (not List[str])
        operators=[
            {"name": "ts_rank", "category": "time_series"},
            {"name": "rank", "category": "cross_sectional"},
            {"name": "multiply", "category": "arithmetic"},
        ],
        factor_tier=1,
        available_dataset_pool=["pv1"],
    )
    base.update(overrides)
    return MiningState(**base)


@pytest.mark.asyncio
async def test_level_0_does_not_persist_typed_hypothesis():
    """HYPOTHESIS_CENTRIC_LEVEL = 0 (legacy) — no DB rows created."""
    from backend.agents.graph.nodes.generation import node_hypothesis

    llm = _mock_llm_response([
        {"idea": f"{_TAG}level0",
         "rationale": "test",
         "selected_datasets": ["pv1"]}
    ])
    state = _state()
    config = {"configurable": {"hypothesis_centric_level": 0}}

    result = await node_hypothesis(state, llm, config)
    assert result["current_hypothesis_id"] is None
    assert result["current_hypothesis_ids"] == []

    # No row was inserted
    engine = create_async_engine(
        "postgresql+asyncpg://postgres:postgres@localhost:5433/alpha_gpt",
        echo=False,
    )
    async with engine.connect() as c:
        r = await c.execute(
            text("SELECT COUNT(*) FROM hypotheses WHERE statement LIKE :s"),
            {"s": f"{_TAG}%"},
        )
        assert r.scalar() == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_level_1_does_not_persist_typed_hypothesis():
    """HYPOTHESIS_CENTRIC_LEVEL = 1 (Phase 1 only) — cross-dataset works
    but no DB row yet. Persistence kicks in only at level>=2."""
    from backend.agents.graph.nodes.generation import node_hypothesis

    llm = _mock_llm_response([
        {"idea": f"{_TAG}level1",
         "rationale": "phase 1",
         "selected_datasets": ["pv1"]}
    ])
    state = _state()
    config = {"configurable": {"hypothesis_centric_level": 1}}

    result = await node_hypothesis(state, llm, config)
    assert result["current_hypothesis_id"] is None
    assert result["current_hypothesis_ids"] == []


@pytest.mark.asyncio
async def test_level_2_persists_one_hypothesis():
    """HYPOTHESIS_CENTRIC_LEVEL = 2 — one LLM hypothesis becomes one DB row.
    Primary hypothesis_id flows to state."""
    from backend.agents.graph.nodes.generation import node_hypothesis

    llm = _mock_llm_response([
        {"idea": f"{_TAG}single",
         "rationale": "phase 2 primary",
         "expected_signal": "momentum",
         "confidence": "high",
         "novelty": "emerging",
         "key_fields": ["close", "volume"],
         "suggested_operators": ["ts_rank"],
         "selected_datasets": ["pv1"]}
    ])
    state = _state()
    config = {
        "configurable": {
            "hypothesis_centric_level": 2,
            "experiment_variant": "phase2-test",
        }
    }

    result = await node_hypothesis(state, llm, config)

    assert result["current_hypothesis_id"] is not None
    assert len(result["current_hypothesis_ids"]) == 1
    assert result["current_hypothesis_id"] == result["current_hypothesis_ids"][0]

    # Verify DB row
    engine = create_async_engine(
        "postgresql+asyncpg://postgres:postgres@localhost:5433/alpha_gpt",
        echo=False,
    )
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        row = await s.get(Hypothesis, result["current_hypothesis_id"])
        assert row is not None
        assert row.statement == f"{_TAG}single"
        assert row.rationale == "phase 2 primary"
        assert row.expected_signal == "momentum"
        assert row.confidence == "high"
        assert row.novelty == "emerging"
        assert row.target_tier == 1
        assert row.region == "USA"
        assert row.universe == "TOP3000"
        assert row.dataset_pool == ["pv1"]
        assert row.experiment_variant == "phase2-test"
        assert row.status == HypothesisStatus.PROPOSED.value
        assert row.is_active is True
        assert row.alpha_count == 0
        assert row.pass_count == 0
        assert row.key_fields == ["close", "volume"]
        assert row.suggested_operators == ["ts_rank"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_level_2_persists_multiple_hypotheses():
    """LLM may emit 2-3 hypotheses per round; all should persist, primary
    is the first."""
    from backend.agents.graph.nodes.generation import node_hypothesis

    llm = _mock_llm_response([
        {"idea": f"{_TAG}multi-A", "rationale": "first", "selected_datasets": ["pv1"]},
        {"idea": f"{_TAG}multi-B", "rationale": "second", "selected_datasets": ["pv1"]},
        {"idea": f"{_TAG}multi-C", "rationale": "third", "selected_datasets": ["pv1"]},
    ])
    state = _state()
    config = {"configurable": {"hypothesis_centric_level": 2}}

    result = await node_hypothesis(state, llm, config)
    ids = result["current_hypothesis_ids"]
    assert len(ids) == 3
    assert result["current_hypothesis_id"] == ids[0]

    # All 3 rows present
    engine = create_async_engine(
        "postgresql+asyncpg://postgres:postgres@localhost:5433/alpha_gpt",
        echo=False,
    )
    async with engine.connect() as c:
        r = await c.execute(
            text("SELECT COUNT(*) FROM hypotheses WHERE statement LIKE :s"),
            {"s": f"{_TAG}multi-%"},
        )
        assert r.scalar() == 3
    await engine.dispose()


@pytest.mark.asyncio
async def test_level_2_writes_hypothesis_id_back_to_dict():
    """Each hypothesis dict in `result["hypotheses"]` should be enriched with
    its persisted hypothesis_id for downstream node logging."""
    from backend.agents.graph.nodes.generation import node_hypothesis

    llm = _mock_llm_response([
        {"idea": f"{_TAG}writeback",
         "rationale": "test writeback",
         "selected_datasets": ["pv1"]}
    ])
    state = _state()
    config = {"configurable": {"hypothesis_centric_level": 2}}

    result = await node_hypothesis(state, llm, config)
    hyp = result["hypotheses"][0]
    assert "hypothesis_id" in hyp
    assert hyp["hypothesis_id"] == result["current_hypothesis_id"]


@pytest.mark.asyncio
async def test_level_2_skips_hypotheses_without_statement():
    """Empty / missing 'idea' field should not produce a row."""
    from backend.agents.graph.nodes.generation import node_hypothesis

    llm = _mock_llm_response([
        {"idea": "", "rationale": "should skip"},
        {"idea": f"{_TAG}valid", "rationale": "should persist"},
        {"rationale": "no idea key at all"},
    ])
    state = _state()
    config = {"configurable": {"hypothesis_centric_level": 2}}

    result = await node_hypothesis(state, llm, config)
    assert len(result["current_hypothesis_ids"]) == 1


@pytest.mark.asyncio
async def test_level_2_persistence_failure_is_non_fatal():
    """If create_hypothesis raises (e.g. DB down mid-round), the node still
    returns successfully — workflow continues with empty hypothesis_id (which
    falls back to legacy alpha.hypothesis Text storage)."""
    import backend.agents.graph.nodes.generation as gen

    # Patch HypothesisService to raise
    from unittest.mock import patch
    original = gen.AsyncSessionLocal if hasattr(gen, "AsyncSessionLocal") else None

    llm = _mock_llm_response([
        {"idea": f"{_TAG}fail-path", "rationale": "test",
         "selected_datasets": ["pv1"]}
    ])
    state = _state()
    config = {"configurable": {"hypothesis_centric_level": 2}}

    # Inject a session factory that raises on commit
    class _BoomService:
        def __init__(self, *a, **k): ...
        async def create_hypothesis(self, *a, **k):
            raise RuntimeError("synthetic DB failure")

    with patch(
        "backend.services.hypothesis_service.HypothesisService", _BoomService,
    ):
        result = await gen.node_hypothesis(state, llm, config)

    # Node returned (no exception bubble), but no IDs persisted
    assert result["current_hypothesis_id"] is None
    assert result["current_hypothesis_ids"] == []
    # Legacy hypotheses dict is still populated
    assert len(result["hypotheses"]) == 1
