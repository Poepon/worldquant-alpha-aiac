"""G8 Phase A follow-up — /ops/hypothesis/forest endpoint integration tests
(2026-05-19).

Mocks AsyncSession.execute returning the 4 SQL query results in order:
  1. head COUNT eligible
  2. top_rows SELECT (forest entries)
  3. per-entry COUNT alphas referencing this hid (one call per entry)
  4. refed (total + pass count of referenced alphas)
  5. pillar_rows GROUP BY pillar
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
def _isolate_g8_flag():
    from backend.config import settings as _stg
    prev = getattr(_stg, "ENABLE_HYPOTHESIS_FOREST_REUSE", False)
    yield
    setattr(_stg, "ENABLE_HYPOTHESIS_FOREST_REUSE", prev)


def _mock_db_for_forest(
    *,
    head: Tuple,
    top_rows: List[Tuple],
    per_entry_counts: List[Tuple],
    refed: Tuple,
    pillar_rows: List[Tuple],
):
    """Build AsyncSession mock with 6 sequential execute calls."""

    def _one_result(row):
        r = MagicMock()
        r.one = MagicMock(return_value=row)
        return r

    def _rows_result(rows):
        r = MagicMock()
        r.all = MagicMock(return_value=list(rows))
        return r

    # Order:
    #   1. head COUNT
    #   2. top_rows SELECT
    #   3..(2+N). per-entry count (N = len(top_rows))
    #   then refed
    #   then pillar_rows
    n_entries = len(top_rows)
    results = (
        [_one_result(head), _rows_result(top_rows)]
        + [_one_result(c) for c in per_entry_counts]
        + [_one_result(refed), _rows_result(pillar_rows)]
    )

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=results)
    return db


@pytest_asyncio.fixture
async def client_factory():
    async def _build(*, head, top_rows, per_entry_counts, refed, pillar_rows):
        app = FastAPI()
        app.include_router(ops_router, prefix="/api/v1")
        app.dependency_overrides[get_db] = lambda: _mock_db_for_forest(
            head=head, top_rows=top_rows,
            per_entry_counts=per_entry_counts,
            refed=refed, pillar_rows=pillar_rows,
        )
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test")
    return _build


@pytest.mark.asyncio
async def test_forest_aggregates_pool_and_attribution(client_factory):
    """Typical 7-day window with 2 PROMOTED hypotheses, 12 referenced alphas."""
    from backend.config import settings as _stg
    setattr(_stg, "ENABLE_HYPOTHESIS_FOREST_REUSE", True)

    head = (2,)  # eligible_count
    top_rows = [
        # (id, statement, pillar, region, sharpe_avg, pass_count, alpha_count, status)
        (101, "momentum stmt", "momentum", "USA", 1.85, 3, 5, "PROMOTED"),
        (202, "value stmt", "value", "USA", 1.40, 2, 4, "ACTIVE"),
    ]
    # per-entry alphas referenced count (in order matching top_rows)
    per_entry_counts = [(7,), (5,)]  # 101 ref'd 7×, 202 ref'd 5×
    refed = (12, 8)  # total / pass
    pillar_rows = [
        ("momentum", 1, 1.85, 3),
        ("value", 1, 1.40, 2),
    ]

    client = await client_factory(
        head=head, top_rows=top_rows,
        per_entry_counts=per_entry_counts,
        refed=refed, pillar_rows=pillar_rows,
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/hypothesis/forest?days=7&top_n=5")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["window_days"] == 7
    assert body["eligible_count"] == 2
    assert body["total_referenced_alphas"] == 12
    assert body["reference_pass_count"] == 8
    assert body["reference_pass_rate"] == round(8 / 12, 4)
    assert body["flags"]["ENABLE_HYPOTHESIS_FOREST_REUSE"] is True
    assert body["is_healthy"] is True

    assert len(body["top_entries"]) == 2
    assert body["top_entries"][0]["hypothesis_id"] == 101
    assert body["top_entries"][0]["times_referenced"] == 7
    assert body["top_entries"][0]["sharpe_avg"] == 1.85
    assert body["top_entries"][1]["hypothesis_id"] == 202
    assert body["top_entries"][1]["times_referenced"] == 5

    assert len(body["pillar_breakdown"]) == 2
    assert {p["pillar"] for p in body["pillar_breakdown"]} == {"momentum", "value"}


@pytest.mark.asyncio
async def test_forest_empty_pool_unhealthy(client_factory):
    """Flag ON but eligible_count=0 → unhealthy + no per-entry queries."""
    from backend.config import settings as _stg
    setattr(_stg, "ENABLE_HYPOTHESIS_FOREST_REUSE", True)

    client = await client_factory(
        head=(0,), top_rows=[], per_entry_counts=[],
        refed=(0, 0), pillar_rows=[],
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/hypothesis/forest")
    body = r.json()
    assert body["eligible_count"] == 0
    assert body["total_referenced_alphas"] == 0
    assert body["top_entries"] == []
    assert body["pillar_breakdown"] == []
    # Healthy false: pool empty
    assert body["is_healthy"] is False


@pytest.mark.asyncio
async def test_forest_flag_off_unhealthy(client_factory):
    """Flag OFF marks unhealthy even with eligible rows."""
    from backend.config import settings as _stg
    setattr(_stg, "ENABLE_HYPOTHESIS_FOREST_REUSE", False)

    client = await client_factory(
        head=(2,),
        top_rows=[(1, "x", "momentum", "USA", 1.5, 2, 3, "PROMOTED")],
        per_entry_counts=[(0,)],
        refed=(0, 0),
        pillar_rows=[("momentum", 1, 1.5, 2)],
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/hypothesis/forest")
    body = r.json()
    assert body["flags"]["ENABLE_HYPOTHESIS_FOREST_REUSE"] is False
    assert body["is_healthy"] is False


@pytest.mark.asyncio
async def test_forest_no_referenced_alphas_unhealthy(client_factory):
    """Eligible pool exists but no alpha referenced any of them → unhealthy."""
    from backend.config import settings as _stg
    setattr(_stg, "ENABLE_HYPOTHESIS_FOREST_REUSE", True)

    client = await client_factory(
        head=(1,),
        top_rows=[(42, "x", "value", "USA", 1.5, 2, 3, "PROMOTED")],
        per_entry_counts=[(0,)],
        refed=(0, 0),
        pillar_rows=[("value", 1, 1.5, 2)],
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/hypothesis/forest")
    body = r.json()
    assert body["eligible_count"] == 1
    assert body["total_referenced_alphas"] == 0
    assert body["is_healthy"] is False  # block stamping but no alpha → unhealthy


@pytest.mark.asyncio
async def test_forest_region_filter_passes_through(client_factory):
    """region query param echoed in response."""
    from backend.config import settings as _stg
    setattr(_stg, "ENABLE_HYPOTHESIS_FOREST_REUSE", True)

    client = await client_factory(
        head=(0,), top_rows=[], per_entry_counts=[],
        refed=(0, 0), pillar_rows=[],
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/hypothesis/forest?region=CHN")
    body = r.json()
    assert body["region"] == "CHN"


@pytest.mark.asyncio
async def test_forest_requires_ops_token_when_set(client_factory):
    from backend.config import settings as _stg
    setattr(_stg, "ENABLE_HYPOTHESIS_FOREST_REUSE", True)
    os.environ["OPS_API_TOKEN"] = "secret456"
    try:
        client = await client_factory(
            head=(0,), top_rows=[], per_entry_counts=[],
            refed=(0, 0), pillar_rows=[],
        )
        async with client as ac:
            r = await ac.get("/api/v1/ops/hypothesis/forest")
            assert r.status_code == 401
    finally:
        os.environ.pop("OPS_API_TOKEN", None)
