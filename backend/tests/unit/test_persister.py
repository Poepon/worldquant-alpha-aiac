"""Persister — winner → alphas row write path (real-ORM aiosqlite).

Per :file:`feedback_orm_constructor_real_test`, the Persister's INSERT
field map is the single most failure-prone part of Stage A (15+ kwargs;
silent drops are easy). Real-ORM tests catch:

  - Wrong column names (e.g. ``brain_alpha_id`` vs ``alpha_id``)
  - Missing optimization_run_id / parent_alpha_family_id wiring
  - metrics._origin stamp absence (frontend filter relies on it)
  - Wrong delay (the ``0 or 1 == 1`` bug class from
    test_settings_sweep_generator)
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from backend.models import Alpha, OptimizationRun
from backend.services.optimization.persister import Persister
from backend.services.optimization.protocols import Variant, VariantSimResult


async def _seed_parent_and_run(db_session) -> tuple[int, int]:
    """Insert one parent alpha + one open optimization_run row, return ids."""
    parent = Alpha(
        alpha_id="parent-A",
        expression="parent_expr",
        region="USA",
        universe="TOP3000",
        delay=0,
    )
    db_session.add(parent)
    await db_session.flush()

    # Backfill: root row's family_id = self.id (mimics migration)
    parent.parent_alpha_family_id = parent.id
    await db_session.flush()

    opt_run = OptimizationRun(
        parent_alpha_id=parent.id,
        generator_name="settings_sweep",
        trigger_source="beat",
        sim_budget_granted=10,
    )
    db_session.add(opt_run)
    await db_session.flush()
    return int(parent.id), int(opt_run.id)


def _make_winner(
    *, brain_alpha_id: str = "child-1", sharpe: float = 2.5,
    settings: dict = None,
) -> VariantSimResult:
    s = settings or {
        "region": "USA", "universe": "TOP3000", "delay": 0, "decay": 4,
        "neutralization": "INDUSTRY", "truncation": 0.08,
        "test_period": "P2Y0M",
    }
    v = Variant(
        expression="rank(close)",
        settings=s,
        tag="decay=4|neut=INDUSTRY",
        generator_name="settings_sweep",
    )
    return VariantSimResult(
        variant=v,
        sim_response={"is": {"sharpe": sharpe, "fitness": 1.5, "returns": 0.14,
                              "turnover": 0.25, "margin": 0.001, "drawdown": 0.05,
                              "longCount": 500, "shortCount": 500,
                              "checks": [{"name": "LOW_SHARPE", "result": "PASS"}]}},
        sharpe=sharpe, fitness=1.5, turnover=0.25, margin=0.001, subuniv=0.9,
        brain_alpha_id=brain_alpha_id,
        checks_passed=True,
    )


@pytest.mark.asyncio
async def test_save_inserts_alpha_with_expected_columns(db_session):
    parent_id, opt_run_id = await _seed_parent_and_run(db_session)
    persister = Persister(db_session, corr_service=None)
    w = _make_winner(brain_alpha_id="child-101")

    pks = await persister.save(
        winners=[w], parent_alpha_id=parent_id, opt_run_id=opt_run_id
    )
    assert len(pks) == 1
    new_pk = pks[0]
    assert new_pk is not None

    row = await db_session.get(Alpha, new_pk)
    assert row.alpha_id == "child-101"
    assert row.expression == "rank(close)"
    assert row.parent_alpha_id == parent_id
    assert row.optimization_run_id == opt_run_id
    # family_id inherits from parent (parent's family_id = parent.id)
    assert row.parent_alpha_family_id == parent_id
    assert row.region == "USA"
    assert row.universe == "TOP3000"
    assert row.delay == 0  # honored, NOT collapsed to 1 via `or`
    assert row.decay == 4
    assert row.neutralization == "INDUSTRY"
    assert row.quality_status == "PASS"
    assert row.can_submit is True
    assert row.status == "UNSUBMITTED"
    assert row.is_sharpe == pytest.approx(2.5)
    assert row.is_fitness == pytest.approx(1.5)
    assert row.is_turnover == pytest.approx(0.25)
    assert row.is_returns == pytest.approx(0.14)


@pytest.mark.asyncio
async def test_metrics_jsonb_carries_origin_stamp_and_tag(db_session):
    parent_id, opt_run_id = await _seed_parent_and_run(db_session)
    persister = Persister(db_session)
    w = _make_winner(brain_alpha_id="child-102")
    pks = await persister.save([w], parent_alpha_id=parent_id, opt_run_id=opt_run_id)
    row = await db_session.get(Alpha, pks[0])
    m = row.metrics
    assert m["_origin"] == "opt:settings_sweep"
    assert m["_optimization_tag"] == "decay=4|neut=INDUSTRY"
    # _sim_settings preserves the BRAIN-ready settings dict
    assert m["_sim_settings"]["neutralization"] == "INDUSTRY"
    assert m["_sim_settings"]["delay"] == 0
    # corr_service was None → _self_corr stays None
    assert m["_self_corr"] is None
    assert m["_self_corr_source"] is None


@pytest.mark.asyncio
async def test_save_returns_empty_list_for_empty_winners(db_session):
    persister = Persister(db_session)
    out = await persister.save([], parent_alpha_id=1, opt_run_id=1)
    assert out == []


@pytest.mark.asyncio
async def test_alpha_id_collision_returns_none_slot(db_session):
    """Collide on Alpha.alpha_id UNIQUE constraint — Persister must keep
    the slot in the return list as None so SubmitPolicy indexing stays
    aligned."""
    parent_id, opt_run_id = await _seed_parent_and_run(db_session)
    persister = Persister(db_session)

    # First winner — should land
    w1 = _make_winner(brain_alpha_id="collide-1")
    pks1 = await persister.save([w1], parent_alpha_id=parent_id, opt_run_id=opt_run_id)
    assert pks1[0] is not None

    # Second winner with same brain_alpha_id — UNIQUE violation
    w2 = _make_winner(brain_alpha_id="collide-1", sharpe=3.0)
    pks2 = await persister.save([w2], parent_alpha_id=parent_id, opt_run_id=opt_run_id)
    assert pks2 == [None]


@pytest.mark.asyncio
async def test_self_corr_stamped_when_corr_service_injected(db_session):
    """Inject a fake corr service that returns a known value → Persister
    must stamp metrics._self_corr + metrics._self_corr_source."""
    from backend.services.correlation_service import CorrSource

    class _FakeCorrService:
        async def calc_self_corr(self, *, alpha_id, region):
            return 0.42, CorrSource.LOCAL

    parent_id, opt_run_id = await _seed_parent_and_run(db_session)
    persister = Persister(db_session, corr_service=_FakeCorrService())
    w = _make_winner(brain_alpha_id="child-103")
    pks = await persister.save([w], parent_alpha_id=parent_id, opt_run_id=opt_run_id)
    row = await db_session.get(Alpha, pks[0])
    assert row.metrics["_self_corr"] == pytest.approx(0.42)
    assert row.metrics["_self_corr_source"] == "local"


@pytest.mark.asyncio
async def test_corr_service_exception_soft_fails(db_session):
    """corr_service raising → Persister catches, stamps _self_corr=None,
    persist still succeeds."""

    class _BrokenCorrService:
        async def calc_self_corr(self, *, alpha_id, region):
            raise RuntimeError("corr cache cold")

    parent_id, opt_run_id = await _seed_parent_and_run(db_session)
    persister = Persister(db_session, corr_service=_BrokenCorrService())
    w = _make_winner(brain_alpha_id="child-104")
    pks = await persister.save([w], parent_alpha_id=parent_id, opt_run_id=opt_run_id)
    assert pks[0] is not None
    row = await db_session.get(Alpha, pks[0])
    assert row.metrics["_self_corr"] is None
