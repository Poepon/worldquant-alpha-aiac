"""Manual "optimize-from-blueprint" path — service validation + manual stamp.

Covers the 2026-06-03 feature wiring the reserved ``trigger_source="manual"``
optimization cycle to a user action (``POST /alphas/{id}/optimize``):

  - OptimizationService.run_one_cycle stamps ``trigger_source="manual"`` on the
    optimization_runs row (the value was schema-reserved but never written).
  - AlphaService.prepare_blueprint_optimization validates the alpha, clamps the
    sim budget, previews the SettingsSweep variant tags, and refuses when a
    recent cycle for the same alpha is still in flight.

These run on the in-memory aiosqlite ``db_session`` fixture (real ORM, no
mocks) per the repo's ``feedback_orm_constructor_real_test`` rule.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List

import pytest

from backend.models import Alpha, OptimizationRun
from backend.services.alpha_service import AlphaService
from backend.services.optimization import OptimizationService
from backend.services.optimization.generators.settings_sweep import (
    SettingsSweepGenerator,
)
from backend.services.optimization.persister import Persister
from backend.services.optimization.protocols import Variant, VariantSimResult
from backend.services.optimization.repository import (
    OptimizationRunRepositoryImpl,
)
from backend.services.optimization.robustness import RobustnessFilter
from backend.services.optimization.submit_policy import StageASubmitPolicy
from backend.services.optimization.winner_selector import WinnerSelector


_PARENT_EXPR = (
    "group_neutralize(rank(ts_zscore(divide(cashflow_op, "
    "enterprise_value), 60)), industry)"
)


class _FakeSimulator:
    """All variants clear delay-1 band (sharpe 2.5) → every variant a winner."""

    async def run_batch(
        self, variants: List[Variant], budget: int
    ) -> List[VariantSimResult]:
        out: List[VariantSimResult] = []
        for i, v in enumerate(variants[:budget]):
            out.append(VariantSimResult(
                variant=v,
                sim_response={"is": {"sharpe": 2.5, "fitness": 1.5}},
                sharpe=2.5, fitness=1.5, turnover=0.25, margin=0.001,
                subuniv=0.9, brain_alpha_id=f"mock-manual-{i}",
                checks_passed=True,
            ))
        return out


async def _seed_parent(db_session, *, expression: str = _PARENT_EXPR) -> Alpha:
    parent = Alpha(
        alpha_id="parent-manual",
        expression=expression,
        region="USA",
        universe="TOP3000",
        delay=1,
        truncation=0.08,
    )
    db_session.add(parent)
    await db_session.flush()
    parent.parent_alpha_family_id = parent.id
    await db_session.flush()
    return parent


def _build_service(db_session, *, simulator) -> OptimizationService:
    repo = OptimizationRunRepositoryImpl(db_session)
    return OptimizationService(
        generator=SettingsSweepGenerator(),
        simulator=simulator,
        winner_selector=WinnerSelector(),
        persister=Persister(db_session, corr_service=None, repository=repo),
        submit_policy=StageASubmitPolicy(),
        repository=repo,
    )


# ---------------------------------------------------------------------------
# run_one_cycle stamps trigger_source="manual"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_trigger_source_stamped(db_session):
    parent = await _seed_parent(db_session)
    svc = _build_service(db_session, simulator=_FakeSimulator())

    summary = await svc.run_one_cycle(
        parent, trigger_source="manual", budget=16,
    )

    run = await db_session.get(OptimizationRun, summary["opt_run_id"])
    assert run.trigger_source == "manual"
    assert run.parent_alpha_id == parent.id
    assert run.generator_name == "settings_sweep"
    assert run.cycle_finished_at is not None
    assert run.error is None
    # Stage A never auto-submits, even on the manual path.
    assert summary["n_submitted"] == 0
    assert run.n_submitted == 0


# ---------------------------------------------------------------------------
# AlphaService.prepare_blueprint_optimization
# ---------------------------------------------------------------------------


class _SpikeSimulator:
    """variant 0 is a lone spike (sharpe 2.5, all siblings 0.3) → clears the
    band but fails the plateau gate."""

    async def run_batch(self, variants, budget):
        out = []
        for i, v in enumerate(variants[:budget]):
            sh = 2.5 if i == 0 else 0.3
            out.append(VariantSimResult(
                variant=v, sim_response={"is": {"sharpe": sh}},
                sharpe=sh, fitness=2.0, turnover=0.25, margin=0.001, subuniv=0.9,
                brain_alpha_id=f"spike-{i}", checks_passed=True,
            ))
        return out


@pytest.mark.asyncio
async def test_robustness_filter_rejects_spike_and_stamps_cycle_metadata(db_session):
    """止血: a lone-spike winner is rejected by the RobustnessFilter and the
    rejection is persisted to optimization_runs.cycle_metadata."""
    from backend.models import OptimizationRun

    parent = await _seed_parent(db_session)  # delay=1
    repo = OptimizationRunRepositoryImpl(db_session)
    svc = OptimizationService(
        generator=SettingsSweepGenerator(),
        simulator=_SpikeSimulator(),
        winner_selector=WinnerSelector(),
        persister=Persister(db_session, corr_service=None, repository=repo),
        submit_policy=StageASubmitPolicy(),
        repository=repo,
        robustness=RobustnessFilter(),
    )
    summary = await svc.run_one_cycle(parent, trigger_source="manual", budget=16)

    # The single band-clearing winner (the spike) was deflated away.
    assert summary["n_winners"] == 0
    assert summary["n_robustness_rejected"] >= 1
    assert summary["persisted_pks"] == []

    run = await db_session.get(OptimizationRun, summary["opt_run_id"])
    assert run.n_winners == 0
    rejected = (run.cycle_metadata or {}).get("robustness_rejected")
    assert rejected and any("lone_spike" in r["reason"] for r in rejected)


@pytest.mark.asyncio
async def test_prepare_not_found(db_session):
    svc = AlphaService(db_session)
    res = await svc.prepare_blueprint_optimization(999999)
    assert res["ok"] is False
    assert res["code"] == "not_found"


@pytest.mark.asyncio
async def test_prepare_no_expression(db_session):
    parent = await _seed_parent(db_session, expression="   ")
    svc = AlphaService(db_session)
    res = await svc.prepare_blueprint_optimization(parent.id)
    assert res["ok"] is False
    assert res["code"] == "no_expression"


@pytest.mark.asyncio
async def test_prepare_ok_budget_clamp_and_preview(db_session):
    parent = await _seed_parent(db_session)
    svc = AlphaService(db_session)

    # Default (None) → OPT_MANUAL_SIM_BUDGET (16).
    res = await svc.prepare_blueprint_optimization(parent.id)
    assert res["ok"] is True
    assert res["budget"] == 16
    # ts_zscore(..., 60) has a window → full 10-variant grid.
    assert res["n_variants"] == 10
    assert len(res["variant_tags"]) == 10
    assert all(t.startswith("decay=") for t in res["variant_tags"])

    # Over-max → clamped to OPT_MANUAL_SIM_BUDGET_MAX (30).
    res_hi = await svc.prepare_blueprint_optimization(parent.id, budget_override=999)
    assert res_hi["budget"] == 30

    # Zero / negative → clamped up to 1.
    res_lo = await svc.prepare_blueprint_optimization(parent.id, budget_override=0)
    assert res_lo["budget"] == 1


@pytest.mark.asyncio
async def test_prepare_in_flight_blocks_then_clears(db_session):
    parent = await _seed_parent(db_session)
    svc = AlphaService(db_session)

    # A recent OPEN cycle (no finished_at, no error) blocks a new manual run.
    open_run = OptimizationRun(
        parent_alpha_id=parent.id,
        generator_name="settings_sweep",
        trigger_source="manual",
        sim_budget_granted=16,
        cycle_started_at=datetime.utcnow(),
    )
    db_session.add(open_run)
    await db_session.flush()

    blocked = await svc.prepare_blueprint_optimization(parent.id)
    assert blocked["ok"] is False
    assert blocked["code"] == "in_flight"
    assert blocked["opt_run_id"] == open_run.id

    # Once it finishes, a new manual run is allowed again.
    open_run.cycle_finished_at = datetime.utcnow()
    await db_session.flush()

    cleared = await svc.prepare_blueprint_optimization(parent.id)
    assert cleared["ok"] is True


@pytest.mark.asyncio
async def test_prepare_stale_open_cycle_does_not_block(db_session):
    """An orphaned open cycle older than the guard window must not wedge
    re-triggers forever (crashed-worker safety)."""
    parent = await _seed_parent(db_session)
    svc = AlphaService(db_session)

    stale = OptimizationRun(
        parent_alpha_id=parent.id,
        generator_name="settings_sweep",
        trigger_source="manual",
        sim_budget_granted=16,
        cycle_started_at=datetime.utcnow() - timedelta(hours=3),
    )
    db_session.add(stale)
    await db_session.flush()

    res = await svc.prepare_blueprint_optimization(parent.id)
    assert res["ok"] is True
