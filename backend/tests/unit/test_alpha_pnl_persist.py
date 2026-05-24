"""Unit tests for AlphaService.upsert_alpha_pnl (2026-05-24).

Real in-memory aiosqlite ORM per [[feedback_orm_constructor_real_test]] — the
delete-then-insert + daily-diff + empty-guard is the bug-prone part and must be
exercised against a real session + DB read-back, not mocked.
"""
import pandas as pd
import pytest
from sqlalchemy import func, select

from backend.services.alpha_service import AlphaService


@pytest.mark.asyncio
async def test_upsert_stores_daily_diff_and_cumulative(db_session):
    from backend.models import AlphaPnl

    svc = AlphaService(db_session)
    idx = pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03"])
    series = pd.Series([100.0, 110.0, 105.0], index=idx)  # BRAIN cumulative pnl
    n = await svc.upsert_alpha_pnl(777, series)
    await db_session.commit()
    assert n == 3

    rows = (await db_session.execute(
        select(AlphaPnl).where(AlphaPnl.alpha_id == 777).order_by(AlphaPnl.trade_date)
    )).scalars().all()
    assert len(rows) == 3
    # cumulative = raw BRAIN value; pnl = daily diff (first day NaN → NULL)
    assert rows[0].cumulative_pnl == 100.0 and rows[0].pnl is None
    assert rows[1].cumulative_pnl == 110.0 and rows[1].pnl == pytest.approx(10.0)
    assert rows[2].cumulative_pnl == 105.0 and rows[2].pnl == pytest.approx(-5.0)


@pytest.mark.asyncio
async def test_empty_series_is_noop_never_wipes(db_session):
    """The critical guard: a transient empty/failed BRAIN fetch must NOT delete
    already-stored PnL."""
    from backend.models import AlphaPnl

    svc = AlphaService(db_session)
    await svc.upsert_alpha_pnl(888, pd.Series(
        [1.0, 2.0], index=pd.to_datetime(["2026-01-01", "2026-01-02"])
    ))
    await db_session.commit()

    for empty in (pd.Series(dtype=float), None):
        assert await svc.upsert_alpha_pnl(888, empty) == 0
    await db_session.commit()

    cnt = (await db_session.execute(
        select(func.count(AlphaPnl.id)).where(AlphaPnl.alpha_id == 888)
    )).scalar()
    assert cnt == 2  # preserved, not wiped


@pytest.mark.asyncio
async def test_nonempty_is_full_refresh(db_session):
    """A non-empty fetch fully replaces (BRAIN returns the complete backtest
    series each time, so the latest is authoritative)."""
    from backend.models import AlphaPnl

    svc = AlphaService(db_session)
    await svc.upsert_alpha_pnl(999, pd.Series(
        [1.0, 2.0, 3.0],
        index=pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03"]),
    ))
    await db_session.commit()
    await svc.upsert_alpha_pnl(999, pd.Series(
        [5.0], index=pd.to_datetime(["2026-02-01"])
    ))
    await db_session.commit()

    cnt = (await db_session.execute(
        select(func.count(AlphaPnl.id)).where(AlphaPnl.alpha_id == 999)
    )).scalar()
    assert cnt == 1  # old 3 dropped, new 1 inserted
