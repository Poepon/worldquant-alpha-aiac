"""Integration: GET /ops/r8/query-stats (2026-05-18).

Per-query R8 telemetry endpoint — complements /ops/r8/kb-shape (corpus
snapshot) with runtime layer fall-through stats.

Mocks AsyncSession.execute for 2 calls in order:
  1. aggregate row (total + cache + elev + l0 + l1 + l2 + l3)
  2. region GROUP BY
"""
from __future__ import annotations

import os
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


def _mock_db(agg_row, region_rows):
    agg_r = MagicMock(); agg_r.one = MagicMock(return_value=agg_row)
    reg_r = MagicMock(); reg_r.all = MagicMock(return_value=list(region_rows))
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[agg_r, reg_r])
    return db


@pytest_asyncio.fixture
async def client_factory():
    async def _build(agg_row, region_rows):
        app = FastAPI()
        app.include_router(ops_router, prefix="/api/v1")
        app.dependency_overrides[get_db] = lambda: _mock_db(agg_row, region_rows)
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test")
    return _build


@pytest.mark.asyncio
async def test_query_stats_aggregates_layer_hit_rates(client_factory):
    """100 queries, mixed layer touches → per-layer rates computed correctly."""
    # (total, cache, elev, l0, l1, l2, l3)
    agg_row = (100, 25, 5, 80, 60, 30, 10)
    region_rows = [("USA", 70), ("CHN", 20), ("none", 10)]
    client = await client_factory(agg_row, region_rows)
    async with client as ac:
        r = await ac.get("/api/v1/ops/r8/query-stats?days=7")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_queries"] == 100
    assert body["cache_hit_rate"] == 0.25
    assert body["failure_tree_elevation_rate"] == 0.05
    assert body["layer_hit_rates"]["L0_exact"] == 0.8
    assert body["layer_hit_rates"]["L1_pillar"] == 0.6
    assert body["layer_hit_rates"]["L2_family"] == 0.3
    assert body["layer_hit_rates"]["L3_field"] == 0.1
    assert body["by_region"]["USA"] == 70
    assert body["window_days"] == 7


@pytest.mark.asyncio
async def test_query_stats_empty_log_returns_zero_rates(client_factory):
    """Flag never flipped or no calls in window → all zeros."""
    client = await client_factory((0, 0, 0, 0, 0, 0, 0), [])
    async with client as ac:
        r = await ac.get("/api/v1/ops/r8/query-stats")
    body = r.json()
    assert body["total_queries"] == 0
    assert body["cache_hit_rate"] == 0.0
    assert body["failure_tree_elevation_rate"] == 0.0
    assert all(v == 0.0 for v in body["layer_hit_rates"].values())
    assert body["by_region"] == {}


@pytest.mark.asyncio
async def test_query_stats_exposes_flags(client_factory):
    client = await client_factory((0, 0, 0, 0, 0, 0, 0), [])
    async with client as ac:
        r = await ac.get("/api/v1/ops/r8/query-stats")
    flags = r.json()["flags"]
    assert set(flags.keys()) == {
        "ENABLE_HIERARCHICAL_RAG",
        "ENABLE_R8_QUERY_LOG",
    }


@pytest.mark.asyncio
async def test_query_stats_layer_rates_sum_independently(client_factory):
    """Each layer is independent — a query can touch multiple layers, so
    rates are NOT mutually exclusive (cumulative > 1.0 is valid)."""
    # 10 queries, all 4 layers hit on every query
    client = await client_factory((10, 0, 0, 10, 10, 10, 10), [])
    async with client as ac:
        r = await ac.get("/api/v1/ops/r8/query-stats")
    body = r.json()
    rates = body["layer_hit_rates"]
    # All 1.0 each — not sum-to-1
    assert rates["L0_exact"] == 1.0
    assert rates["L1_pillar"] == 1.0
    assert sum(rates.values()) == 4.0


@pytest.mark.asyncio
async def test_query_stats_requires_ops_token_when_env_set(client_factory):
    os.environ["OPS_API_TOKEN"] = "abc123"
    client = await client_factory((0, 0, 0, 0, 0, 0, 0), [])
    async with client as ac:
        r = await ac.get("/api/v1/ops/r8/query-stats")
    assert r.status_code == 401
