"""Integration: GET /ops/direction-bandit/telemetry (G1 Phase A, 2026-05-19).

Verifies the new direction-bandit telemetry endpoint which aggregates
``direction_bandit_log`` + joins ``alphas.metrics``\\->>'_direction_bandit_
recommended_arm' over a configurable window.

Mocks the AsyncSession.execute side_effect to return 5 result objects in order:
  1. headline (one row: rows_total, distinct_tasks, distinct_segments)
  2. by_arm (rows: arm, pulls, avg_r, sample, cold_pulls)
  3. by_arm PASS-rate join from alphas (rows: arm, n, p)
  4. by_segment (rows: segment_id, region, dscat, fp, pulls, distinct_arms)
  5. GO-gate counter (scalar_one_or_none)

Follows the same pattern as test_ops_cost_telemetry.py / test_r1a_ops_telemetry.py.
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
def _isolate_bandit_flag():
    from backend.config import settings as _stg
    prev = getattr(_stg, "ENABLE_DIRECTION_BANDIT", False)
    yield
    setattr(_stg, "ENABLE_DIRECTION_BANDIT", prev)


def _mock_db(
    *,
    head: Tuple,
    by_arm: List[Tuple],
    pass_rate: List[Tuple],
    by_segment: List[Tuple],
    gate_count: int,
    pass_rate_raises: bool = False,
):
    """Build a mock AsyncSession returning the 5 queries in order."""
    head_result = MagicMock()
    head_result.one = MagicMock(return_value=head)

    arm_result = MagicMock()
    arm_result.all = MagicMock(return_value=list(by_arm))

    pass_result = MagicMock()
    pass_result.all = MagicMock(return_value=list(pass_rate))

    seg_result = MagicMock()
    seg_result.all = MagicMock(return_value=list(by_segment))

    gate_result = MagicMock()
    gate_result.scalar_one_or_none = MagicMock(return_value=int(gate_count))

    side: list = [head_result, arm_result]
    if pass_rate_raises:
        side.append(RuntimeError("alphas table missing in fixture"))
    else:
        side.append(pass_result)
    side.extend([seg_result, gate_result])

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=side)
    return db


@pytest_asyncio.fixture
async def client_factory():
    async def _build(*, head, by_arm, pass_rate, by_segment, gate_count,
                     pass_rate_raises=False):
        app = FastAPI()
        app.include_router(ops_router, prefix="/api/v1")
        app.dependency_overrides[get_db] = lambda: _mock_db(
            head=head, by_arm=by_arm, pass_rate=pass_rate,
            by_segment=by_segment, gate_count=gate_count,
            pass_rate_raises=pass_rate_raises,
        )
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test")
    return _build


# ---------------------------------------------------------------------------
# Endpoint contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_telemetry_aggregates_per_arm_with_pass_rate_join(client_factory):
    """Typical 7-day window: 4 arms each pulled, PASS rates joined."""
    from backend.config import settings as _stg
    setattr(_stg, "ENABLE_DIRECTION_BANDIT", True)

    head = (200, 8, 12)  # 200 log rows, 8 tasks, 12 distinct segments
    by_arm = [
        # (arm, pulls, avg_observed_reward, sample_size, cold_pulls)
        ("rag_template",     80, 0.65, 75,  5),
        ("knowledge_pattern", 60, 0.50, 55, 10),
        ("llm_generation",   40, 0.35, 38,  2),
        ("genetic_mutation", 20, 0.20, 19,  1),
    ]
    pass_rate = [
        # (arm, alpha_count_with_stamp, pass_count)
        ("rag_template",     30, 12),  # 40% PASS
        ("knowledge_pattern", 25,  8),  # 32% PASS
        ("llm_generation",   20,  4),  # 20% PASS
        ("genetic_mutation", 10,  1),  # 10% PASS
    ]
    by_segment = [
        # (segment_id, region, dscat, fp, pulls, distinct_arms)
        ("USA|pricevolume|hypothesis",     "USA", "pricevolume", "hypothesis", 50, 4),
        ("USA|fundamental6|implementation", "USA", "fundamental6", "implementation", 40, 3),
    ]
    gate_count = 2  # 2 segments crossed ≥ 10 pulls

    client = await client_factory(
        head=head, by_arm=by_arm, pass_rate=pass_rate,
        by_segment=by_segment, gate_count=gate_count,
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/direction-bandit/telemetry?days=7")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["window_days"] == 7
    assert body["total_log_rows"] == 200
    assert body["distinct_tasks"] == 8
    assert body["distinct_segments"] == 12
    assert body["flags"]["ENABLE_DIRECTION_BANDIT"] is True

    # Per-arm — rows preserved, PASS rates joined.
    by_arm_out = body["by_arm"]
    assert len(by_arm_out) == 4
    arm0 = by_arm_out[0]
    assert arm0["arm"] == "rag_template"
    assert arm0["pulls"] == 80
    assert arm0["avg_observed_reward"] == 0.65
    assert arm0["sample_size_for_reward"] == 75
    assert arm0["cold_start_pulls"] == 5
    assert arm0["pass_rate"] == 0.4
    assert arm0["pass_sample_size"] == 30

    # Best arm = highest avg_observed_reward (with sample > 0).
    assert body["best_arm"] == "rag_template"
    assert body["best_arm_avg_reward"] == 0.65

    # Regret = best - weighted-actual.
    # weighted_actual = (0.65*75 + 0.50*55 + 0.35*38 + 0.20*19) / (75+55+38+19)
    #                 = (48.75 + 27.5 + 13.3 + 3.8) / 187
    #                 = 93.35 / 187 = 0.499197...
    # regret = 0.65 - 0.499197 = 0.150803
    assert body["approx_regret"] == pytest.approx(0.150803, abs=1e-4)

    # GO gate ready.
    assert body["go_gate_min_pulls"] == 10
    assert body["go_gate_segments_ready"] == 2
    assert body["is_healthy"] is True


@pytest.mark.asyncio
async def test_telemetry_empty_log_returns_zero_and_unhealthy(client_factory):
    """Flag ON but no rows yet → total=0 + unhealthy (haven't started capturing)."""
    from backend.config import settings as _stg
    setattr(_stg, "ENABLE_DIRECTION_BANDIT", True)

    client = await client_factory(
        head=(0, 0, 0),
        by_arm=[], pass_rate=[], by_segment=[], gate_count=0,
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/direction-bandit/telemetry")

    assert r.status_code == 200
    body = r.json()
    assert body["total_log_rows"] == 0
    assert body["by_arm"] == []
    assert body["by_segment"] == []
    assert body["best_arm"] is None
    assert body["best_arm_avg_reward"] is None
    assert body["approx_regret"] is None
    assert body["is_healthy"] is False


@pytest.mark.asyncio
async def test_telemetry_flag_off_reports_unhealthy(client_factory):
    """Flag OFF → is_healthy False even if rows somehow exist (cold-rollout)."""
    from backend.config import settings as _stg
    setattr(_stg, "ENABLE_DIRECTION_BANDIT", False)

    client = await client_factory(
        head=(50, 1, 1),
        by_arm=[("rag_template", 50, 0.5, 50, 0)],
        pass_rate=[("rag_template", 20, 8)],
        by_segment=[("X|y|z", "X", "y", "z", 50, 1)],
        gate_count=1,
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/direction-bandit/telemetry")
    body = r.json()
    assert body["flags"]["ENABLE_DIRECTION_BANDIT"] is False
    assert body["is_healthy"] is False


@pytest.mark.asyncio
async def test_telemetry_gate_count_zero_marks_unhealthy(client_factory):
    """Flag ON + rows captured but NO segment ≥ 10 pulls → still unhealthy
    (Phase 1 R2/Q7 GO gate signal — bandit hasn't gathered enough data)."""
    from backend.config import settings as _stg
    setattr(_stg, "ENABLE_DIRECTION_BANDIT", True)

    client = await client_factory(
        head=(15, 5, 8),  # 15 rows spread thin across 8 segments
        by_arm=[
            ("rag_template", 8, 0.4, 7, 0),
            ("llm_generation", 7, 0.3, 7, 0),
        ],
        pass_rate=[],
        by_segment=[("A|b|c", "A", "b", "c", 4, 2)],
        gate_count=0,  # No segment crossed min_pulls=10
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/direction-bandit/telemetry")
    body = r.json()
    assert body["total_log_rows"] == 15
    assert body["go_gate_segments_ready"] == 0
    assert body["is_healthy"] is False


@pytest.mark.asyncio
async def test_telemetry_handles_pass_rate_join_failure_gracefully(client_factory):
    """If the alphas table query raises (migration not applied, etc.),
    per-arm stats still return with pass_rate=None — endpoint must NOT 500."""
    from backend.config import settings as _stg
    setattr(_stg, "ENABLE_DIRECTION_BANDIT", True)

    client = await client_factory(
        head=(20, 2, 4),
        by_arm=[
            ("rag_template", 10, 0.6, 10, 0),
            ("llm_generation", 10, 0.4, 10, 0),
        ],
        pass_rate=[],
        by_segment=[("USA|x|y", "USA", "x", "y", 20, 2)],
        gate_count=1,
        pass_rate_raises=True,
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/direction-bandit/telemetry")
    assert r.status_code == 200
    body = r.json()
    # Both arms still returned, but no pass_rate joined.
    assert len(body["by_arm"]) == 2
    for a in body["by_arm"]:
        assert a["pass_rate"] is None
        assert a["pass_sample_size"] == 0


@pytest.mark.asyncio
async def test_telemetry_excludes_null_reward_from_avg(client_factory):
    """Round 1 selects have observed_reward=NULL (no prior arm to credit).
    avg_observed_reward MUST skip NULLs (otherwise round 1 drags every arm
    toward 0). Verified by passing sample < pulls."""
    from backend.config import settings as _stg
    setattr(_stg, "ENABLE_DIRECTION_BANDIT", True)

    client = await client_factory(
        head=(40, 2, 2),
        # pulls=20 but only 15 carry a reward (5 are round-1 NULLs).
        by_arm=[
            ("rag_template", 20, 0.70, 15, 0),
            ("llm_generation", 20, 0.30, 15, 0),
        ],
        pass_rate=[],
        by_segment=[("USA|x|y", "USA", "x", "y", 40, 2)],
        gate_count=2,
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/direction-bandit/telemetry")
    body = r.json()
    arm = body["by_arm"][0]
    assert arm["pulls"] == 20
    assert arm["sample_size_for_reward"] == 15
    # Best arm computed over non-null sample only.
    assert body["best_arm"] == "rag_template"
    assert body["best_arm_avg_reward"] == 0.70


@pytest.mark.asyncio
async def test_telemetry_requires_ops_token_when_set(client_factory):
    """OPS_API_TOKEN set → 401 without header."""
    from backend.config import settings as _stg
    setattr(_stg, "ENABLE_DIRECTION_BANDIT", True)
    os.environ["OPS_API_TOKEN"] = "secret-bandit"
    try:
        client = await client_factory(
            head=(0, 0, 0), by_arm=[], pass_rate=[],
            by_segment=[], gate_count=0,
        )
        async with client as ac:
            r = await ac.get("/api/v1/ops/direction-bandit/telemetry")
            assert r.status_code == 401
    finally:
        os.environ.pop("OPS_API_TOKEN", None)


@pytest.mark.asyncio
async def test_telemetry_window_days_param_echoed(client_factory):
    from backend.config import settings as _stg
    setattr(_stg, "ENABLE_DIRECTION_BANDIT", True)

    client = await client_factory(
        head=(0, 0, 0), by_arm=[], pass_rate=[],
        by_segment=[], gate_count=0,
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/direction-bandit/telemetry?days=30")
    assert r.status_code == 200
    assert r.json()["window_days"] == 30


@pytest.mark.asyncio
async def test_telemetry_best_arm_skips_arms_with_zero_sample(client_factory):
    """An arm pulled but with zero rewarded samples (all round-1 NULLs) must
    NOT be eligible as best_arm — best is picked over reward-bearing samples
    only, otherwise a single rewarded arm with low reward beats a sample-0
    arm with default 0.0."""
    from backend.config import settings as _stg
    setattr(_stg, "ENABLE_DIRECTION_BANDIT", True)

    client = await client_factory(
        head=(10, 1, 1),
        by_arm=[
            # arm_A: 5 pulls but 0 rewarded → SQL returns avg_r=0.0, sample=0
            ("arm_A", 5, 0.0, 0, 0),
            # arm_B: 5 pulls, 5 rewarded with avg 0.25 — wins.
            ("arm_B", 5, 0.25, 5, 0),
        ],
        pass_rate=[],
        by_segment=[("USA|x|y", "USA", "x", "y", 10, 2)],
        gate_count=1,
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/direction-bandit/telemetry")
    body = r.json()
    assert body["best_arm"] == "arm_B"
    assert body["best_arm_avg_reward"] == 0.25
    # Approx regret weighted only by sample-bearing arms: 0.25 - 0.25 = 0.0
    assert body["approx_regret"] == 0.0
