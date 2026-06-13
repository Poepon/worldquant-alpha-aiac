"""Integration: GET /ops/r1a/telemetry (2026-05-18).

Replaces an earlier standalone diagnostic script per plan
§1.7 + feedback_no_reflex_flag_cleanup memory. R1a hook has been
production-ON for months; this endpoint surfaces attribution distribution
+ R5 c1/c2 agreement rates without manual SQL.

Mirrors the test pattern from test_r1b_ops_telemetry.py (mocked
AsyncSession.execute returning canonical rows).
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


def _mock_db(distribution_rows, r5_row):
    """Build AsyncSession-like mock returning distribution rows first, then
    the single R5 stat row from execute(...).one()."""
    dist_result = MagicMock()
    dist_result.all = MagicMock(return_value=list(distribution_rows))
    r5_result = MagicMock()
    r5_result.one = MagicMock(return_value=r5_row)
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[dist_result, r5_result])
    return db


@pytest_asyncio.fixture
async def client_factory():
    async def _build(distribution_rows, r5_row, settings_overrides=None):
        app = FastAPI()
        app.include_router(ops_router, prefix="/api/v1")
        app.dependency_overrides[get_db] = lambda: _mock_db(distribution_rows, r5_row)
        if settings_overrides:
            from backend.config import settings as _stg
            for k, v in settings_overrides.items():
                setattr(_stg, k, v)
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test")
    return _build


# ---------------------------------------------------------------------------
# Distribution + KPI aggregation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_telemetry_aggregates_attribution_distribution(client_factory):
    """4 attribution buckets + non_null + non_unknown computed correctly."""
    distribution_rows = [
        # (attribution, count, errs_count, avg_confidence)
        ("hypothesis",     50, 0, 0.80),
        ("implementation", 40, 1, 0.75),
        ("both",           10, 0, 0.70),
        ("unknown",        20, 0, 0.50),
        ("null",            5, 5, 0.0),   # hook fully failed
    ]
    r5_row = (0, 0, 0.0, 0.0, 0)  # No R5 data (LLM judge off)
    client = await client_factory(distribution_rows, r5_row)
    async with client as ac:
        r = await ac.get("/api/v1/ops/r1a/telemetry?days=7")
    assert r.status_code == 200, r.text
    body = r.json()

    # total = 125, non_null = 120, actionable (h+i+both) = 100
    assert body["total_in_window"] == 125
    assert body["non_null_pct"] == round(120 / 125, 4)        # 0.96
    assert body["non_unknown_pct"] == round(100 / 120, 4)     # 0.8333
    assert body["errs_count_total"] == 6
    assert len(body["distribution"]) == 5
    # Bucket sort by count DESC — verify hypothesis first
    assert body["distribution"][0]["attribution"] == "hypothesis"


@pytest.mark.asyncio
async def test_telemetry_empty_log_returns_zero_kpis(client_factory):
    client = await client_factory([], (0, 0, 0.0, 0.0, 0))
    async with client as ac:
        r = await ac.get("/api/v1/ops/r1a/telemetry")
    assert r.status_code == 200
    body = r.json()
    assert body["distribution"] == []
    assert body["total_in_window"] == 0
    assert body["non_null_pct"] == 0.0
    assert body["non_unknown_pct"] == 0.0
    assert body["errs_count_total"] == 0
    assert body["r5_agrees_r1a_pct"] is None
    assert body["r5_avg_composite_score"] is None
    assert body["r5_sample_size"] == 0
    assert body["r5_total_cost_usd"] == 0.0


# ---------------------------------------------------------------------------
# R5 stats — agreement rate + avg composite + cost
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_telemetry_r5_stats_populated_when_judge_ran(client_factory):
    """R5 judge fired on subset of rows → agreement + avg + cost reflect that subset."""
    distribution_rows = [("hypothesis", 100, 0, 0.80)]
    # (agree_n, r5_total, avg_score, cost, sample)
    r5_row = (30, 50, 0.65, 1.2345, 50)
    client = await client_factory(distribution_rows, r5_row)
    async with client as ac:
        r = await ac.get("/api/v1/ops/r1a/telemetry?days=14")
    body = r.json()
    # 30/50 agree → 0.6
    assert body["r5_agrees_r1a_pct"] == 0.6
    assert body["r5_avg_composite_score"] == 0.65
    assert body["r5_total_cost_usd"] == 1.2345
    assert body["r5_sample_size"] == 50
    assert body["window_days"] == 14


@pytest.mark.asyncio
async def test_telemetry_r5_agreement_null_when_no_judge_rows(client_factory):
    """r5_total=0 + sample=0 → both percentage fields None (legitimately
    no data, not a default zero)."""
    distribution_rows = [("hypothesis", 5, 0, 0.5)]
    r5_row = (0, 0, 0.0, 0.0, 0)
    client = await client_factory(distribution_rows, r5_row)
    async with client as ac:
        r = await ac.get("/api/v1/ops/r1a/telemetry")
    body = r.json()
    assert body["r5_agrees_r1a_pct"] is None
    assert body["r5_avg_composite_score"] is None
    assert body["r5_sample_size"] == 0


# ---------------------------------------------------------------------------
# Flags + auth
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_telemetry_exposes_r1a_and_r5_flags(client_factory):
    client = await client_factory([], (0, 0, 0.0, 0.0, 0))
    async with client as ac:
        r = await ac.get("/api/v1/ops/r1a/telemetry")
    flags = r.json()["flags"]
    assert set(flags.keys()) == {"ENABLE_R1A_HOOK", "ENABLE_LLM_JUDGE"}
    for v in flags.values():
        assert isinstance(v, bool)


@pytest.mark.asyncio
async def test_telemetry_requires_ops_token_when_env_set(client_factory):
    os.environ["OPS_API_TOKEN"] = "abc123"
    client = await client_factory([], (0, 0, 0.0, 0.0, 0))
    async with client as ac:
        r = await ac.get("/api/v1/ops/r1a/telemetry")
    assert r.status_code == 401
