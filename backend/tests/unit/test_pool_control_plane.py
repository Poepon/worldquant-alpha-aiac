"""Phase 1b B3 — pool drain + budget control-plane tests."""
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from backend.database import SQLAlchemyBase
from backend.models import CandidateQueue
from backend.pool import budget, drain
from backend.pool import stages as st


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def get(self, k):
        return self.store.get(k)

    def set(self, k, v):
        self.store[k] = v

    def delete(self, k):
        self.store.pop(k, None)

    def incrby(self, k, n):
        self.store[k] = int(self.store.get(k, 0)) + n
        return self.store[k]

    def expire(self, k, t):
        pass


# get_redis_client is lazy-imported from backend.tasks.redis_pool inside both
# modules, so patching it there intercepts.
_PATCH = "backend.tasks.redis_pool.get_redis_client"


def test_drain_set_clear_and_check():
    fr = _FakeRedis()
    with patch(_PATCH, return_value=fr):
        assert drain.is_draining("s") is False
        drain.set_drain("s")
        assert drain.is_draining("s") is True
        assert fr.store["pool:s:drain"] == "1"
        drain.clear_drain("s")
        assert drain.is_draining("s") is False


def test_is_draining_fails_open_on_redis_error():
    def _boom():
        raise RuntimeError("redis down")

    with patch(_PATCH, side_effect=_boom):
        assert drain.is_draining("hg") is False  # fail-open


def test_budget_sims_incr_and_exceeded():
    fr = _FakeRedis()
    with patch(_PATCH, return_value=fr):
        assert budget.sims_today() == 0
        budget.incr_sims(3)
        budget.incr_sims()
        assert budget.sims_today() == 4
        assert budget.sims_budget_exceeded() is False
        # force above the daily limit
        fr.store[budget._sims_key()] = str(int(__import__("backend.config", fromlist=["settings"]).settings.BRAIN_DAILY_SIMULATE_LIMIT))
        assert budget.sims_budget_exceeded() is True


def test_budget_tokens_incr_and_exceeded():
    fr = _FakeRedis()
    with patch(_PATCH, return_value=fr):
        assert budget.tokens_today() == 0
        budget.incr_tokens(5000)
        assert budget.tokens_today() == 5000
        assert budget.tokens_budget_exceeded() is False


def test_budget_fails_open_on_redis_error():
    with patch(_PATCH, side_effect=RuntimeError("down")):
        assert budget.sims_today() == 0
        assert budget.sims_budget_exceeded() is False  # fail-open
        budget.incr_sims(1)  # non-fatal, no raise


@pytest.mark.asyncio
async def test_purge_pending_skips_inflight():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(SQLAlchemyBase.metadata.create_all)
    sf = async_sessionmaker(eng, expire_on_commit=False)
    try:
        async with sf() as s:
            async with s.begin():
                s.add_all([
                    CandidateQueue(region="USA", expression="a", stage=st.SIM_PENDING),
                    CandidateQueue(region="USA", expression="b", stage=st.EVAL_PENDING),
                    CandidateQueue(region="USA", expression="c", stage=st.SIM_INFLIGHT),  # in-flight
                    CandidateQueue(region="USA", expression="d", stage=st.CAND_DONE),     # terminal
                ])
        n = await drain.purge_pending(CandidateQueue, session_factory=sf)
        assert n == 2  # only the two PENDING-family rows
        async with sf() as s:
            from sqlalchemy import select
            rows = (await s.execute(select(CandidateQueue))).scalars().all()
            by_expr = {r.expression: r.stage for r in rows}
        assert by_expr["a"] == st.CAND_PURGED
        assert by_expr["b"] == st.CAND_PURGED
        assert by_expr["c"] == st.SIM_INFLIGHT  # untouched
        assert by_expr["d"] == st.CAND_DONE      # untouched
    finally:
        await eng.dispose()
