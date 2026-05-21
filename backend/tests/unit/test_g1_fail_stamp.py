"""G1 follow-up (2026-05-19): unit tests for FAIL-path bandit-arm stamp +
/ops/direction-bandit/telemetry UNION SQL contract.

Coverage:
  A. AlphaFailure model schema (new column + index)
  B. workflow.run_with_persistence stamps fail_record with _g1_bandit_arm
     (mocked path — full integration covered in test_ops_direction_bandit
     _telemetry.py via the endpoint contract test)
  C. /ops/direction-bandit/telemetry pass_rate now sources from PASS+FAIL
     UNION (denominator includes fails)
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


# ---------------------------------------------------------------------------
# A. AlphaFailure model schema
# ---------------------------------------------------------------------------


def test_alpha_failure_has_bandit_arm_column():
    """Column exists with the documented shape: String(40) nullable + indexed."""
    from backend.models import AlphaFailure
    cols = {c.name: c for c in AlphaFailure.__table__.columns}
    assert "bandit_arm_recommended" in cols
    col = cols["bandit_arm_recommended"]
    assert col.nullable is True
    assert col.type.length == 40
    assert col.index is True


def test_alpha_failure_bandit_arm_indexed():
    """SQLAlchemy auto-named index ix_alpha_failures_bandit_arm_recommended
    matches the Alembic revision (h8d3c9f2e1b6)."""
    from backend.models import AlphaFailure
    idx_names = {i.name for i in AlphaFailure.__table__.indexes}
    assert "ix_alpha_failures_bandit_arm_recommended" in idx_names


# ---------------------------------------------------------------------------
# B. workflow.run_with_persistence stamp (constructor accepts the kwarg)
# ---------------------------------------------------------------------------


def test_alpha_failure_constructor_accepts_bandit_arm():
    """Defensive: callers passing bandit_arm_recommended=... must not raise.
    This is the exact constructor invocation inside workflow.run_with_
    persistence's fail INSERT loop."""
    from backend.models import AlphaFailure
    rec = AlphaFailure(
        task_id=1,
        run_id=10,
        expression="ts_rank(returns, 20)",
        error_type="QUALITY_CHECK_FAILED",
        error_message="sharpe below threshold",
        hypothesis_id=None,
        bandit_arm_recommended="rag_template",
    )
    assert rec.bandit_arm_recommended == "rag_template"


def test_alpha_failure_constructor_accepts_none():
    """Flag OFF / round 1 / read failed → bandit_arm_recommended=None.
    NULL is the legacy default and must not break the constructor."""
    from backend.models import AlphaFailure
    rec = AlphaFailure(
        task_id=1,
        expression="x",
        bandit_arm_recommended=None,
    )
    assert rec.bandit_arm_recommended is None


# ---------------------------------------------------------------------------
# C. /ops/direction-bandit/telemetry UNION SQL contract
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_ops_token():
    prev = os.environ.pop("OPS_API_TOKEN", None)
    yield
    if prev is not None:
        os.environ["OPS_API_TOKEN"] = prev
    else:
        os.environ.pop("OPS_API_TOKEN", None)


def _mock_db_for_direction_bandit(
    *,
    head_row: Tuple,
    arm_rows: List[Tuple],
    pass_rate_rows: List[Tuple],
    seg_rows: List[Tuple],
    gate_count: int,
):
    """Build AsyncSession mock returning 5 sequential execute results to
    match /ops/direction-bandit/telemetry's 5 SQL queries (head / arm /
    per-arm PASS rate UNION / segments / gate count)."""

    def _one(row):
        r = MagicMock()
        r.one = MagicMock(return_value=row)
        return r

    def _all(rows):
        r = MagicMock()
        r.all = MagicMock(return_value=list(rows))
        return r

    def _scalar(v):
        r = MagicMock()
        r.scalar_one_or_none = MagicMock(return_value=v)
        return r

    # Endpoint order:
    #   1. head — (rows_total, distinct_tasks, distinct_segs) via .one()
    #   2. arm aggregate via .all()
    #   3. pass-rate UNION via .all() <-- G1 follow-up behavior tested here
    #   4. seg_rows via .all()
    #   5. gate count via .scalar_one_or_none()
    results = [
        _one(head_row),
        _all(arm_rows),
        _all(pass_rate_rows),
        _all(seg_rows),
        _scalar(gate_count),
    ]
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=results)
    return db


@pytest_asyncio.fixture
async def client_factory():
    async def _build(*, head_row, arm_rows, pass_rate_rows, seg_rows, gate_count):
        app = FastAPI()
        app.include_router(ops_router, prefix="/api/v1")
        app.dependency_overrides[get_db] = lambda: _mock_db_for_direction_bandit(
            head_row=head_row, arm_rows=arm_rows, pass_rate_rows=pass_rate_rows,
            seg_rows=seg_rows, gate_count=gate_count,
        )
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test")
    return _build


@pytest.mark.asyncio
async def test_pass_rate_denominator_includes_fails(client_factory):
    """G1 follow-up invariant: per-arm pass_rate denominator = PASS + FAIL
    on that arm, not PASS-only. UNION ALL of alphas.metrics + alpha_failures
    .bandit_arm_recommended produces this denominator. We feed the mock
    db.execute() a pass_rate_rows that reflects the UNION (8 total = 3 pass
    + 5 fail under arm 'rag_template') and verify the endpoint reports
    pass_rate = 3/8 = 0.375, NOT 3/3 = 1.0."""
    arm_rows = [
        # (arm, pulls, avg_observed_reward, sample_size, cold_pulls)
        ("rag_template", 8, 0.42, 8, 2),
        ("llm_generation", 5, 0.55, 5, 1),
    ]
    # Post-UNION counts as the endpoint receives them
    pass_rate_rows = [
        # (arm, n=PASS+FAIL, p=PASS)
        ("rag_template", 8, 3),    # 3/8 = 0.375 (G1 follow-up: was 3/3 = 1.0 PASS-only)
        ("llm_generation", 5, 3),  # 3/5 = 0.6
    ]
    seg_rows = []
    gate_count = 0
    head_row = (13, 1, 2)  # rows_total, distinct_tasks, distinct_segs
    client = await client_factory(
        head_row=head_row, arm_rows=arm_rows, pass_rate_rows=pass_rate_rows,
        seg_rows=seg_rows, gate_count=gate_count,
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/direction-bandit/telemetry?days=7")
    assert r.status_code == 200, r.text
    body = r.json()
    by_arm = {row["arm"]: row for row in body["by_arm"]}
    assert by_arm["rag_template"]["pass_rate"] == 0.375
    assert by_arm["rag_template"]["pass_sample_size"] == 8
    assert by_arm["llm_generation"]["pass_rate"] == 0.6
    assert by_arm["llm_generation"]["pass_sample_size"] == 5


@pytest.mark.asyncio
async def test_pass_rate_empty_union_returns_none(client_factory):
    """No rows in either alphas.metrics or alpha_failures → empty UNION →
    pass_rate stays None per-arm (existing legacy behavior unchanged)."""
    arm_rows = [("rag_template", 3, 0.30, 3, 1)]
    pass_rate_rows = []
    client = await client_factory(
        head_row=(3, 1, 1), arm_rows=arm_rows,
        pass_rate_rows=pass_rate_rows, seg_rows=[], gate_count=0,
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/direction-bandit/telemetry")
    body = r.json()
    assert body["by_arm"][0]["pass_rate"] is None
    assert body["by_arm"][0]["pass_sample_size"] == 0
