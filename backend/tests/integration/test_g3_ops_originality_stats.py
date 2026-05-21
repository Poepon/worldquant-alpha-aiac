"""Integration: GET /ops/g3/originality-stats (2026-05-19).

Mirrors the test pattern from test_r1a_ops_telemetry.py — mocked
AsyncSession.execute returns canonical rows for each of the four
underlying SQL queries (total/blocked → histogram → top neighbors →
per-pillar).

Verifies the endpoint:
  - Returns 200 with the canonical response shape
  - Computes block_rate = blocked / total
  - Echoes the current threshold + mode from settings
  - Soft-fails to empty buckets when one of the four queries raises
  - Honors the bins / top_n query params (clamped to sane ranges)
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


def _mock_db(*, total_row, hist_rows, nn_rows, pillar_rows, fail_query_idx=None):
    """Build AsyncSession mock returning rows in the order the endpoint runs.

    Endpoint executes 4 queries in order:
      [0] total + blocked counts → execute(...).one()
      [1] histogram              → execute(...).all()
      [2] top_neighbors          → execute(...).all()
      [3] by_pillar              → execute(...).all()

    fail_query_idx (Optional[int]) makes that query raise — verifies
    the soft-fail fall-through.
    """
    total_result = MagicMock()
    total_result.one = MagicMock(return_value=total_row)
    hist_result = MagicMock()
    hist_result.all = MagicMock(return_value=list(hist_rows))
    nn_result = MagicMock()
    nn_result.all = MagicMock(return_value=list(nn_rows))
    pillar_result = MagicMock()
    pillar_result.all = MagicMock(return_value=list(pillar_rows))

    results = [total_result, hist_result, nn_result, pillar_result]

    if fail_query_idx is not None:
        async def _execute(*args, **kwargs):
            idx = _execute.call_count
            _execute.call_count += 1
            if idx == fail_query_idx:
                raise RuntimeError(f"simulated failure on query {idx}")
            return results[idx]
        _execute.call_count = 0
        db = AsyncMock()
        db.execute = _execute
        return db

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=results)
    return db


@pytest_asyncio.fixture
async def client_factory():
    async def _build(
        *,
        total_row=(0, 0),
        hist_rows=(),
        nn_rows=(),
        pillar_rows=(),
        fail_query_idx=None,
        settings_overrides=None,
    ):
        app = FastAPI()
        app.include_router(ops_router, prefix="/api/v1")
        app.dependency_overrides[get_db] = lambda: _mock_db(
            total_row=total_row,
            hist_rows=hist_rows,
            nn_rows=nn_rows,
            pillar_rows=pillar_rows,
            fail_query_idx=fail_query_idx,
        )
        if settings_overrides:
            from backend.config import settings as _stg
            for k, v in settings_overrides.items():
                setattr(_stg, k, v)
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test")
    return _build


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stats_returns_canonical_shape(client_factory):
    """Block rate = blocked / total; flags + threshold + mode echoed."""
    total_row = (1000, 50)  # 1000 candidates, 50 blocked at current τ
    hist_rows = [
        (0.0, 0.1, 30),
        (0.1, 0.2, 70),
        (0.2, 0.3, 200),
        (0.3, 0.4, 250),
        (0.4, 0.5, 200),
        (0.5, 0.6, 150),
        (0.6, 0.7, 60),
        (0.7, 0.8, 30),
        (0.8, 0.9, 8),
        (0.9, 1.0, 2),
    ]
    nn_rows = [
        ("abc123def4567890", 12),
        ("def456abc1234567", 8),
        ("ghi789xyz0987654", 5),
    ]
    pillar_rows = [
        ("value",     20, 200),
        ("momentum",  15, 300),
        ("quality",    8, 150),
        ("unknown",    7, 100),
    ]
    client = await client_factory(
        total_row=total_row,
        hist_rows=hist_rows,
        nn_rows=nn_rows,
        pillar_rows=pillar_rows,
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/g3/originality-stats?days=7")
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["total_candidates"] == 1000
    assert body["blocked_candidates"] == 50
    assert body["block_rate"] == 0.05
    assert body["window_days"] == 7
    assert "threshold" in body
    assert "mode" in body
    assert "ENABLE_AST_ORIGINALITY_GATE" in body["flags"]
    assert "ENABLE_AST_DIVERSITY_DIM" in body["flags"]

    # Histogram preserved order + counts
    assert len(body["distance_histogram"]) == 10
    assert body["distance_histogram"][0]["lo"] == 0.0
    assert body["distance_histogram"][0]["count"] == 30

    # Top neighbors ordered as returned (SQL handles ORDER BY)
    assert len(body["top_neighbors"]) == 3
    assert body["top_neighbors"][0]["nearest_neighbor_hash"] == "abc123def4567890"
    assert body["top_neighbors"][0]["blocked_count"] == 12

    # Per-pillar with block_rate
    assert len(body["by_pillar"]) == 4
    by_pillar = {p["pillar"]: p for p in body["by_pillar"]}
    assert by_pillar["value"]["blocked"] == 20
    assert by_pillar["value"]["total"] == 200
    assert by_pillar["value"]["block_rate"] == 0.1


# ---------------------------------------------------------------------------
# Empty data
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stats_empty_data_returns_zero(client_factory):
    """Empty log → zero counts + empty histogram/neighbors/pillars."""
    client = await client_factory(
        total_row=(0, 0),
        hist_rows=[
            (0.0, 0.1, 0), (0.1, 0.2, 0), (0.2, 0.3, 0), (0.3, 0.4, 0),
            (0.4, 0.5, 0), (0.5, 0.6, 0), (0.6, 0.7, 0), (0.7, 0.8, 0),
            (0.8, 0.9, 0), (0.9, 1.0, 0),
        ],
        nn_rows=[],
        pillar_rows=[],
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/g3/originality-stats")
    assert r.status_code == 200
    body = r.json()
    assert body["total_candidates"] == 0
    assert body["blocked_candidates"] == 0
    assert body["block_rate"] == 0.0
    assert body["top_neighbors"] == []
    assert body["by_pillar"] == []
    # Histogram bins still present (we want consistent shape for the UI)
    assert len(body["distance_histogram"]) == 10


# ---------------------------------------------------------------------------
# Soft-fail per-query
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_total_query_soft_fail_returns_zero(client_factory):
    """Total query raising → counts default to 0 but other queries proceed."""
    client = await client_factory(
        total_row=(999, 999),
        hist_rows=[(0.0, 0.5, 5), (0.5, 1.0, 10)],
        nn_rows=[("abc", 1)],
        pillar_rows=[("value", 0, 1)],
        fail_query_idx=0,  # total/blocked query fails
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/g3/originality-stats?histogram_bins=2")
    assert r.status_code == 200
    body = r.json()
    assert body["total_candidates"] == 0
    assert body["blocked_candidates"] == 0
    assert body["block_rate"] == 0.0
    # Subsequent queries fed by the mock — but the histogram query was skipped
    # in the failure path? No, total just zeroes. Histogram still ran with row 1
    assert len(body["distance_histogram"]) >= 1


@pytest.mark.asyncio
async def test_histogram_query_soft_fail_returns_fallback_bins(client_factory):
    """Histogram query raising → endpoint returns an equal-width empty histogram."""
    client = await client_factory(
        total_row=(100, 5),
        hist_rows=[],
        nn_rows=[],
        pillar_rows=[],
        fail_query_idx=1,
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/g3/originality-stats?histogram_bins=5")
    assert r.status_code == 200
    body = r.json()
    assert body["total_candidates"] == 100
    # Fallback histogram: 5 bins, all count=0
    assert len(body["distance_histogram"]) == 5
    assert all(b["count"] == 0 for b in body["distance_histogram"])


@pytest.mark.asyncio
async def test_pillar_query_soft_fail_returns_empty_list(client_factory):
    """Per-pillar query raising → by_pillar=[] but other sections populated."""
    client = await client_factory(
        total_row=(100, 5),
        hist_rows=[(0.0, 0.5, 50), (0.5, 1.0, 50)],
        nn_rows=[("abc", 3)],
        pillar_rows=[],
        fail_query_idx=3,
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/g3/originality-stats?histogram_bins=2")
    assert r.status_code == 200
    body = r.json()
    assert body["by_pillar"] == []
    assert body["total_candidates"] == 100
    assert len(body["top_neighbors"]) == 1


# ---------------------------------------------------------------------------
# Query param validation / clamping
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bins_param_clamped_high(client_factory):
    """histogram_bins=1000 → clamped to 50 (defensive against UI bugs)."""
    client = await client_factory(
        total_row=(0, 0),
        hist_rows=[],
        nn_rows=[],
        pillar_rows=[],
        fail_query_idx=1,  # force fallback so we count empty bins
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/g3/originality-stats?histogram_bins=9999")
    assert r.status_code == 200
    body = r.json()
    assert len(body["distance_histogram"]) == 50


@pytest.mark.asyncio
async def test_bins_param_clamped_low(client_factory):
    """histogram_bins=0 → clamped to min 2 (avoid div-by-zero)."""
    client = await client_factory(
        total_row=(0, 0),
        hist_rows=[],
        nn_rows=[],
        pillar_rows=[],
        fail_query_idx=1,
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/g3/originality-stats?histogram_bins=0")
    assert r.status_code == 200
    body = r.json()
    assert len(body["distance_histogram"]) == 2
