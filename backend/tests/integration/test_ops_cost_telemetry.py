"""Integration: GET /ops/cost/telemetry (G2 Phase A, 2026-05-19).

Verifies the new G2 cost telemetry endpoint which aggregates llm_call_log
via 6 separate SQL queries (headline / by_model / by_node_key / by_pillar /
top_tasks / hourly_last_24h). Uses an AsyncMock SQL row return rather than
a real Postgres fixture — same pattern as test_r1b_ops_telemetry.py.
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
def _isolate_cost_flag():
    from backend.config import settings as _stg
    prev = getattr(_stg, "ENABLE_COST_TELEMETRY", False)
    yield
    setattr(_stg, "ENABLE_COST_TELEMETRY", prev)


def _mock_db_for_cost(
    *,
    head: Tuple,
    by_model: List[Tuple],
    by_node: List[Tuple],
    by_pillar: List[Tuple],
    top_tasks: List[Tuple],
    hourly: List[Tuple],
):
    """Build a mock AsyncSession returning the 6 queries in order:
    1. headline SELECT (one row)
    2. by_model SELECT
    3. by_node_key SELECT
    4. by_pillar SELECT
    5. top_tasks SELECT
    6. hourly_last_24h SELECT
    """
    head_result = MagicMock()
    head_result.one = MagicMock(return_value=head)

    def _rows_result(rows):
        r = MagicMock()
        r.all = MagicMock(return_value=list(rows))
        return r

    results = [
        head_result,
        _rows_result(by_model),
        _rows_result(by_node),
        _rows_result(by_pillar),
        _rows_result(top_tasks),
        _rows_result(hourly),
    ]

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=results)
    return db


@pytest_asyncio.fixture
async def client_factory():
    async def _build(*, head, by_model, by_node, by_pillar, top_tasks, hourly):
        app = FastAPI()
        app.include_router(ops_router, prefix="/api/v1")
        app.dependency_overrides[get_db] = lambda: _mock_db_for_cost(
            head=head, by_model=by_model, by_node=by_node,
            by_pillar=by_pillar, top_tasks=top_tasks, hourly=hourly,
        )
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test")
    return _build


# ---------------------------------------------------------------------------
# Endpoint contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cost_telemetry_aggregates_headline_and_groups(client_factory):
    """Typical 7-day window with 100 calls split across two models."""
    head = (100, 95, 5, 50_000, 0.05)  # calls, ok, bad, toks, cost
    by_model = [
        ("deepseek-chat", 80, 40_000, 0.0108, 420.0, 78),
        ("claude-haiku-4-5", 20, 10_000, 0.0125, 800.0, 17),
    ]
    by_node = [
        ("hypothesis", 50, 25_000, 0.020, 500.0, 48),
        ("code_gen", 30, 15_000, 0.018, 600.0, 29),
        ("self_correct", 20, 10_000, 0.012, 300.0, 18),
    ]
    by_pillar = [
        ("momentum", 60, 30_000, 0.030, 450.0, 58),
        ("value", 40, 20_000, 0.020, 500.0, 37),
    ]
    top_tasks = [
        (101, 60, 30_000, 0.030),
        (202, 40, 20_000, 0.020),
    ]
    hourly = [
        ("2026-05-19T10:00:00Z", 10, 5_000, 0.005),
        ("2026-05-19T11:00:00Z", 15, 7_500, 0.0075),
    ]
    from backend.config import settings as _stg
    setattr(_stg, "ENABLE_COST_TELEMETRY", True)

    client = await client_factory(
        head=head, by_model=by_model, by_node=by_node,
        by_pillar=by_pillar, top_tasks=top_tasks, hourly=hourly,
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/cost/telemetry?days=7&top_n=2")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["window_days"] == 7
    assert body["total_calls"] == 100
    assert body["successful_calls"] == 95
    assert body["failed_calls"] == 5
    assert body["error_rate"] == 0.05
    assert body["total_tokens"] == 50_000
    assert body["total_cost_usd"] == 0.05
    assert body["avg_cost_per_call"] == round(0.05 / 100, 6)
    assert body["avg_tokens_per_call"] == 500.0
    assert body["flags"]["ENABLE_COST_TELEMETRY"] is True
    # Healthy: flag ON + calls > 0 + error_rate <= 0.10
    assert body["is_healthy"] is True

    # Group rows preserved
    assert len(body["by_model"]) == 2
    assert body["by_model"][0]["label"] == "deepseek-chat"
    assert body["by_model"][0]["calls"] == 80
    assert body["by_model"][0]["success_rate"] == round(78 / 80, 4)

    assert len(body["by_node_key"]) == 3
    assert body["by_node_key"][0]["label"] == "hypothesis"

    assert len(body["by_pillar"]) == 2
    assert len(body["top_tasks_by_cost"]) == 2
    assert body["top_tasks_by_cost"][0]["task_id"] == 101
    assert body["top_tasks_by_cost"][0]["cost_usd"] == 0.030
    assert len(body["hourly_last_24h"]) == 2


@pytest.mark.asyncio
async def test_cost_telemetry_empty_log_returns_zero_and_unhealthy(client_factory):
    """Flag ON but no rows yet → total=0 + unhealthy (haven't started capturing)."""
    from backend.config import settings as _stg
    setattr(_stg, "ENABLE_COST_TELEMETRY", True)

    client = await client_factory(
        head=(0, 0, 0, 0, 0.0),
        by_model=[], by_node=[], by_pillar=[], top_tasks=[], hourly=[],
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/cost/telemetry")

    assert r.status_code == 200
    body = r.json()
    assert body["total_calls"] == 0
    assert body["error_rate"] == 0.0
    assert body["avg_cost_per_call"] == 0.0
    assert body["by_model"] == []
    # is_healthy False even with flag ON when no calls captured
    assert body["is_healthy"] is False


@pytest.mark.asyncio
async def test_cost_telemetry_flag_off_reports_unhealthy(client_factory):
    """Flag OFF should mark unhealthy even if rows somehow exist (legacy)."""
    from backend.config import settings as _stg
    setattr(_stg, "ENABLE_COST_TELEMETRY", False)

    client = await client_factory(
        head=(10, 10, 0, 5_000, 0.01),
        by_model=[("deepseek-chat", 10, 5_000, 0.01, 400.0, 10)],
        by_node=[("hypothesis", 10, 5_000, 0.01, 400.0, 10)],
        by_pillar=[],
        top_tasks=[],
        hourly=[],
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/cost/telemetry")
    body = r.json()
    assert body["flags"]["ENABLE_COST_TELEMETRY"] is False
    assert body["is_healthy"] is False


@pytest.mark.asyncio
async def test_cost_telemetry_high_error_rate_unhealthy(client_factory):
    """error_rate > 0.10 marks unhealthy (LLM provider degraded)."""
    from backend.config import settings as _stg
    setattr(_stg, "ENABLE_COST_TELEMETRY", True)

    client = await client_factory(
        head=(100, 80, 20, 50_000, 0.05),  # error_rate = 0.20
        by_model=[("deepseek-chat", 100, 50_000, 0.05, 420.0, 80)],
        by_node=[("hypothesis", 100, 50_000, 0.05, 420.0, 80)],
        by_pillar=[],
        top_tasks=[(101, 100, 50_000, 0.05)],
        hourly=[],
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/cost/telemetry")
    body = r.json()
    assert body["error_rate"] == 0.20
    assert body["is_healthy"] is False


@pytest.mark.asyncio
async def test_cost_telemetry_requires_ops_token_when_set(client_factory):
    """OPS_API_TOKEN set → 401 without header."""
    from backend.config import settings as _stg
    setattr(_stg, "ENABLE_COST_TELEMETRY", True)
    os.environ["OPS_API_TOKEN"] = "secret123"
    try:
        client = await client_factory(
            head=(0, 0, 0, 0, 0.0),
            by_model=[], by_node=[], by_pillar=[], top_tasks=[], hourly=[],
        )
        async with client as ac:
            r_no_token = await ac.get("/api/v1/ops/cost/telemetry")
            assert r_no_token.status_code == 401
    finally:
        os.environ.pop("OPS_API_TOKEN", None)


@pytest.mark.asyncio
async def test_cost_telemetry_window_days_param_echoed(client_factory):
    from backend.config import settings as _stg
    setattr(_stg, "ENABLE_COST_TELEMETRY", True)

    client = await client_factory(
        head=(0, 0, 0, 0, 0.0),
        by_model=[], by_node=[], by_pillar=[], top_tasks=[], hourly=[],
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/cost/telemetry?days=30")
    assert r.status_code == 200
    assert r.json()["window_days"] == 30
