"""Phase 1b B1 — claim/lease primitive tests (backend/pool/queue.py).

In-memory async SQLite (conftest registers the JSONB→JSON shim). SKIP LOCKED is
a no-op on SQLite (SQLAlchemy omits FOR UPDATE), so these exercise the claim/
lease/complete/recycle LOGIC single-threaded; true concurrent-claim-no-double-
領 is a PG integration test (B1 follow-up, @requires_postgres).
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from backend.database import SQLAlchemyBase
from backend.models import HypothesisIntent, CandidateQueue
from backend.pool import queue as q
from backend.pool import stages as st


async def _setup():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(SQLAlchemyBase.metadata.create_all)
    return eng, async_sessionmaker(eng, expire_on_commit=False)


async def _add_intents(sf, n, *, stage=st.INTENT_PENDING, attempts=0):
    ids = []
    async with sf() as s:
        async with s.begin():
            for _ in range(n):
                row = HypothesisIntent(region="USA", config_snapshot={},
                                       stage=stage, attempts=attempts)
                s.add(row)
                await s.flush()
                ids.append(row.id)
    return ids


async def _stage_of(sf, model, row_id):
    async with sf() as s:
        row = await s.get(model, row_id)
        return None if row is None else row.stage


@pytest.mark.asyncio
async def test_claim_flips_stage_stamps_and_skips_inflight():
    eng, sf = await _setup()
    try:
        await _add_intents(sf, 2)
        row = await q.claim_one(HypothesisIntent, st.INTENT_PENDING, "hg-1", 300, session_factory=sf)
        assert row is not None
        assert row.stage == st.INTENT_CLAIMED
        assert row.claimed_by == "hg-1"
        assert row.attempts == 1
        assert row.lease_expires_at is not None
        # next claim gets the OTHER row (the first is now in-flight, not PENDING)
        row2 = await q.claim_one(HypothesisIntent, st.INTENT_PENDING, "hg-2", 300, session_factory=sf)
        assert row2 is not None and row2.id != row.id
        # queue drained → None
        assert await q.claim_one(HypothesisIntent, st.INTENT_PENDING, "hg-3", 300, session_factory=sf) is None
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_complete_advances_and_clears_lease():
    eng, sf = await _setup()
    try:
        [rid] = await _add_intents(sf, 1)
        await q.claim_one(HypothesisIntent, st.INTENT_PENDING, "hg-1", 300, session_factory=sf)
        ok = await q.complete(HypothesisIntent, rid, st.INTENT_DONE, session_factory=sf)
        assert ok
        async with sf() as s:
            row = await s.get(HypothesisIntent, rid)
            assert row.stage == st.INTENT_DONE
            assert row.claimed_by is None and row.lease_expires_at is None
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_fail_or_retry_then_poison():
    eng, sf = await _setup()
    try:
        # attempts pre-set to 0; claim bumps to 1 each time.
        [rid] = await _add_intents(sf, 1)
        await q.claim_one(HypothesisIntent, st.INTENT_PENDING, "hg-1", 300, session_factory=sf)  # attempts=1
        out = await q.fail_or_retry(HypothesisIntent, rid, st.INTENT_PENDING, max_attempts=2, session_factory=sf)
        assert out == "retry"
        assert await _stage_of(sf, HypothesisIntent, rid) == st.INTENT_PENDING
        await q.claim_one(HypothesisIntent, st.INTENT_PENDING, "hg-1", 300, session_factory=sf)  # attempts=2
        out = await q.fail_or_retry(HypothesisIntent, rid, st.INTENT_PENDING, max_attempts=2, session_factory=sf)
        assert out == "failed"  # attempts(2) >= cap(2) → poison-pill
        assert await _stage_of(sf, HypothesisIntent, rid) == st.INTENT_FAILED
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_renew_lease_extends_and_guards_owner():
    eng, sf = await _setup()
    try:
        [rid] = await _add_intents(sf, 1)
        await q.claim_one(HypothesisIntent, st.INTENT_PENDING, "hg-1", 60, session_factory=sf)
        async with sf() as s:
            before = (await s.get(HypothesisIntent, rid)).lease_expires_at
        ok = await q.renew_lease(HypothesisIntent, rid, 600, worker_id="hg-1", session_factory=sf)
        assert ok
        async with sf() as s:
            after = (await s.get(HypothesisIntent, rid)).lease_expires_at
        assert after > before
        # wrong owner → refused
        assert not await q.renew_lease(HypothesisIntent, rid, 600, worker_id="other", session_factory=sf)
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_recycle_expired_recovers_and_poisons():
    eng, sf = await _setup()
    try:
        # one CLAIMED-with-expired-lease, attempts=1 (< cap) → recycled to PENDING
        # one CLAIMED-with-expired-lease, attempts=3 (>= cap) → poisoned to FAILED
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        async with sf() as s:
            async with s.begin():
                a = HypothesisIntent(region="USA", config_snapshot={}, stage=st.INTENT_CLAIMED,
                                     claimed_by="dead", lease_expires_at=past, attempts=1)
                b = HypothesisIntent(region="USA", config_snapshot={}, stage=st.INTENT_CLAIMED,
                                     claimed_by="dead", lease_expires_at=past, attempts=3)
                # a live CLAIMED (future lease) must NOT be touched
                fut = datetime.now(timezone.utc) + timedelta(minutes=5)
                c = HypothesisIntent(region="USA", config_snapshot={}, stage=st.INTENT_CLAIMED,
                                     claimed_by="alive", lease_expires_at=fut, attempts=1)
                s.add_all([a, b, c])
                await s.flush()
                aid, bid, cid = a.id, b.id, c.id
        res = await q.recycle_expired(HypothesisIntent, max_attempts=3, session_factory=sf)
        assert res == {"recycled": 1, "poisoned": 1}
        assert await _stage_of(sf, HypothesisIntent, aid) == st.INTENT_PENDING   # recovered
        assert await _stage_of(sf, HypothesisIntent, bid) == st.INTENT_FAILED    # poisoned
        assert await _stage_of(sf, HypothesisIntent, cid) == st.INTENT_CLAIMED   # untouched (live lease)
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_candidate_queue_full_stage_chain():
    eng, sf = await _setup()
    try:
        async with sf() as s:
            async with s.begin():
                row = CandidateQueue(region="USA", expression="ts_rank(close, 20)",
                                     stage=st.SIM_PENDING, attempts=0)
                s.add(row)
                await s.flush()
                cid = row.id
        # S claims PENDING_SIM → SIMULATING, writes sim_result, → PENDING_EVAL
        c = await q.claim_one(CandidateQueue, st.SIM_PENDING, "s-1", 600, session_factory=sf)
        assert c.id == cid and c.stage == st.SIM_INFLIGHT
        await q.complete(CandidateQueue, cid, st.EVAL_PENDING,
                         updates={"sim_result": {"sharpe": 1.4}}, session_factory=sf)
        assert await _stage_of(sf, CandidateQueue, cid) == st.EVAL_PENDING
        # E claims PENDING_EVAL → EVALUATING, writes verdict, → DONE
        c = await q.claim_one(CandidateQueue, st.EVAL_PENDING, "e-1", 300, session_factory=sf)
        assert c.id == cid and c.stage == st.EVAL_INFLIGHT
        await q.complete(CandidateQueue, cid, st.CAND_DONE,
                         updates={"verdict": "PASS"}, session_factory=sf)
        async with sf() as s:
            row = await s.get(CandidateQueue, cid)
            assert row.stage == st.CAND_DONE
            assert row.sim_result == {"sharpe": 1.4}
            assert row.verdict == "PASS"
            assert row.claimed_by is None and row.lease_expires_at is None
    finally:
        await eng.dispose()
