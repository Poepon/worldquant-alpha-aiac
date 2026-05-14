"""Phase 2 B7 — HypothesisService integration tests.

Runs against the live Postgres dev DB (the Hypothesis schema uses JSONB
which aiosqlite can't render). Each test creates rows with task_id<0 in
the abandon_reason marker so the cleanup sweep can identify and drop
test data even if a test crashes mid-way.

Skipped automatically if Postgres isn't reachable.
"""
from __future__ import annotations

import os
import uuid
import pytest
import pytest_asyncio
from sqlalchemy import select, delete, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from backend.models import Alpha, Hypothesis, HypothesisStatus, HypothesisKind
from backend.services.hypothesis_service import (
    HypothesisService,
    HypothesisCreateData,
)


_PG_URL = os.environ.get(
    "TEST_PG_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5433/alpha_gpt",
)


def _pg_reachable() -> bool:
    import socket
    try:
        s = socket.create_connection(("localhost", 5433), timeout=1)
        s.close()
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_reachable(),
    reason="Postgres not reachable on localhost:5433 (B7 tests need JSONB)",
)


_TAG = f"_b7_test_{uuid.uuid4().hex[:8]}_"


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine(_PG_URL, echo=False)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
        # Cleanup any rows tagged by this test run
        try:
            await s.execute(
                delete(Hypothesis).where(Hypothesis.statement.like(f"{_TAG}%"))
            )
            await s.commit()
        except Exception:
            await s.rollback()
    await engine.dispose()


def _data(suffix: str = "", **overrides) -> HypothesisCreateData:
    """Tagged statement keeps cleanup easy."""
    base = dict(
        statement=f"{_TAG}{suffix}",
        rationale="test rationale",
        region="USA",
        universe="TOP3000",
        kind=HypothesisKind.INVESTMENT_THESIS.value,
        target_tier=1,
        expected_signal="momentum",
        confidence="medium",
        novelty="established",
        key_fields=["close", "volume"],
        suggested_operators=["ts_rank", "rank"],
        dataset_pool=["pv1"],
        experiment_variant="b7-test",
    )
    base.update(overrides)
    return HypothesisCreateData(**base)


# =============================================================================
# CRUD
# =============================================================================

@pytest.mark.asyncio
async def test_create_hypothesis_proposes_and_persists(session):
    svc = HypothesisService(session)
    h = await svc.create_hypothesis(_data("create-1"))
    await session.commit()

    assert h.id is not None
    assert h.status == HypothesisStatus.PROPOSED.value
    assert h.is_active is True
    assert h.alpha_count == 0
    assert h.pass_count == 0
    assert h.kind == HypothesisKind.INVESTMENT_THESIS.value
    assert h.region == "USA"
    assert h.dataset_pool == ["pv1"]


@pytest.mark.asyncio
async def test_get_by_id_returns_full_row(session):
    svc = HypothesisService(session)
    created = await svc.create_hypothesis(_data("get-by-id"))
    await session.commit()

    fetched = await svc.get_by_id(created.id)
    assert fetched is not None
    assert fetched.statement == created.statement
    assert fetched.experiment_variant == "b7-test"


@pytest.mark.asyncio
async def test_list_active_filters_by_region_kind_tier_variant(session):
    svc = HypothesisService(session)
    h_usa_t1 = await svc.create_hypothesis(_data("usa-t1", region="USA", target_tier=1))
    h_usa_t2 = await svc.create_hypothesis(_data(
        "usa-t2", region="USA", target_tier=2,
        kind=HypothesisKind.IMPROVEMENT_RULE.value,
    ))
    h_chn_t1 = await svc.create_hypothesis(_data("chn-t1", region="CHN", target_tier=1))
    await session.commit()

    usa_only = await svc.list_active(region="USA", limit=50)
    ids = {h.id for h in usa_only}
    assert h_usa_t1.id in ids
    assert h_usa_t2.id in ids
    assert h_chn_t1.id not in ids

    usa_t1_only = await svc.list_active(region="USA", target_tier=1, limit=50)
    ids = {h.id for h in usa_t1_only}
    assert h_usa_t1.id in ids
    assert h_usa_t2.id not in ids

    improvement_only = await svc.list_active(
        region="USA", kind=HypothesisKind.IMPROVEMENT_RULE.value, limit=50,
    )
    ids = {h.id for h in improvement_only}
    assert h_usa_t2.id in ids
    assert h_usa_t1.id not in ids


@pytest.mark.asyncio
async def test_list_active_excludes_abandoned_and_inactive(session):
    svc = HypothesisService(session)
    h_active = await svc.create_hypothesis(_data("active"))
    h_abandoned = await svc.create_hypothesis(_data("abandoned"))
    h_frozen = await svc.create_hypothesis(_data("frozen"))
    await session.commit()

    await svc.mark_abandoned(h_abandoned.id, reason="test abandon")
    await svc.set_active_flag(h_frozen.id, is_active=False, reason="regime test")
    await session.commit()

    listing = await svc.list_active(region="USA", limit=50)
    ids = {h.id for h in listing}
    assert h_active.id in ids
    assert h_abandoned.id not in ids  # ABANDONED status
    assert h_frozen.id not in ids     # is_active=False


# =============================================================================
# Lifecycle transitions
# =============================================================================

@pytest.mark.asyncio
async def test_mark_active_proposed_to_active(session):
    svc = HypothesisService(session)
    h = await svc.create_hypothesis(_data("active-trans"))
    await session.commit()
    assert h.status == HypothesisStatus.PROPOSED.value

    changed = await svc.mark_active(h.id)
    await session.commit()
    assert changed is True

    refreshed = await svc.get_by_id(h.id)
    assert refreshed.status == HypothesisStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_mark_active_idempotent(session):
    svc = HypothesisService(session)
    h = await svc.create_hypothesis(_data("idem-active"))
    await session.commit()

    assert await svc.mark_active(h.id) is True
    await session.commit()
    # Second call: already ACTIVE so nothing transitions
    assert await svc.mark_active(h.id) is False


@pytest.mark.asyncio
async def test_mark_promoted_from_proposed_or_active(session):
    svc = HypothesisService(session)
    # Direct PROPOSED → PROMOTED (skipping ACTIVE)
    h1 = await svc.create_hypothesis(_data("prop-promote"))
    await session.commit()
    assert await svc.mark_promoted(h1.id) is True
    await session.commit()
    assert (await svc.get_by_id(h1.id)).status == HypothesisStatus.PROMOTED.value

    # ACTIVE → PROMOTED
    h2 = await svc.create_hypothesis(_data("act-promote"))
    await session.commit()
    await svc.mark_active(h2.id)
    await session.commit()
    assert await svc.mark_promoted(h2.id) is True
    await session.commit()
    assert (await svc.get_by_id(h2.id)).status == HypothesisStatus.PROMOTED.value


@pytest.mark.asyncio
async def test_mark_promoted_does_not_overwrite_abandoned(session):
    svc = HypothesisService(session)
    h = await svc.create_hypothesis(_data("abandoned-no-promote"))
    await session.commit()
    await svc.mark_abandoned(h.id, reason="trial")
    await session.commit()
    assert await svc.mark_promoted(h.id) is False
    await session.commit()
    assert (await svc.get_by_id(h.id)).status == HypothesisStatus.ABANDONED.value


@pytest.mark.asyncio
async def test_mark_abandoned_records_reason(session):
    svc = HypothesisService(session)
    h = await svc.create_hypothesis(_data("abandon"))
    await session.commit()
    assert await svc.mark_abandoned(h.id, reason="3 rounds 0 PASS HYPOTHESIS-fail") is True
    await session.commit()
    refreshed = await svc.get_by_id(h.id)
    assert refreshed.status == HypothesisStatus.ABANDONED.value
    assert "3 rounds 0 PASS" in refreshed.abandon_reason


@pytest.mark.asyncio
async def test_mark_abandoned_requires_reason(session):
    svc = HypothesisService(session)
    h = await svc.create_hypothesis(_data("no-reason"))
    await session.commit()
    with pytest.raises(ValueError):
        await svc.mark_abandoned(h.id, reason="")


# V-27.B (2026-05-14): test_mark_superseded_* removed — mark_superseded
# and the G-refine loop it served were deleted (never fired in production).


@pytest.mark.asyncio
async def test_set_active_flag_does_not_change_status(session):
    """Regime-toggle: is_active=False but status stays PROPOSED/ACTIVE.
    Plan v5+ Final §简化冷冻 — status reflects lifecycle, is_active is
    orthogonal eligibility flag."""
    svc = HypothesisService(session)
    h = await svc.create_hypothesis(_data("flag-toggle"))
    await session.commit()
    await svc.mark_active(h.id)
    await session.commit()

    await svc.set_active_flag(h.id, is_active=False, reason="regime shift to bear")
    await session.commit()
    refreshed = await svc.get_by_id(h.id)
    assert refreshed.is_active is False
    assert refreshed.status == HypothesisStatus.ACTIVE.value  # NOT changed
    assert "regime-freeze" in (refreshed.abandon_reason or "")

    # Unfreeze
    await svc.set_active_flag(h.id, is_active=True, reason="regime back to bull")
    await session.commit()
    refreshed2 = await svc.get_by_id(h.id)
    assert refreshed2.is_active is True
    assert refreshed2.status == HypothesisStatus.ACTIVE.value


# =============================================================================
# Stats aggregation
# =============================================================================

@pytest.mark.asyncio
async def test_refresh_stats_zero_alphas(session):
    svc = HypothesisService(session)
    h = await svc.create_hypothesis(_data("stats-zero"))
    await session.commit()

    stats = await svc.refresh_stats(h.id)
    await session.commit()
    assert stats.alpha_count == 0
    assert stats.pass_count == 0
    assert stats.sharpe_avg is None
    assert stats.sharpe_max is None


@pytest.mark.asyncio
async def test_refresh_stats_aggregates_alpha_join(session):
    """Insert 4 alphas under one hypothesis (2 PASS, 1 PROV, 1 FAIL); stats
    should reflect 4 / 3 / mean / max."""
    svc = HypothesisService(session)
    h = await svc.create_hypothesis(_data("stats-agg"))
    await session.commit()

    # Need a real task_id and run_id. We'll inline mining_task / run rows.
    from backend.models import MiningTask, ExperimentRun
    task = MiningTask(
        task_name=f"{_TAG}stats-task",
        region="USA", universe="TOP3000",
        dataset_strategy="AUTO", agent_mode="AUTONOMOUS_TIER1",
        status="RUNNING", daily_goal=4, max_iterations=2,
    )
    session.add(task)
    await session.flush()
    run = ExperimentRun(task_id=task.id, status="RUNNING")
    session.add(run)
    await session.flush()

    rows = [
        Alpha(task_id=task.id, run_id=run.id, alpha_id=f"_b7_{uuid.uuid4().hex[:8]}",
              expression="e1", region="USA", universe="TOP3000",
              quality_status="PASS", is_sharpe=2.0, hypothesis_id=h.id, factor_tier=1),
        Alpha(task_id=task.id, run_id=run.id, alpha_id=f"_b7_{uuid.uuid4().hex[:8]}",
              expression="e2", region="USA", universe="TOP3000",
              quality_status="PASS", is_sharpe=1.5, hypothesis_id=h.id, factor_tier=1),
        Alpha(task_id=task.id, run_id=run.id, alpha_id=f"_b7_{uuid.uuid4().hex[:8]}",
              expression="e3", region="USA", universe="TOP3000",
              quality_status="PASS_PROVISIONAL", is_sharpe=1.0, hypothesis_id=h.id, factor_tier=1),
        Alpha(task_id=task.id, run_id=run.id, alpha_id=f"_b7_{uuid.uuid4().hex[:8]}",
              expression="e4", region="USA", universe="TOP3000",
              quality_status="FAIL", is_sharpe=-0.3, hypothesis_id=h.id, factor_tier=1),
    ]
    for r in rows:
        session.add(r)
    await session.commit()

    try:
        stats = await svc.refresh_stats(h.id)
        await session.commit()
        assert stats.alpha_count == 4
        assert stats.pass_count == 3  # 2 PASS + 1 PROV
        assert stats.sharpe_avg == pytest.approx((2.0 + 1.5 + 1.0 + -0.3) / 4, abs=1e-6)
        assert stats.sharpe_max == pytest.approx(2.0)

        # Denormalized cols updated on the row itself
        h_refreshed = await svc.get_by_id(h.id)
        assert h_refreshed.alpha_count == 4
        assert h_refreshed.pass_count == 3
        assert h_refreshed.sharpe_max == pytest.approx(2.0)
    finally:
        # Cleanup
        await session.execute(delete(Alpha).where(Alpha.hypothesis_id == h.id))
        await session.execute(delete(ExperimentRun).where(ExperimentRun.id == run.id))
        await session.execute(text("DELETE FROM mining_tasks WHERE id = :i"), {"i": task.id})
        await session.commit()


@pytest.mark.asyncio
async def test_refresh_stats_counts_alpha_failures_v26_13(session):
    """V-26.13: alpha_count must include alpha_failures rows so a hypothesis
    that hit only validation / sim errors still advances PROPOSED→ACTIVE."""
    from backend.models import MiningTask, ExperimentRun, AlphaFailure
    svc = HypothesisService(session)
    h = await svc.create_hypothesis(_data("v26-13-fails-only"))
    await session.commit()

    task = MiningTask(
        task_name=f"{_TAG}v2613-task",
        region="USA", universe="TOP3000",
        dataset_strategy="AUTO", agent_mode="AUTONOMOUS_TIER1",
        status="RUNNING", daily_goal=4, max_iterations=2,
    )
    session.add(task)
    await session.flush()
    run = ExperimentRun(task_id=task.id, status="RUNNING")
    session.add(run)
    await session.flush()

    # 3 failures, no Alpha rows at all
    fails = [
        AlphaFailure(
            task_id=task.id, run_id=run.id,
            expression=f"bad_expr_{i}", error_type="SYNTAX_ERROR",
            error_message="parse fail", hypothesis_id=h.id,
        )
        for i in range(3)
    ]
    for f in fails:
        session.add(f)
    await session.commit()

    try:
        stats = await svc.refresh_stats(h.id)
        await session.commit()
        # Pre-V-26.13 this asserted alpha_count==0 (FAIL-only path
        # invisible). Post-fix: 3 attempts visible.
        assert stats.alpha_count == 3
        assert stats.fail_count == 3
        assert stats.pass_count == 0
        # And auto_activate_if_eligible should now fire on the FAIL-only path
        activated = await svc.auto_activate_if_eligible(h.id)
        await session.commit()
        assert activated is True
        h_after = await svc.get_by_id(h.id)
        assert h_after.status == HypothesisStatus.ACTIVE.value
    finally:
        await session.execute(delete(AlphaFailure).where(AlphaFailure.hypothesis_id == h.id))
        await session.execute(delete(ExperimentRun).where(ExperimentRun.id == run.id))
        await session.execute(text("DELETE FROM mining_tasks WHERE id = :i"), {"i": task.id})
        await session.commit()


@pytest.mark.asyncio
async def test_refresh_stats_sums_alpha_and_failures_v26_13(session):
    """V-26.13: when both Alpha and AlphaFailure rows exist, alpha_count
    sums both. pass_count and sharpe_* still come from Alpha only."""
    from backend.models import MiningTask, ExperimentRun, AlphaFailure
    svc = HypothesisService(session)
    h = await svc.create_hypothesis(_data("v26-13-mixed"))
    await session.commit()

    task = MiningTask(
        task_name=f"{_TAG}v2613-mix-task",
        region="USA", universe="TOP3000",
        dataset_strategy="AUTO", agent_mode="AUTONOMOUS_TIER1",
        status="RUNNING", daily_goal=4, max_iterations=2,
    )
    session.add(task)
    await session.flush()
    run = ExperimentRun(task_id=task.id, status="RUNNING")
    session.add(run)
    await session.flush()

    # 2 PASS alphas + 5 failure rows = 7 total attempts
    alpha_rows = [
        Alpha(task_id=task.id, run_id=run.id, alpha_id=f"_b7_{uuid.uuid4().hex[:8]}",
              expression=f"e_pass_{i}", region="USA", universe="TOP3000",
              quality_status="PASS", is_sharpe=1.5, hypothesis_id=h.id, factor_tier=1)
        for i in range(2)
    ]
    fail_rows = [
        AlphaFailure(
            task_id=task.id, run_id=run.id,
            expression=f"e_fail_{i}", error_type="TIMEOUT",
            error_message="brain timeout", hypothesis_id=h.id,
        )
        for i in range(5)
    ]
    for r in alpha_rows + fail_rows:
        session.add(r)
    await session.commit()

    try:
        stats = await svc.refresh_stats(h.id)
        await session.commit()
        assert stats.alpha_count == 7  # 2 + 5
        assert stats.fail_count == 5
        assert stats.pass_count == 2
        assert stats.sharpe_max == pytest.approx(1.5)
    finally:
        await session.execute(delete(Alpha).where(Alpha.hypothesis_id == h.id))
        await session.execute(delete(AlphaFailure).where(AlphaFailure.hypothesis_id == h.id))
        await session.execute(delete(ExperimentRun).where(ExperimentRun.id == run.id))
        await session.execute(text("DELETE FROM mining_tasks WHERE id = :i"), {"i": task.id})
        await session.commit()


@pytest.mark.asyncio
async def test_pass_rate_returns_none_when_no_alphas(session):
    svc = HypothesisService(session)
    h = await svc.create_hypothesis(_data("pr-none"))
    await session.commit()
    assert await svc.pass_rate(h.id) is None


@pytest.mark.asyncio
async def test_rounds_active_counts_distinct_round_buckets(session):
    """Phase 3 prep: rounds_active counts distinct minute-buckets of alpha
    created_at as proxy for "how many rounds did this hypothesis live"."""
    import asyncio
    from backend.models import Alpha, MiningTask, ExperimentRun
    from sqlalchemy import text as _text

    svc = HypothesisService(session)
    h = await svc.create_hypothesis(_data("rounds-active-test"))
    await session.commit()

    # 0 alphas → rounds_active = 0
    assert await svc.rounds_active(h.id) == 0

    # Need a real task / run for FK
    task = MiningTask(
        task_name=f"{_TAG}rounds-task", region="USA", universe="TOP3000",
        dataset_strategy="AUTO", agent_mode="AUTONOMOUS_TIER1",
        status="RUNNING", daily_goal=4, max_iterations=2,
    )
    session.add(task)
    await session.flush()
    run = ExperimentRun(task_id=task.id, status="RUNNING")
    session.add(run)
    await session.flush()

    try:
        # Insert 3 alphas, 2 created in same minute-bucket, 1 in another.
        # Manipulate created_at via SQL to force buckets.
        # alphas.created_at is TIMESTAMP WITHOUT TIME ZONE — pass naive
        from datetime import datetime
        t1 = datetime(2026, 5, 6, 12, 0, 30)
        t2 = datetime(2026, 5, 6, 12, 0, 45)  # same minute as t1
        t3 = datetime(2026, 5, 6, 12, 5, 10)  # different minute

        for i, ts in enumerate([t1, t2, t3]):
            session.add(Alpha(
                task_id=task.id, run_id=run.id,
                alpha_id=f"_b7_{uuid.uuid4().hex[:13]}",
                expression=f"e{i}", region="USA", universe="TOP3000",
                quality_status="PASS", is_sharpe=1.0, factor_tier=1,
                hypothesis_id=h.id,
            ))
        await session.flush()
        # Override created_at via raw UPDATE (server_default ate our timestamp)
        await session.execute(_text(
            "UPDATE alphas SET created_at = :t1 WHERE alpha_id LIKE '_b7_%' "
            "AND hypothesis_id = :hid AND expression = 'e0'"
        ), {"t1": t1, "hid": h.id})
        await session.execute(_text(
            "UPDATE alphas SET created_at = :t2 WHERE alpha_id LIKE '_b7_%' "
            "AND hypothesis_id = :hid AND expression = 'e1'"
        ), {"t2": t2, "hid": h.id})
        await session.execute(_text(
            "UPDATE alphas SET created_at = :t3 WHERE alpha_id LIKE '_b7_%' "
            "AND hypothesis_id = :hid AND expression = 'e2'"
        ), {"t3": t3, "hid": h.id})
        await session.commit()

        # 3 alphas, 2 in bucket t1==t2 (12:00) and 1 in t3 (12:05) → 2 distinct buckets
        assert await svc.rounds_active(h.id) == 2
    finally:
        await session.execute(_text("DELETE FROM alphas WHERE hypothesis_id = :hid"),
                              {"hid": h.id})
        await session.execute(_text("DELETE FROM experiment_runs WHERE id = :i"),
                              {"i": run.id})
        await session.execute(_text("DELETE FROM mining_tasks WHERE id = :i"),
                              {"i": task.id})
        await session.commit()


@pytest.mark.asyncio
async def test_auto_promote_if_eligible_no_pass_means_no_promote(session):
    svc = HypothesisService(session)
    h = await svc.create_hypothesis(_data("auto-prom-none"))
    await session.commit()
    assert await svc.auto_promote_if_eligible(h.id) is False
    await session.commit()
    assert (await svc.get_by_id(h.id)).status == HypothesisStatus.PROPOSED.value
