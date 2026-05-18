"""Integration: GET /ops/r1b/telemetry + /ops/r1b/chain-depth-distribution (2026-05-18).

Verifies the R1b operator decision-support endpoints introduced this
session as a follow-up to R1b plan §10 deploy sequence. Both endpoints
aggregate over r1b_retry_log + hypotheses + MiningTask.config via raw
SQL; tests mock AsyncSession.execute to return canonical row tuples and
assert the JSON contract + computed fields (success rates, weighted avg
depth, top-N ordering).
"""
from __future__ import annotations

import os
from typing import AsyncGenerator, List, Tuple
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


def _mock_db_multi_execute(*per_call_rows: List[Tuple]):
    """Build AsyncSession-like mock returning each call's rows in order.

    The endpoints call execute() multiple times (stat query + budget query
    for telemetry; single chain-depth query for distribution).
    """
    results = []
    for rows in per_call_rows:
        r = MagicMock()
        r.all = MagicMock(return_value=list(rows))
        results.append(r)

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=results)
    return db


_MISSING = object()


@pytest.fixture
def _isolate_r1b_settings():
    """Snapshot + restore settings that other tests in this file mutate
    (R1B_MAX_MUTATION_DEPTH most notably). Without this autouse fixture
    a sibling unit test (test_r1b_mutate.py::test_mutate_depth_cap_*)
    sees a leaked override and either over- or under-counts the cap.
    Mirrors the _isolate_flag_state pattern added to
    test_costeer_deploy_recommendation 2026-05-18.

    Review LOW #2 fix (2026-05-18):
    1. Expand ``keys`` to cover all 4 sibling R1b settings so a future
       test that overrides ``R1B_MAX_RETRIES_PER_ALPHA``,
       ``R1B_MAX_MUTATIONS_PER_DATASET_CYCLE``, or
       ``R1B_MAX_COST_USD_PER_ROUND`` does not leak into other tests.
    2. Use a sentinel (``_MISSING``) so the restore loop distinguishes
       "key did not exist on settings at snapshot time" from "key was
       legitimately ``None``". Restore unconditionally for present keys
       and ``delattr`` for snapshotted-absent keys (future Optional
       settings would otherwise round-trip absent → None silently).
    """
    from backend.config import settings as _stg
    keys = (
        "R1B_MAX_MUTATION_DEPTH",
        "R1B_MAX_RETRIES_PER_ALPHA",
        "R1B_MAX_MUTATIONS_PER_DATASET_CYCLE",
        "R1B_MAX_COST_USD_PER_ROUND",
    )
    saved = {k: getattr(_stg, k, _MISSING) for k in keys}
    yield
    for k, v in saved.items():
        if v is _MISSING:
            # key did not exist at snapshot time — drop any value a test added
            if hasattr(_stg, k):
                try:
                    delattr(_stg, k)
                except AttributeError:
                    # pydantic model attrs may not be deletable; best effort
                    pass
        else:
            setattr(_stg, k, v)


@pytest_asyncio.fixture
async def client_factory(_isolate_r1b_settings):
    async def _build(per_call_rows, settings_overrides=None):
        app = FastAPI()
        app.include_router(ops_router, prefix="/api/v1")
        app.dependency_overrides[get_db] = lambda: _mock_db_multi_execute(*per_call_rows)
        if settings_overrides:
            from backend.config import settings as _stg
            for k, v in settings_overrides.items():
                setattr(_stg, k, v)
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test")
    return _build


# ---------------------------------------------------------------------------
# Telemetry endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_telemetry_aggregates_success_rate_per_attempt_type(client_factory):
    """retry_impl 3 pass / 1 fail → rate 0.75; mutate_hyp 1 pass / 1 fail → 0.5."""
    stat_rows = [
        # (attempt_type, outcome, n, cost, toks)
        ("retry_impl", "pass", 3, 0.030, 1500),
        ("retry_impl", "fail", 1, 0.010,  500),
        ("retry_impl", "pending", 2, 0.020, 800),
        ("mutate_hyp", "pass", 1, 0.015, 700),
        ("mutate_hyp", "fail", 1, 0.012, 600),
    ]
    budget_rows = [
        (101, 5, 2, 0.085),
        (202, 3, 0, 0.030),
    ]
    client = await client_factory([stat_rows, budget_rows])
    async with client as ac:
        r = await ac.get("/api/v1/ops/r1b/telemetry?days=7&top_n=2")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success_rate_retry_impl"] == 0.75
    assert body["success_rate_mutate_hyp"] == 0.5
    assert body["window_days"] == 7
    # Pending NOT counted into either pass/fail rate denominator
    assert body["total_attempts_in_window"] == 3 + 1 + 2 + 1 + 1
    # Top tasks by budget descending — task 101 first (higher cost)
    tops = body["top_tasks_by_budget"]
    assert len(tops) == 2
    assert tops[0]["task_id"] == 101
    assert tops[0]["cost_usd_total"] == 0.085


@pytest.mark.asyncio
async def test_telemetry_empty_log_returns_zero_rates(client_factory):
    """No retry_log rows → both rates 0.0, attempt_stats empty."""
    client = await client_factory([[], []])
    async with client as ac:
        r = await ac.get("/api/v1/ops/r1b/telemetry")
    assert r.status_code == 200
    body = r.json()
    assert body["success_rate_retry_impl"] == 0.0
    assert body["success_rate_mutate_hyp"] == 0.0
    assert body["attempt_stats"] == []
    assert body["top_tasks_by_budget"] == []
    assert body["total_attempts_in_window"] == 0


@pytest.mark.asyncio
async def test_telemetry_exposes_all_5_r1b_flags(client_factory):
    """flags dict must include exactly the 5 ENABLE_R1B_* keys."""
    client = await client_factory([[], []])
    async with client as ac:
        r = await ac.get("/api/v1/ops/r1b/telemetry")
    flags = r.json()["flags"]
    expected = {
        "ENABLE_R1B_RETRY_LOOP", "ENABLE_R1B_HYPOTHESIS_MUTATE",
        "ENABLE_R1B_FAILURE_TREE", "ENABLE_R1B_TYPED_PIPELINE",
        "ENABLE_R1B_DAG_RETRY_REWARD",
    }
    assert set(flags.keys()) == expected
    for v in flags.values():
        assert isinstance(v, bool)


@pytest.mark.asyncio
async def test_telemetry_pass_fail_only_for_known_attempt_types(client_factory):
    """Unknown attempt_type rows count toward total but NOT toward rates."""
    stat_rows = [
        ("retry_impl", "pass", 1, 0.005, 200),
        # 'spurious_type' is not retry_impl/mutate_hyp — must not poison rates
        ("spurious_type", "pass", 99, 0.0, 0),
    ]
    client = await client_factory([stat_rows, []])
    async with client as ac:
        r = await ac.get("/api/v1/ops/r1b/telemetry")
    body = r.json()
    # retry_impl: 1 pass / 0 fail = denom 1, rate 1.0
    assert body["success_rate_retry_impl"] == 1.0
    assert body["success_rate_mutate_hyp"] == 0.0
    # Spurious rows still surfaced in attempt_stats list
    assert any(s["attempt_type"] == "spurious_type" for s in body["attempt_stats"])


@pytest.mark.asyncio
async def test_telemetry_requires_ops_token_when_env_set(client_factory):
    os.environ["OPS_API_TOKEN"] = "abc123"
    client = await client_factory([[], []])
    async with client as ac:
        r = await ac.get("/api/v1/ops/r1b/telemetry")  # no X-Ops-Token header
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Chain depth distribution endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chain_depth_distribution_aggregates_buckets(client_factory):
    """Histogram input → distribution list + max_depth + avg + root/mutated split."""
    # (mutation_depth, count)
    chain_rows = [
        (0, 100),  # roots
        (1, 30),
        (2, 8),
        (3, 2),
    ]
    client = await client_factory([chain_rows], settings_overrides={"R1B_MAX_MUTATION_DEPTH": 3})
    async with client as ac:
        r = await ac.get("/api/v1/ops/r1b/chain-depth-distribution")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_root_hypotheses"] == 100
    assert body["total_mutated_hypotheses"] == 30 + 8 + 2
    assert body["max_depth_observed"] == 3
    # Weighted avg = (0*100 + 1*30 + 2*8 + 3*2) / 140 = 52/140 ≈ 0.3714
    assert body["chain_depth_avg"] == 0.3714
    assert len(body["distribution"]) == 4
    assert body["distribution"][0]["mutation_depth"] == 0


@pytest.mark.asyncio
async def test_chain_depth_empty_returns_zeros(client_factory):
    """No hypotheses → all counts 0, avg 0.0."""
    client = await client_factory([[]])
    async with client as ac:
        r = await ac.get("/api/v1/ops/r1b/chain-depth-distribution")
    assert r.status_code == 200
    body = r.json()
    assert body["distribution"] == []
    assert body["max_depth_observed"] == 0
    assert body["total_mutated_hypotheses"] == 0
    assert body["total_root_hypotheses"] == 0
    assert body["chain_depth_avg"] == 0.0
    # Cap-firing surface still present on empty KB
    assert body["tasks_at_or_above_cap_count"] == 0
    assert body["r1b_max_mutation_depth_setting"] >= 1


@pytest.mark.asyncio
async def test_chain_depth_surfaces_cap_firing_count(client_factory):
    """Review LOW 3: response surfaces N tasks at depth >= configured cap.

    Setting R1B_MAX_MUTATION_DEPTH=2 over distribution {0:100, 1:30, 2:8, 3:2}
    → tasks_at_or_above_cap_count = 8 + 2 = 10, setting = 2.
    """
    chain_rows = [(0, 100), (1, 30), (2, 8), (3, 2)]
    client = await client_factory(
        [chain_rows], settings_overrides={"R1B_MAX_MUTATION_DEPTH": 2}
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/r1b/chain-depth-distribution")
    body = r.json()
    assert body["r1b_max_mutation_depth_setting"] == 2
    assert body["tasks_at_or_above_cap_count"] == 10
    # max_depth_observed still surfaces depth 3 (above the cap)
    assert body["max_depth_observed"] == 3


@pytest.mark.asyncio
async def test_chain_depth_cap_above_max_depth_returns_zero(client_factory):
    """Cap=5 over distribution that maxes at 3 → cap-firing count = 0."""
    chain_rows = [(0, 100), (1, 30), (2, 8), (3, 2)]
    client = await client_factory(
        [chain_rows], settings_overrides={"R1B_MAX_MUTATION_DEPTH": 5}
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/r1b/chain-depth-distribution")
    body = r.json()
    assert body["r1b_max_mutation_depth_setting"] == 5
    assert body["tasks_at_or_above_cap_count"] == 0


@pytest.mark.asyncio
async def test_chain_depth_requires_ops_token_when_env_set(client_factory):
    os.environ["OPS_API_TOKEN"] = "abc123"
    client = await client_factory([[]])
    async with client as ac:
        r = await ac.get("/api/v1/ops/r1b/chain-depth-distribution")
    assert r.status_code == 401
