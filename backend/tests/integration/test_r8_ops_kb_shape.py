"""Integration: GET /ops/r8/kb-shape (2026-05-18).

R8 hierarchical RAG KB-shape telemetry — completes the R1a/R1b/R8
telemetry trio. Unlike R1a/R1b which write to dedicated log tables,
R8 has no per-query persistence; the actionable signal for operators
is the KB's *shape* (entry_type distribution + decayed split + pillar
diversity + R5-rankable subset).

Mocks AsyncSession.execute returning canonical rows (3 calls: entry_type
GROUP BY → pillar GROUP BY → R5 join scalar).
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


def _mock_db(et_rows, pillar_rows, r5_count):
    """Build AsyncSession mock for 3 execute calls in order."""
    et_result = MagicMock()
    et_result.all = MagicMock(return_value=list(et_rows))
    pillar_result = MagicMock()
    pillar_result.all = MagicMock(return_value=list(pillar_rows))
    r5_result = MagicMock()
    r5_result.scalar = MagicMock(return_value=r5_count)
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[et_result, pillar_result, r5_result])
    return db


@pytest_asyncio.fixture
async def client_factory():
    async def _build(et_rows, pillar_rows, r5_count, settings_overrides=None):
        app = FastAPI()
        app.include_router(ops_router, prefix="/api/v1")
        app.dependency_overrides[get_db] = lambda: _mock_db(et_rows, pillar_rows, r5_count)
        if settings_overrides:
            from backend.config import settings as _stg
            for k, v in settings_overrides.items():
                setattr(_stg, k, v)
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test")
    return _build


# ---------------------------------------------------------------------------
# Entry-type + pillar aggregation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_kb_shape_aggregates_entry_types_with_decayed_split(client_factory):
    """3 entry types, mixed active/decayed counts → totals + per-type split."""
    et_rows = [
        # (entry_type, active, decayed)
        ("SUCCESS_PATTERN",  100, 20),
        ("FAILURE_PITFALL",  50,  5),
        ("IMPORT",           30,  0),
    ]
    # Mock pre-sorted (DESC by count) the way the SQL ORDER BY would
    pillar_rows = [
        ("none",     70),  # NULL pillar bucket — backfill candidates
        ("momentum", 60),
        ("value",    50),
    ]
    client = await client_factory(et_rows, pillar_rows, 35)
    async with client as ac:
        r = await ac.get("/api/v1/ops/r8/kb-shape")
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["total_active"] == 180
    assert body["total_decayed"] == 25
    assert body["success_pattern_active"] == 100
    assert body["failure_pitfall_active"] == 50
    assert body["r5_rankable_success_count"] == 35
    # Entry types preserve mock order (SQL ORDER BY active DESC)
    assert body["entry_types"][0]["entry_type"] == "SUCCESS_PATTERN"
    assert body["entry_types"][0]["decayed_count"] == 20
    # Pillars preserve mock order (none=70 first)
    assert len(body["pillars"]) == 3
    assert body["pillars"][0]["pillar"] == "none"


@pytest.mark.asyncio
async def test_kb_shape_empty_kb_returns_zeros(client_factory):
    """Empty KB → zero totals + empty arrays + zero R5 count."""
    client = await client_factory([], [], 0)
    async with client as ac:
        r = await ac.get("/api/v1/ops/r8/kb-shape")
    assert r.status_code == 200
    body = r.json()
    assert body["total_active"] == 0
    assert body["total_decayed"] == 0
    assert body["success_pattern_active"] == 0
    assert body["failure_pitfall_active"] == 0
    assert body["r5_rankable_success_count"] == 0
    assert body["entry_types"] == []
    assert body["pillars"] == []


@pytest.mark.asyncio
async def test_kb_shape_unknown_entry_type_falls_through(client_factory):
    """Entry types other than SUCCESS_PATTERN/FAILURE_PITFALL count toward
    total_active but NOT the dedicated success/failure scalars."""
    et_rows = [
        ("WEIRD_TYPE", 42, 0),
    ]
    client = await client_factory(et_rows, [], 0)
    async with client as ac:
        r = await ac.get("/api/v1/ops/r8/kb-shape")
    body = r.json()
    assert body["total_active"] == 42
    assert body["success_pattern_active"] == 0
    assert body["failure_pitfall_active"] == 0


@pytest.mark.asyncio
async def test_kb_shape_r5_count_null_scalar_returns_zero(client_factory):
    """sqlalchemy scalar() returning None → int(0) coercion."""
    client = await client_factory([], [], None)
    async with client as ac:
        r = await ac.get("/api/v1/ops/r8/kb-shape")
    body = r.json()
    assert body["r5_rankable_success_count"] == 0


# ---------------------------------------------------------------------------
# Flag exposure + auth
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_kb_shape_exposes_r8_flags(client_factory):
    client = await client_factory([], [], 0)
    async with client as ac:
        r = await ac.get("/api/v1/ops/r8/kb-shape")
    flags = r.json()["flags"]
    assert set(flags.keys()) == {"ENABLE_HIERARCHICAL_RAG", "ENABLE_R5_L2_RANKING"}
    for v in flags.values():
        assert isinstance(v, bool)


@pytest.mark.asyncio
async def test_kb_shape_requires_ops_token_when_env_set(client_factory):
    os.environ["OPS_API_TOKEN"] = "abc123"
    client = await client_factory([], [], 0)
    async with client as ac:
        r = await ac.get("/api/v1/ops/r8/kb-shape")
    assert r.status_code == 401
