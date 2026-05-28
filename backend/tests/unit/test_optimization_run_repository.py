"""OptimizationRunRepositoryImpl lifecycle — open → record_* → finish.

Uses the in-memory aiosqlite fixture so the real model + real SQL chain
runs end-to-end (per feedback_orm_constructor_real_test — mock-only tests
hide schema-name typos and column drift).
"""
from __future__ import annotations

import pytest

from backend.models import Alpha, OptimizationRun
from backend.services.optimization.repository import (
    OptimizationRunRepositoryImpl,
)


async def _seed_parent_alpha(db_session) -> int:
    """Helper — minimal valid Alpha row for FK satisfaction."""
    a = Alpha(
        alpha_id="parent-1",
        expression="dummy",
        region="USA",
        universe="TOP3000",
    )
    db_session.add(a)
    await db_session.flush()
    return int(a.id)


@pytest.mark.asyncio
async def test_open_cycle_returns_id_and_persists_row(db_session):
    parent_id = await _seed_parent_alpha(db_session)
    repo = OptimizationRunRepositoryImpl(db_session)
    opt_run_id = await repo.open_cycle(
        parent_alpha_id=parent_id,
        generator_name="settings_sweep",
        trigger_source="beat",
        sim_budget_granted=100,
    )
    assert isinstance(opt_run_id, int) and opt_run_id > 0

    row = await db_session.get(OptimizationRun, opt_run_id)
    assert row is not None
    assert row.parent_alpha_id == parent_id
    assert row.generator_name == "settings_sweep"
    assert row.trigger_source == "beat"
    assert row.sim_budget_granted == 100
    assert row.n_variants == 0
    assert row.n_winners == 0
    assert row.n_submitted == 0
    assert row.cycle_started_at is not None
    assert row.cycle_finished_at is None
    assert row.error is None


@pytest.mark.asyncio
async def test_record_persist_updates_counters(db_session):
    parent_id = await _seed_parent_alpha(db_session)
    repo = OptimizationRunRepositoryImpl(db_session)
    opt_run_id = await repo.open_cycle(
        parent_alpha_id=parent_id,
        generator_name="settings_sweep",
        trigger_source="beat",
        sim_budget_granted=100,
    )
    await repo.record_persist(
        opt_run_id=opt_run_id, n_variants=10, n_winners=3, sim_spent=10
    )
    row = await db_session.get(OptimizationRun, opt_run_id)
    assert row.n_variants == 10
    assert row.n_winners == 3
    assert row.sim_budget_used == 10


@pytest.mark.asyncio
async def test_record_submit_then_finish(db_session):
    parent_id = await _seed_parent_alpha(db_session)
    repo = OptimizationRunRepositoryImpl(db_session)
    opt_run_id = await repo.open_cycle(
        parent_alpha_id=parent_id,
        generator_name="settings_sweep",
        trigger_source="beat",
        sim_budget_granted=100,
    )
    await repo.record_submit(opt_run_id=opt_run_id, n_submitted=0)
    await repo.finish_cycle(opt_run_id=opt_run_id)

    row = await db_session.get(OptimizationRun, opt_run_id)
    assert row.n_submitted == 0
    assert row.cycle_finished_at is not None
    assert row.error is None


@pytest.mark.asyncio
async def test_finish_cycle_with_error_stamps_field(db_session):
    parent_id = await _seed_parent_alpha(db_session)
    repo = OptimizationRunRepositoryImpl(db_session)
    opt_run_id = await repo.open_cycle(
        parent_alpha_id=parent_id,
        generator_name="settings_sweep",
        trigger_source="beat",
        sim_budget_granted=100,
    )
    await repo.finish_cycle(opt_run_id=opt_run_id, error="brain timeout")
    row = await db_session.get(OptimizationRun, opt_run_id)
    assert row.error == "brain timeout"
    assert row.cycle_finished_at is not None


@pytest.mark.asyncio
async def test_unknown_opt_run_id_raises(db_session):
    repo = OptimizationRunRepositoryImpl(db_session)
    with pytest.raises(LookupError):
        await repo.record_persist(
            opt_run_id=999999, n_variants=1, n_winners=1, sim_spent=1
        )
