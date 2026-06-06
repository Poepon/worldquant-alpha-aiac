"""V-27 backlog C 段 — submit gate + JSONB 并发 + portfolio beat(Commit 2).

Covers V-27.127 / 140 / 147. Targets the real PostgreSQL DB; BRAIN is mocked.

Run:
    pytest backend/tests/integration/test_v27c_submit_gate_jsonb.py -v

Requires: PostgreSQL on POSTGRES_PORT (5433).
"""
from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio

os.environ.setdefault("POSTGRES_PORT", "5433")

from backend.services.alpha_service import AlphaService  # noqa: E402

_TAG = f"v27c-test-{uuid.uuid4().hex[:8]}"


@pytest_asyncio.fixture
async def pg_session():
    """Real-PG session per test. Cleans up _TAG-tagged rows after."""
    from sqlalchemy import delete, text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from backend.config import settings
    from backend.models import Alpha, MiningTask

    engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as db:
        yield db
        async with Session() as cleanup:
            tids = (
                await cleanup.execute(
                    text("SELECT id FROM mining_tasks WHERE task_name LIKE :p"),
                    {"p": f"{_TAG}%"},
                )
            ).scalars().all()
            if tids:
                await cleanup.execute(
                    delete(Alpha).where(Alpha.task_id.in_(tids))
                )
                await cleanup.execute(
                    delete(MiningTask).where(MiningTask.id.in_(tids))
                )
                await cleanup.commit()
    await engine.dispose()


async def _mk_alpha(pg_session, *, can_submit, metrics, region="ZZ1"):
    """A submitted-eligible alpha row + its parent task."""
    from backend.models import Alpha, MiningTask
    task = MiningTask(
        task_name=f"{_TAG}-task-{uuid.uuid4().hex[:6]}", region=region,
        universe="TOP3000", dataset_strategy="AUTO",        status="RUNNING", daily_goal=4, 
    )
    pg_session.add(task)
    await pg_session.flush()
    alpha = Alpha(
        alpha_id=f"brain-{uuid.uuid4().hex[:14]}",
        task_id=task.id,
        expression="rank(close)",
        expression_hash=uuid.uuid4().hex,
        region=region,
        universe="TOP3000",
        status="created",
        quality_status="PASS",
        human_feedback="NONE",
        can_submit=can_submit,
        metrics=metrics,
        date_submitted=None,
    )
    pg_session.add(alpha)
    await pg_session.commit()
    await pg_session.refresh(alpha)
    return alpha


class _MockBrain:
    """Minimal BrainAdapter stand-in for submit_alpha / refresh_can_submit."""
    def __init__(self, *, check_corr=None, submit_result=None, get_alpha=None):
        self._check_corr = check_corr
        self._submit = submit_result or {
            "success": True, "status_code": 200, "body": {},
        }
        self._get_alpha = get_alpha

    async def _get_slot_redis(self):
        # Force submit_alpha onto its "redis unavailable → proceed" path so
        # the test doesn't need a real Redis lock.
        raise RuntimeError("no redis in test")

    async def check_correlation(self, alpha_id, check_type="SELF"):
        return self._check_corr

    async def submit_alpha(self, alpha_id):
        return self._submit

    async def get_alpha(self, alpha_id):
        return self._get_alpha


# ---------------------------------------------------------------------------
# V-27.127 — submit gate-3 self-corr override
# ---------------------------------------------------------------------------

class TestSubmitGate3SelfCorrOverride:
    @pytest.mark.asyncio
    async def test_only_self_corr_fail_defers_to_live_precheck(self, pg_session):
        # can_submit=False, only self-corr failed checks, live corr now LOW
        # → gate-3 defers, gate-4 lets it through, submit succeeds.
        alpha = await _mk_alpha(
            pg_session, can_submit=False,
            metrics={"_brain_failed_checks": [
                {"name": "LOCAL_SELF_CORRELATION", "result": "FAIL"}
            ]},
        )
        brain = _MockBrain(check_corr={"max": 0.1})  # live corr dropped
        res = await AlphaService(pg_session).submit_alpha(
            alpha.id, brain_adapter=brain
        )
        assert res["submitted"] is True, res

    @pytest.mark.asyncio
    async def test_non_self_corr_fail_still_hard_blocks(self, pg_session):
        alpha = await _mk_alpha(
            pg_session, can_submit=False,
            metrics={"_brain_failed_checks": [
                {"name": "LOCAL_SELF_CORRELATION", "result": "FAIL"},
                {"name": "LOW_SHARPE", "result": "FAIL"},  # non-self-corr
            ]},
        )
        brain = _MockBrain(check_corr={"max": 0.1})
        res = await AlphaService(pg_session).submit_alpha(
            alpha.id, brain_adapter=brain
        )
        assert res["submitted"] is False
        assert "can_submit" in res["reason"]

    @pytest.mark.asyncio
    async def test_can_submit_none_not_overridable(self, pg_session):
        # None = "no BRAIN signal", not "tested & stale" — must hard-block.
        alpha = await _mk_alpha(
            pg_session, can_submit=None,
            metrics={"_brain_failed_checks": [
                {"name": "LOCAL_SELF_CORRELATION", "result": "FAIL"}
            ]},
        )
        brain = _MockBrain(check_corr={"max": 0.1})
        res = await AlphaService(pg_session).submit_alpha(
            alpha.id, brain_adapter=brain
        )
        assert res["submitted"] is False
        assert "can_submit" in res["reason"]

    @pytest.mark.asyncio
    async def test_gate4_still_blocks_when_live_corr_high(self, pg_session):
        # Gate-3 defers, but gate-4's live precheck measures corr >= 0.7 →
        # still blocked (correctly, just at gate-4 not gate-3).
        alpha = await _mk_alpha(
            pg_session, can_submit=False,
            metrics={"_brain_failed_checks": [
                {"name": "SELF_CORRELATION", "result": "FAIL"}
            ]},
        )
        brain = _MockBrain(check_corr={"max": 0.92})  # live corr still high
        res = await AlphaService(pg_session).submit_alpha(
            alpha.id, brain_adapter=brain
        )
        assert res["submitted"] is False
        assert "self_corr" in res["reason"]

    @pytest.mark.asyncio
    async def test_flag_off_restores_hard_block(self, pg_session, monkeypatch):
        from backend.config import settings
        monkeypatch.setattr(
            settings, "SUBMIT_GATE_LIVE_SELF_CORR_OVERRIDE", False
        )
        alpha = await _mk_alpha(
            pg_session, can_submit=False,
            metrics={"_brain_failed_checks": [
                {"name": "LOCAL_SELF_CORRELATION", "result": "FAIL"}
            ]},
        )
        brain = _MockBrain(check_corr={"max": 0.1})
        res = await AlphaService(pg_session).submit_alpha(
            alpha.id, brain_adapter=brain
        )
        # flag off → gate-3 hard-blocks again (pre-fix behaviour)
        assert res["submitted"] is False
        assert "can_submit" in res["reason"]


# ---------------------------------------------------------------------------
# V-27.140 — refresh_can_submit JSONB in-place merge (no clobber)
# ---------------------------------------------------------------------------

class TestRefreshCanSubmitJsonbMerge:
    @pytest.mark.asyncio
    async def test_unrelated_keys_survive(self, pg_session):
        from backend.models import Alpha
        from sqlalchemy import select

        # metrics carries an unrelated key written by some other path.
        alpha = await _mk_alpha(
            pg_session, can_submit=None,
            metrics={"_unrelated_iqc": "keep_me", "sharpe": 1.5},
        )
        # BRAIN detail with an all-PASS checks array → can_submit True.
        brain = _MockBrain(get_alpha={
            "is": {"checks": [{"name": "SHARPE", "result": "PASS"}]}
        })
        out = await AlphaService(pg_session).refresh_can_submit(
            alpha.id, brain_adapter=brain
        )
        assert out is not None and out["can_submit"] is True

        # Re-read from a fresh state — the in-place `||` merge must have
        # preserved the unrelated key while setting the _brain_* keys.
        refreshed = (
            await pg_session.execute(select(Alpha).where(Alpha.id == alpha.id))
        ).scalar_one()
        await pg_session.refresh(refreshed)
        assert refreshed.metrics["_unrelated_iqc"] == "keep_me"
        assert refreshed.metrics["sharpe"] == 1.5
        assert refreshed.metrics["_brain_can_submit"] is True
        assert refreshed.can_submit is True


# ---------------------------------------------------------------------------
# V-27.147 — refresh_portfolio_skeletons_all beat task
# ---------------------------------------------------------------------------

def test_refresh_portfolio_skeletons_beat(monkeypatch):
    """The beat sweep resolves distinct submitted-alpha regions from the DB
    and calls refresh_portfolio_from_db once per region (best-effort)."""
    from backend.tasks import sync_tasks

    called: list = []

    async def _fake_refresh(region="USA"):
        called.append(region)
        return 7

    monkeypatch.setattr(
        "backend.agents.seed_pool.portfolio_skeletons.refresh_portfolio_from_db",
        _fake_refresh,
    )
    result = sync_tasks.refresh_portfolio_skeletons_all()
    assert "refreshed" in result
    # one call per distinct region surfaced from the DB (may be empty if the
    # dev DB has no submitted alphas — still a valid no-op sweep)
    assert set(called) == set(result["refreshed"].keys())
    for region, n in result["refreshed"].items():
        assert n == 7
