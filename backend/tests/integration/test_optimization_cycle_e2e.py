"""End-to-end OptimizationService cycle on aiosqlite + in-memory fake BRAIN.

Wires the full Stage A pipeline (SettingsSweepGenerator → fake Simulator →
WinnerSelector → Persister → StageASubmitPolicy → OptimizationRunRepository)
and asserts:

  - optimization_runs row goes through open → record_persist → record_submit
    → finish_cycle in sequence (no half-finished row left visible)
  - alphas rows get optimization_run_id + parent_alpha_family_id + origin
    stamp wired
  - StageASubmitPolicy keeps n_submitted = 0 (NEVER auto-submit invariant)
  - returned summary dict shape matches what beat/ops endpoints consume
  - error path: a simulator that raises mid-batch → finish_cycle stamps
    error field, exception propagates

These cover the multi-collaborator surface that unit tests don't (each
collab is unit-tested in isolation already; e2e catches wire-up bugs).
"""
from __future__ import annotations

from typing import List

import pytest

from backend.models import Alpha, OptimizationRun
from backend.services.optimization import OptimizationService
from backend.services.optimization.generators.settings_sweep import (
    SettingsSweepGenerator,
)
from backend.services.optimization.persister import Persister
from backend.services.optimization.protocols import (
    Variant,
    VariantSimResult,
)
from backend.services.optimization.repository import (
    OptimizationRunRepositoryImpl,
)
from backend.services.optimization.submit_policy import StageASubmitPolicy
from backend.services.optimization.winner_selector import WinnerSelector


# ---------------------------------------------------------------------------
# Fake Simulator — sidesteps BRAIN + Redis + asyncio gather mechanics.
# Each call returns a synthetic VariantSimResult; sharpe ramps so half the
# variants clear delay-0 band (≥2.0) and the rest don't.
# ---------------------------------------------------------------------------


class _FakeSimulator:
    def __init__(self, *, raise_on_call: bool = False):
        self.raise_on_call = raise_on_call
        self.calls: int = 0

    async def run_batch(
        self, variants: List[Variant], budget: int
    ) -> List[VariantSimResult]:
        self.calls += 1
        if self.raise_on_call:
            raise RuntimeError("simulated BRAIN outage")
        out: List[VariantSimResult] = []
        for i, v in enumerate(variants[:budget]):
            # Alternate winners/losers: even i passes, odd i fails sharpe.
            sharpe = 2.5 if i % 2 == 0 else 1.0
            out.append(VariantSimResult(
                variant=v,
                sim_response={
                    "is": {
                        "sharpe": sharpe, "fitness": 1.5, "returns": 0.14,
                        "turnover": 0.25, "margin": 0.001, "drawdown": 0.05,
                        "longCount": 500, "shortCount": 500,
                        "checks": [
                            {"name": "LOW_SHARPE", "result": "PASS"},
                            {"name": "LOW_SUB_UNIVERSE_SHARPE", "value": 0.9, "result": "PASS"},
                        ],
                    }
                },
                sharpe=sharpe, fitness=1.5, turnover=0.25, margin=0.001,
                subuniv=0.9,
                brain_alpha_id=f"mock-alpha-{i}",
                checks_passed=True,
            ))
        return out


async def _seed_parent_alpha(db_session, *, delay: int = 0):
    parent = Alpha(
        alpha_id="parent-e2e",
        expression=(
            "group_neutralize(rank(ts_zscore(divide(cashflow_op, "
            "enterprise_value), 60)), industry)"
        ),
        region="USA",
        universe="TOP3000",
        delay=delay,
        truncation=0.08,
    )
    db_session.add(parent)
    await db_session.flush()
    parent.parent_alpha_family_id = parent.id   # backfill mimic
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
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_cycle_happy_path(db_session):
    parent = await _seed_parent_alpha(db_session, delay=0)
    sim = _FakeSimulator()
    svc = _build_service(db_session, simulator=sim)

    summary = await svc.run_one_cycle(
        parent, trigger_source="beat", budget=10,
    )

    # Summary shape
    assert isinstance(summary["opt_run_id"], int)
    assert summary["parent_alpha_id"] == parent.id
    assert summary["generator_name"] == "settings_sweep"
    assert summary["sim_budget_granted"] == 10
    assert summary["sim_budget_used"] == 10
    # SettingsSweepGenerator yields 10 variants for an alpha with ts_window;
    # FakeSimulator picks every other as winner → 5
    assert summary["n_variants"] == 10
    assert summary["n_winners"] == 5
    assert summary["n_submitted"] == 0   # NEVER auto-submit
    assert len(summary["persisted_pks"]) == 5

    # optimization_runs row lifecycle: open → finish stamped
    run = await db_session.get(OptimizationRun, summary["opt_run_id"])
    assert run.parent_alpha_id == parent.id
    assert run.generator_name == "settings_sweep"
    assert run.trigger_source == "beat"
    assert run.n_variants == 10
    assert run.n_winners == 5
    assert run.n_submitted == 0
    assert run.sim_budget_used == 10
    assert run.cycle_finished_at is not None
    assert run.error is None


@pytest.mark.asyncio
async def test_persisted_alphas_have_optimization_wiring(db_session):
    parent = await _seed_parent_alpha(db_session, delay=0)
    svc = _build_service(db_session, simulator=_FakeSimulator())
    summary = await svc.run_one_cycle(
        parent, trigger_source="beat", budget=10,
    )
    for pk in summary["persisted_pks"]:
        row = await db_session.get(Alpha, pk)
        assert row.optimization_run_id == summary["opt_run_id"]
        assert row.parent_alpha_id == parent.id
        # family_id traced back to root (parent itself)
        assert row.parent_alpha_family_id == parent.id
        assert row.delay == 0   # honored, not collapsed to 1
        # origin stamp readable by submit-backlog UI
        assert row.metrics["_origin"] == "opt:settings_sweep"
        assert row.metrics["_optimization_tag"].startswith("decay=")


@pytest.mark.asyncio
async def test_budget_truncation(db_session):
    """budget < n_variants → only budget many sims fire (truncation)."""
    parent = await _seed_parent_alpha(db_session, delay=0)
    sim = _FakeSimulator()
    svc = _build_service(db_session, simulator=sim)
    summary = await svc.run_one_cycle(
        parent, trigger_source="beat", budget=4,
    )
    # Generator emitted 10, simulator ran 4 (budget cap)
    assert summary["n_variants"] == 10
    assert summary["sim_budget_used"] == 4
    # Of 4 sim results, FakeSimulator alternates even-index sharpe=2.5 →
    # i=0 and i=2 pass = 2 winners
    assert summary["n_winners"] == 2


# ---------------------------------------------------------------------------
# Error path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_simulator_raise_stamps_error_and_propagates(db_session):
    """Mid-cycle exception → optimization_runs.error stamped + raise."""
    parent = await _seed_parent_alpha(db_session, delay=0)
    sim = _FakeSimulator(raise_on_call=True)
    svc = _build_service(db_session, simulator=sim)

    with pytest.raises(RuntimeError, match="simulated BRAIN outage"):
        await svc.run_one_cycle(
            parent, trigger_source="beat", budget=10,
        )

    # Find the half-finished row (last optimization_run for this parent).
    from sqlalchemy import select
    row = (await db_session.execute(
        select(OptimizationRun)
        .where(OptimizationRun.parent_alpha_id == parent.id)
        .order_by(OptimizationRun.id.desc())
    )).scalars().first()
    assert row is not None
    assert row.error is not None
    assert "simulated BRAIN outage" in row.error
    assert row.cycle_finished_at is not None
    # n_winners/n_variants stay 0 because we never reached record_persist
    assert row.n_variants == 0
    assert row.n_winners == 0


@pytest.mark.asyncio
async def test_delay_1_parent_uses_delay_1_band(db_session):
    """delay-1 parent → WinnerSelector picks against delay-1 band (sharpe>=1.5),
    so sharpe=1.0 still fails but sharpe=2.5 passes both bands."""
    parent = await _seed_parent_alpha(db_session, delay=1)
    sim = _FakeSimulator()
    svc = _build_service(db_session, simulator=sim)
    summary = await svc.run_one_cycle(
        parent, trigger_source="beat", budget=10,
    )
    # Same 5 winners under either delay because sharpe=2.5 clears both
    # 1.5 (delay-1) and 2.0 (delay-0). What's verified here is that delay=1
    # is accepted without exception (no AttributeError on band lookup).
    assert summary["n_winners"] == 5
