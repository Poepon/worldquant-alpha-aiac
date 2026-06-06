"""Pool Phase 2 (1a) — lease-recycle dedup for typed Hypothesis creation.

HypothesisService.find_open_by_intent + create_hypothesis(hyp_intent_id) ensure a
re-claimed HG intent reuses the open PROPOSED/ACTIVE row instead of inserting an
orphan duplicate (plan §7 Track C guard #3). Terminal rows (PROMOTED / ABANDONED /
SUPERSEDED) are NOT reused — a re-run after graduation should make a fresh row.

Service-level (sqlite) so it runs in the regular `--all` suite (no live PG).
"""
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.database import SQLAlchemyBase
from backend.models import Hypothesis
from backend.services.hypothesis_service import HypothesisCreateData, HypothesisService


async def _setup_db():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(SQLAlchemyBase.metadata.create_all)
    return eng, async_sessionmaker(eng, expire_on_commit=False)


def _data(intent_id, stmt="h"):
    return HypothesisCreateData(statement=stmt, region="USA", hyp_intent_id=intent_id)


@pytest.mark.asyncio
async def test_create_stamps_hyp_intent_id():
    eng, sf = await _setup_db()
    try:
        async with sf() as s:
            svc = HypothesisService(s)
            row = await svc.create_hypothesis(_data(42))
            await s.commit()
            assert row.hyp_intent_id == 42
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_find_open_by_intent_returns_open_row():
    eng, sf = await _setup_db()
    try:
        async with sf() as s:
            svc = HypothesisService(s)
            row = await svc.create_hypothesis(_data(99))
            await s.commit()
            found = await svc.find_open_by_intent(99)
            assert found is not None and found.id == row.id
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_dedup_reuse_on_reclaim_no_second_insert():
    """A second worker re-running the same intent finds the open row — the
    dedup the node uses to avoid an orphan PROPOSED duplicate."""
    eng, sf = await _setup_db()
    try:
        async with sf() as s:
            svc = HypothesisService(s)
            first = await svc.create_hypothesis(_data(7, "first"))
            await s.commit()
            first_id = first.id
        async with sf() as s:  # re-claimed intent, fresh session
            svc = HypothesisService(s)
            existing = await svc.find_open_by_intent(7)
            assert existing is not None and existing.id == first_id
            cnt = (await s.execute(
                select(func.count(Hypothesis.id)).where(Hypothesis.hyp_intent_id == 7)
            )).scalar()
            assert cnt == 1  # still exactly one row for the intent
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_promoted_row_not_reused():
    """A graduated (PROMOTED) hypothesis is excluded — a re-run starts fresh,
    never resurrects a closed lifecycle."""
    eng, sf = await _setup_db()
    try:
        async with sf() as s:
            svc = HypothesisService(s)
            row = await svc.create_hypothesis(_data(5))
            await s.commit()
            await svc.mark_promoted(row.id)
            await s.commit()
            assert await svc.find_open_by_intent(5) is None
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_abandoned_row_not_reused():
    eng, sf = await _setup_db()
    try:
        async with sf() as s:
            svc = HypothesisService(s)
            row = await svc.create_hypothesis(_data(6))
            await s.commit()
            await svc.mark_abandoned(row.id, reason="test abandon")
            await s.commit()
            assert await svc.find_open_by_intent(6) is None
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_none_intent_returns_none():
    eng, sf = await _setup_db()
    try:
        async with sf() as s:
            svc = HypothesisService(s)
            assert await svc.find_open_by_intent(None) is None
    finally:
        await eng.dispose()
