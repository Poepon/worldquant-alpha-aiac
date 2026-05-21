"""G5 Phase A follow-up — /ops/g5/crossover-stats endpoint integration tests
(2026-05-19).

Mocks AsyncSession.execute returning 4 SQL query results in order:
  1. headline aggregate (one row: total_calls, total_offspring, total_outcome, total_pass)
  2. per_strategy rows
  3. per_pillar_pair rows
  4. recent_events rows
"""
from __future__ import annotations

import os
from typing import List, Tuple
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.database import get_db
from backend.routers.ops import router as ops_router


@pytest.fixture(autouse=True)
def _isolate_ops_token():
    prev = os.environ.pop("OPS_API_TOKEN", None)
    yield
    if prev is not None:
        os.environ["OPS_API_TOKEN"] = prev
    else:
        os.environ.pop("OPS_API_TOKEN", None)


@pytest.fixture(autouse=True)
def _isolate_g5_flag():
    from backend.config import settings as _stg
    prev = getattr(_stg, "ENABLE_G5_CROSSOVER", False)
    yield
    setattr(_stg, "ENABLE_G5_CROSSOVER", prev)


def _mock_db_for_g5(
    *,
    head: Tuple,
    strategy_rows: List[Tuple],
    pillar_rows: List[Tuple],
    recent_rows: List[Tuple],
):
    def _one(row):
        r = MagicMock()
        r.one = MagicMock(return_value=row)
        return r

    def _all(rows):
        r = MagicMock()
        r.all = MagicMock(return_value=list(rows))
        return r

    results = [
        _one(head),
        _all(strategy_rows),
        _all(pillar_rows),
        _all(recent_rows),
    ]
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=results)
    return db


@pytest_asyncio.fixture
async def client_factory():
    async def _build(*, head, strategy_rows, pillar_rows, recent_rows):
        app = FastAPI()
        app.include_router(ops_router, prefix="/api/v1")
        app.dependency_overrides[get_db] = lambda: _mock_db_for_g5(
            head=head, strategy_rows=strategy_rows,
            pillar_rows=pillar_rows, recent_rows=recent_rows,
        )
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test")
    return _build


@pytest.mark.asyncio
async def test_g5_stats_aggregates_headline_and_groups(client_factory):
    """Typical 7-day window with 10 crossover calls, 18 offspring, 5 PASS."""
    from backend.config import settings as _stg
    setattr(_stg, "ENABLE_G5_CROSSOVER", True)

    # head = (total_calls, total_offspring, total_outcome, total_pass)
    head = (10, 18, 15, 5)
    strategy_rows = [
        ("cross_sectional_confirm", 6, 1.8, 3),
        ("weighted_sum", 3, 2.0, 2),
        ("wrapper_graft", 1, 1.0, 0),
    ]
    pillar_rows = [
        ("momentum→value", 5, 3),
        ("value→quality", 3, 2),
        ("?→?", 2, 0),
    ]
    recent_rows = [
        (101, 42, 5, 1001, 1002, 2, 1, 0.012, "2026-05-19T13:00:00Z"),
        (100, 42, 4, 1000, 1003, 2, 0, 0.011, "2026-05-19T12:00:00Z"),
    ]
    client = await client_factory(
        head=head, strategy_rows=strategy_rows,
        pillar_rows=pillar_rows, recent_rows=recent_rows,
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/g5/crossover-stats?days=7&top_n=10")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["window_days"] == 7
    assert body["total_crossover_calls"] == 10
    assert body["total_offspring"] == 18
    assert body["total_offspring_referenced_alphas"] == 15
    assert body["offspring_pass_count"] == 5
    assert body["offspring_pass_rate"] == round(5 / 15, 4)
    assert body["avg_offspring_per_call"] == round(18 / 10, 2)
    assert body["is_healthy"] is True

    assert len(body["per_strategy"]) == 3
    assert body["per_strategy"][0]["strategy"] == "cross_sectional_confirm"
    assert body["per_strategy"][0]["calls"] == 6
    assert body["per_strategy"][0]["avg_offspring_count"] == 1.8

    assert len(body["per_pillar_pair"]) == 3
    assert body["per_pillar_pair"][0]["pillar_pair"] == "momentum→value"

    assert len(body["recent_events"]) == 2
    assert body["recent_events"][0]["id"] == 101
    assert body["recent_events"][0]["parent_a_alpha_id"] == 1001
    assert body["recent_events"][0]["llm_cost_usd"] == 0.012


@pytest.mark.asyncio
async def test_g5_stats_empty_log_unhealthy(client_factory):
    from backend.config import settings as _stg
    setattr(_stg, "ENABLE_G5_CROSSOVER", True)

    client = await client_factory(
        head=(0, 0, 0, 0),
        strategy_rows=[], pillar_rows=[], recent_rows=[],
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/g5/crossover-stats")
    body = r.json()
    assert body["total_crossover_calls"] == 0
    assert body["offspring_pass_rate"] == 0.0
    assert body["per_strategy"] == []
    assert body["recent_events"] == []
    assert body["is_healthy"] is False


@pytest.mark.asyncio
async def test_g5_stats_flag_off_unhealthy(client_factory):
    """Flag OFF marks unhealthy even with data."""
    from backend.config import settings as _stg
    setattr(_stg, "ENABLE_G5_CROSSOVER", False)

    client = await client_factory(
        head=(5, 8, 8, 3),
        strategy_rows=[("weighted_sum", 5, 1.6, 3)],
        pillar_rows=[("momentum→value", 5, 3)],
        recent_rows=[(1, 42, 3, 100, 200, 2, 1, 0.01, "2026-05-19T10:00:00Z")],
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/g5/crossover-stats")
    body = r.json()
    assert body["flags"]["ENABLE_G5_CROSSOVER"] is False
    assert body["is_healthy"] is False


@pytest.mark.asyncio
async def test_g5_stats_zero_pass_rate_unhealthy(client_factory):
    """Calls present but 0 PASS → unhealthy(LLM hallucinating)."""
    from backend.config import settings as _stg
    setattr(_stg, "ENABLE_G5_CROSSOVER", True)

    client = await client_factory(
        head=(8, 12, 12, 0),
        strategy_rows=[("weighted_sum", 8, 1.5, 0)],
        pillar_rows=[("momentum→value", 8, 0)],
        recent_rows=[(1, 42, 3, 100, 200, 2, 0, 0.01, "2026-05-19T10:00:00Z")],
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/g5/crossover-stats")
    body = r.json()
    assert body["total_crossover_calls"] == 8
    assert body["offspring_pass_count"] == 0
    assert body["offspring_pass_rate"] == 0.0
    assert body["is_healthy"] is False


@pytest.mark.asyncio
async def test_g5_stats_requires_ops_token_when_set(client_factory):
    from backend.config import settings as _stg
    setattr(_stg, "ENABLE_G5_CROSSOVER", True)
    os.environ["OPS_API_TOKEN"] = "secret789"
    try:
        client = await client_factory(
            head=(0, 0, 0, 0), strategy_rows=[],
            pillar_rows=[], recent_rows=[],
        )
        async with client as ac:
            r = await ac.get("/api/v1/ops/g5/crossover-stats")
            assert r.status_code == 401
    finally:
        os.environ.pop("OPS_API_TOKEN", None)
