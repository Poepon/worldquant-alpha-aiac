"""Real-ORM tests for AlphaService._freeze_predicted_marginal (forward-test
capture, 2026-06-03).

Per [[feedback_orm_constructor_real_test]] the predicted-marginal freeze writes a
nested key into the raw JSONB ``Alpha.metrics`` column at submit time — the
bug-prone part (whole-dict reassign must be ORM-change-tracked; the pool query
must EXCLUDE the candidate; non-measurable must still freeze the prediction) is
exercised against a real aiosqlite session + DB read-back, not mocked.
"""
import math
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from backend.services.alpha_service import AlphaService
from backend.models import Alpha, AlphaPnl


def _pnl_rows(alpha_id: int, n: int, phase: float, *, scale: float = 1.0):
    """Deterministic daily PnL with non-zero variance (sin) so annualized_sharpe
    is well-defined. Distinct phase per series → non-degenerate correlation."""
    start = datetime(2020, 1, 1)
    out = []
    for i in range(n):
        val = math.sin(i / 5.0 + phase) * scale + 0.01
        out.append(AlphaPnl(alpha_id=alpha_id, trade_date=start + timedelta(days=i), pnl=val))
    return out


async def _mk_alpha(db, alpha_id: str, *, submitted: bool, region: str = "USA", metrics=None):
    a = Alpha(
        alpha_id=alpha_id,
        expression="ts_rank(close, 5)",
        region=region,
        universe="TOP3000",
    )
    if submitted:
        a.date_submitted = datetime.utcnow()
    if metrics is not None:
        a.metrics = metrics
    db.add(a)
    await db.flush()   # assign autoincrement id
    return a


async def _readback(db, alpha_pk: int):
    """Force a DB reload (not the identity-mapped instance) to prove persistence."""
    a = (await db.execute(select(Alpha).where(Alpha.id == alpha_pk))).scalar_one()
    db.expire(a)
    return (await db.execute(select(Alpha).where(Alpha.id == alpha_pk))).scalar_one()


@pytest.mark.asyncio
async def test_freeze_measurable_against_pool(db_session):
    svc = AlphaService(db_session)
    p1 = await _mk_alpha(db_session, "POOL0001A", submitted=True)
    p2 = await _mk_alpha(db_session, "POOL0002B", submitted=True)
    cand = await _mk_alpha(
        db_session, "CAND0001X", submitted=True,
        metrics={"_iqc_marginal": {"delta_sharpe": -0.05}},
    )
    N = 80
    for r in _pnl_rows(p1.id, N, 0.0):
        db_session.add(r)
    for r in _pnl_rows(p2.id, N, 1.3):
        db_session.add(r)
    for r in _pnl_rows(cand.id, N, 2.6):
        db_session.add(r)
    await db_session.flush()

    await svc._freeze_predicted_marginal(cand)
    await db_session.commit()

    rec = (await _readback(db_session, cand.id)).metrics["_recon_predicted_delta_sharpe"]
    assert rec["measurable"] is True
    assert isinstance(rec["predicted_delta_sharpe"], float)
    # BRAIN pre-submit before-and-after frozen from _iqc_marginal.delta_sharpe.
    assert rec["brain_pre_submit_delta_sharpe"] == -0.05
    # pool EXCLUDES the candidate itself (id != sid) → 2 submitted members.
    assert rec["pool_n"] == 2
    assert rec["method"] == "marginal_drain.v1"
    assert rec["region"] == "USA"
    assert rec["captured_at"]
    assert "pool_window_start" in rec and "pool_window_end" in rec
    assert "cand_pnl_end" in rec
    # the co-resident _iqc_marginal key is untouched by the whole-dict reassign.
    assert (await _readback(db_session, cand.id)).metrics["_iqc_marginal"]["delta_sharpe"] == -0.05


@pytest.mark.asyncio
async def test_freeze_no_pnl_still_records_prediction(db_session):
    # No local PnL (and no pool) ⇒ predicted is unmeasurable, but the prediction
    # record is STILL frozen (measurable=False) so the forward test knows we tried
    # and BRAIN's pre-submit estimate is captured at its only available moment.
    svc = AlphaService(db_session)
    cand = await _mk_alpha(
        db_session, "CAND0002Y", submitted=True,
        metrics={"_iqc_marginal": {"delta_sharpe": 0.07}},
    )
    await svc._freeze_predicted_marginal(cand)
    await db_session.commit()

    rec = (await _readback(db_session, cand.id)).metrics["_recon_predicted_delta_sharpe"]
    assert rec["measurable"] is False
    assert rec["predicted_delta_sharpe"] is None
    assert rec["brain_pre_submit_delta_sharpe"] == 0.07
    assert rec["pool_n"] == 0


@pytest.mark.asyncio
async def test_freeze_no_brain_pre_submit_estimate(db_session):
    # No _iqc_marginal (audit never ran) ⇒ brain_pre is None, but the offline
    # predicted ΔSharpe is still computed against the 1-member pool.
    svc = AlphaService(db_session)
    p1 = await _mk_alpha(db_session, "POOL0003C", submitted=True)
    cand = await _mk_alpha(db_session, "CAND0003Z", submitted=True, metrics={})
    N = 80
    for r in _pnl_rows(p1.id, N, 0.5):
        db_session.add(r)
    for r in _pnl_rows(cand.id, N, 2.0):
        db_session.add(r)
    await db_session.flush()

    await svc._freeze_predicted_marginal(cand)
    await db_session.commit()

    rec = (await _readback(db_session, cand.id)).metrics["_recon_predicted_delta_sharpe"]
    assert rec["brain_pre_submit_delta_sharpe"] is None
    assert isinstance(rec["predicted_delta_sharpe"], float)
    assert rec["pool_n"] == 1


@pytest.mark.asyncio
async def test_freeze_excludes_other_region_pool(db_session):
    # Pool is per-region: a CHN submitted alpha must NOT enter a USA candidate's
    # pool (cross-region pooling would be a meaningless mixed portfolio).
    svc = AlphaService(db_session)
    usa = await _mk_alpha(db_session, "POOLUSA01", submitted=True, region="USA")
    chn = await _mk_alpha(db_session, "POOLCHN01", submitted=True, region="CHN")
    cand = await _mk_alpha(db_session, "CANDUSA01", submitted=True, region="USA", metrics={})
    N = 80
    for r in _pnl_rows(usa.id, N, 0.0):
        db_session.add(r)
    for r in _pnl_rows(chn.id, N, 1.0):
        db_session.add(r)
    for r in _pnl_rows(cand.id, N, 2.0):
        db_session.add(r)
    await db_session.flush()

    await svc._freeze_predicted_marginal(cand)
    await db_session.commit()

    rec = (await _readback(db_session, cand.id)).metrics["_recon_predicted_delta_sharpe"]
    assert rec["pool_n"] == 1   # only the USA member, NOT the CHN one
    assert rec["region"] == "USA"
