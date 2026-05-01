"""Tests for backend/agents/graph/nodes/tier_seed.py — node_tier_seed_load.

Coverage targets (plan §6.1):
- candidates query selects N-1 PASS alphas in target region
- BRAIN refresh updates is_sharpe/fitness/turnover and metrics.checks
- demoted alphas (now below tier threshold) trigger
  apply_quality_status_change → transition row written
- survivors sorted by sharpe desc, written to state.tier_seeds
- insufficient seeds (< MIN_TIER_SEED_COUNT) sets should_stop=True
- single BRAIN failure doesn't kill the batch

Uses the live Postgres dev DB rather than conftest's in-memory SQLite because
several Alpha columns (settings, metrics, fields_used) are JSONB which SQLite
can't compile. Test rows are tagged with REGION='ZTEST' so we can clean them
up unconditionally without touching real data.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.graph.nodes.tier_seed import _meets_pass, node_tier_seed_load
from backend.agents.graph.state import MiningState
from backend.agents.graph.tier_thresholds import get_tier_thresholds
from backend.database import AsyncSessionLocal
from backend.models import Alpha, AlphaStatusTransition
from backend.services.alpha_service import AlphaService


# Sentinel region — guarantees no overlap with real data. Cleanup deletes
# everything in this region to keep the table tidy across runs.
TEST_REGION = "ZTEST"


def _fresh_payload(sharpe: float, fitness: float, turnover: float) -> dict:
    """Mock BRAIN GET /alphas/{id} response shape."""
    return {
        "id": "x",
        "is": {"sharpe": sharpe, "fitness": fitness, "turnover": turnover},
        "checks": [],
    }


@pytest_asyncio.fixture
async def pg_session():
    """Open a live Postgres session and clean up test artifacts at the end.

    The teardown ALWAYS runs (even on test failure) so leftover ZTEST rows
    don't accumulate.
    """
    async with AsyncSessionLocal() as session:
        # Clean any leftovers from a prior failed run
        await _cleanup(session)
        await session.commit()
        try:
            yield session
        finally:
            await _cleanup(session)
            await session.commit()


async def _cleanup(session: AsyncSession) -> None:
    """Remove all alphas + transitions associated with TEST_REGION."""
    test_alpha_ids_q = select(Alpha.id).where(Alpha.region == TEST_REGION)
    test_ids = [r for (r,) in (await session.execute(test_alpha_ids_q)).all()]
    if test_ids:
        await session.execute(
            delete(AlphaStatusTransition).where(
                AlphaStatusTransition.alpha_id.in_(test_ids)
            )
        )
        await session.execute(delete(Alpha).where(Alpha.id.in_(test_ids)))


@pytest_asyncio.fixture
async def t1_pass_alphas(pg_session):
    """Insert 6 T1 PASS alphas in TEST_REGION with varying sharpe values."""
    alphas = []
    specs = [
        ("ztest-a1", 1.5, 0.9, 0.30),
        ("ztest-a2", 1.2, 0.8, 0.40),
        ("ztest-a3", 1.0, 0.7, 0.20),
        ("ztest-a4", 0.95, 0.6, 0.50),
        ("ztest-a5", 0.85, 0.55, 0.35),
        ("ztest-a6", 2.0, 1.2, 0.25),  # highest sharpe
    ]
    for brain_id, sharpe, fitness, turnover in specs:
        a = Alpha(
            alpha_id=brain_id,
            expression=f"ts_rank(close_{brain_id}, 20)",
            expression_hash=f"hash_{brain_id}",
            region=TEST_REGION,
            universe="TOP3000",
            status="simulated",
            quality_status="PASS",
            human_feedback="NONE",
            factor_tier=1,
            is_sharpe=sharpe,
            is_fitness=fitness,
            is_turnover=turnover,
        )
        pg_session.add(a)
        alphas.append(a)
    await pg_session.commit()
    for a in alphas:
        await pg_session.refresh(a)
    return alphas


@pytest.mark.asyncio
async def test_seed_load_happy_path(pg_session, t1_pass_alphas):
    """All seeds remain PASS after refresh — sorted desc by sharpe, populated."""
    state = MiningState(task_id=1, region=TEST_REGION, factor_tier=2, num_alphas_target=4)

    # BRAIN returns same metrics → no demotion. Build the lookup table up-front
    # so the AsyncMock side_effect doesn't run a generator (avoids StopIteration
    # leaking through pytest-asyncio).
    metrics_by_id = {
        a.alpha_id: (a.is_sharpe, a.is_fitness, a.is_turnover)
        for a in t1_pass_alphas
    }
    brain = AsyncMock()
    brain.get_alpha = AsyncMock(
        side_effect=lambda aid: _fresh_payload(*metrics_by_id[aid])
    )

    config = {
        "configurable": {
            "db_session": pg_session,
            "brain_adapter": brain,
            "alpha_service": AlphaService(pg_session),
        }
    }
    result = await node_tier_seed_load(state, config)

    assert "tier_seeds" in result
    seeds = result["tier_seeds"]
    assert len(seeds) == 6
    # Top seed has the highest sharpe (ztest-a6, sharpe=2.0)
    assert seeds[0]["brain_alpha_id"] == "ztest-a6"
    # Sorted desc
    sharpes = [s["metrics"]["sharpe"] for s in seeds]
    assert sharpes == sorted(sharpes, reverse=True)
    # No early stop
    assert result.get("should_stop") is not True


@pytest.mark.asyncio
async def test_seed_load_demotes_drifters(pg_session, t1_pass_alphas, monkeypatch):
    """A seed whose refresh shows sharpe below T1 bar gets demoted; transition written.

    Forces TIER_SEED_LOAD_REFRESH_VIA_BRAIN=True for this test (the production
    default is False per the P0 finding, but the demote logic itself still
    needs to be correct when refresh is on).
    """
    from backend.config import settings
    monkeypatch.setattr(settings, "TIER_SEED_LOAD_REFRESH_VIA_BRAIN", True)

    state = MiningState(task_id=1, region=TEST_REGION, factor_tier=2, num_alphas_target=4)

    # ztest-a1 will show drifted sharpe (0.5 — below T1's 0.8 PASS bar);
    # everyone else stays PASS.
    metrics_by_id = {
        a.alpha_id: (a.is_sharpe, a.is_fitness, a.is_turnover)
        for a in t1_pass_alphas
    }

    def fresh_for(aid):
        if aid == "ztest-a1":
            return _fresh_payload(sharpe=0.5, fitness=0.4, turnover=0.30)
        return _fresh_payload(*metrics_by_id[aid])

    brain = AsyncMock()
    brain.get_alpha = AsyncMock(side_effect=fresh_for)

    config = {
        "configurable": {
            "db_session": pg_session,
            "brain_adapter": brain,
            "alpha_service": AlphaService(pg_session),
        }
    }
    result = await node_tier_seed_load(state, config)

    # 5 survivors, ztest-a1 demoted out
    assert len(result["tier_seeds"]) == 5
    survivor_ids = {s["brain_alpha_id"] for s in result["tier_seeds"]}
    assert "ztest-a1" not in survivor_ids

    # transition row written for ztest-a1 (by db id, not brain alpha_id)
    a1_id = next(a.id for a in t1_pass_alphas if a.alpha_id == "ztest-a1")
    transitions_q = select(AlphaStatusTransition).where(
        AlphaStatusTransition.alpha_id == a1_id
    )
    rows = (await pg_session.execute(transitions_q)).scalars().all()
    assert len(rows) == 1
    assert rows[0].old_status == "PASS"
    assert rows[0].new_status == "PASS_PROVISIONAL"
    assert rows[0].source == "tier_seed_refresh"
    assert "drifted" in (rows[0].reason or "").lower()


@pytest.mark.asyncio
async def test_seed_load_insufficient_seeds_triggers_stop(pg_session):
    """When fewer than MIN_TIER_SEED_COUNT survivors remain, set should_stop=True."""
    # Only 2 PASS T1 alphas — below default MIN_TIER_SEED_COUNT=5
    for i in range(2):
        a = Alpha(
            alpha_id=f"ztest-few-{i}",
            expression=f"ts_rank(close_{i}, 20)",
            expression_hash=f"hash_few_{i}",
            region=TEST_REGION,
            universe="TOP3000",
            quality_status="PASS",
            factor_tier=1,
            is_sharpe=1.5,
            is_fitness=0.9,
            is_turnover=0.3,
        )
        pg_session.add(a)
    await pg_session.commit()

    state = MiningState(task_id=1, region=TEST_REGION, factor_tier=2, num_alphas_target=4)
    brain = AsyncMock()
    brain.get_alpha = AsyncMock(return_value=_fresh_payload(1.5, 0.9, 0.3))

    config = {
        "configurable": {
            "db_session": pg_session,
            "brain_adapter": brain,
            "alpha_service": AlphaService(pg_session),
        }
    }
    result = await node_tier_seed_load(state, config)

    assert result.get("should_stop") is True
    assert "insufficient_fresh_seeds" in (result.get("early_stop_reason") or "")


@pytest.mark.asyncio
async def test_seed_load_brain_failure_per_alpha_doesnt_kill_batch(
    pg_session, t1_pass_alphas
):
    """Single BRAIN GET failure → that alpha keeps cached metrics; others succeed."""
    state = MiningState(task_id=1, region=TEST_REGION, factor_tier=2, num_alphas_target=4)

    metrics_by_id = {
        a.alpha_id: (a.is_sharpe, a.is_fitness, a.is_turnover)
        for a in t1_pass_alphas
    }

    def fresh_for(aid):
        if aid == "ztest-a3":
            raise RuntimeError("simulated BRAIN timeout")
        return _fresh_payload(*metrics_by_id[aid])

    brain = AsyncMock()
    brain.get_alpha = AsyncMock(side_effect=fresh_for)

    config = {
        "configurable": {
            "db_session": pg_session,
            "brain_adapter": brain,
            "alpha_service": AlphaService(pg_session),
        }
    }
    result = await node_tier_seed_load(state, config)

    # All 6 still survive (ztest-a3 used cached metrics which met PASS bar)
    assert len(result["tier_seeds"]) == 6
    a3_seeds = [s for s in result["tier_seeds"] if s["brain_alpha_id"] == "ztest-a3"]
    assert len(a3_seeds) == 1
    # ztest-a3 cached metrics preserved (sharpe=1.0 from spec)
    assert a3_seeds[0]["metrics"]["sharpe"] == 1.0


@pytest.mark.asyncio
async def test_seed_load_no_predecessor_alphas(pg_session):
    """No T1 PASS alphas in region → empty seeds + early stop."""
    state = MiningState(task_id=1, region=TEST_REGION, factor_tier=2, num_alphas_target=4)
    brain = AsyncMock()
    config = {
        "configurable": {
            "db_session": pg_session,
            "brain_adapter": brain,
            "alpha_service": AlphaService(pg_session),
        }
    }
    result = await node_tier_seed_load(state, config)

    assert result["tier_seeds"] == []
    assert result.get("should_stop") is True
    assert "0 T1 PASS" in (result.get("early_stop_reason") or "") or \
           "insufficient" in (result.get("early_stop_reason") or "").lower()


# Sanity tests for the helper

class TestMeetsPass:
    def test_t1_pass(self):
        a = Alpha(is_sharpe=1.0, is_fitness=0.7, is_turnover=0.3)
        assert _meets_pass(a, get_tier_thresholds(1)) is True

    def test_t1_fail_sharpe(self):
        a = Alpha(is_sharpe=0.5, is_fitness=0.7, is_turnover=0.3)
        assert _meets_pass(a, get_tier_thresholds(1)) is False

    def test_t2_fail_turnover(self):
        # T2 ceiling is 0.55, this is over
        a = Alpha(is_sharpe=1.5, is_fitness=1.0, is_turnover=0.65)
        assert _meets_pass(a, get_tier_thresholds(2)) is False

    def test_none_metrics_treated_as_zero(self):
        a = Alpha(is_sharpe=None, is_fitness=None, is_turnover=None)
        assert _meets_pass(a, get_tier_thresholds(1)) is False
