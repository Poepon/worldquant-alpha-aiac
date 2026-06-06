"""Pool Phase 2 (1c) — cognitive reconcile beat.

Drives the typed-Hypothesis lifecycle off recently-landed alphas:
refresh_stats → auto-activate → PROMOTE-on-can_submit (NOT pass_count, guard #5)
→ cheap attribution stamp. Watermark on alphas.created_at + grace; idempotent.

Service-level (sqlite) so it runs in the regular `--all` suite.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.database import SQLAlchemyBase
from backend.models import Alpha, AlphaFailure, Hypothesis, SystemConfig
from backend.services.hypothesis_service import HypothesisCreateData, HypothesisService
from backend.tasks.cognitive_reconcile_tasks import (
    _WM_KEY,
    _bucket_failures,
    _reconcile_async,
    run_pool_cognitive_reconcile,
)


async def _setup_db():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(SQLAlchemyBase.metadata.create_all)
    return eng, async_sessionmaker(eng, expire_on_commit=False)


def _past(hours=1):
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)


async def _mk_hyp(s, status="PROPOSED"):
    svc = HypothesisService(s)
    h = await svc.create_hypothesis(HypothesisCreateData(statement="h", region="USA"))
    if status != "PROPOSED":
        await s.execute(update(Hypothesis).where(Hypothesis.id == h.id).values(status=status))
    await s.commit()
    return h.id


def _alpha(hid, **kw):
    base = dict(region="USA", universe="TOP3000", expression="ts_rank(close,5)",
                hypothesis_id=hid, created_at=_past(), delay=1)
    base.update(kw)
    return Alpha(**base)


# ---------------------------------------------------------------------------
class TestBucketFailures:
    def test_syntax(self):
        assert _bucket_failures(["SYNTAX_ERROR", "FIELD_NOT_FOUND"]) == (2, 0, 0)

    def test_simulate(self):
        assert _bucket_failures(["SIMULATE_TIMEOUT", "BRAIN_429"]) == (0, 2, 0)

    def test_quality_and_none(self):
        # Unknown / quality gate names + None → quality bucket (ran, missed gate).
        assert _bucket_failures(["LOW_SHARPE", "CONCENTRATED_WEIGHT", None]) == (0, 0, 3)

    def test_mixed(self):
        assert _bucket_failures(["SYNTAX_ERROR", "SIM_FAIL", "LOW_FITNESS"]) == (1, 1, 1)


# ---------------------------------------------------------------------------
class TestReconcile:

    @pytest.mark.asyncio
    async def test_promote_on_can_submit(self):
        eng, sf = await _setup_db()
        try:
            async with sf() as s:
                hid = await _mk_hyp(s)
                s.add(_alpha(hid, alpha_id="A1", quality_status="PASS",
                             can_submit=True, is_sharpe=1.6))
                await s.commit()
            res = await _reconcile_async(grace_sec=60, window_days=7, session_factory=sf)
            assert res["promoted"] == 1
            async with sf() as s:
                h = await s.get(Hypothesis, hid)
                assert h.status == "PROMOTED"
                assert h.can_submit_count == 1
        finally:
            await eng.dispose()

    @pytest.mark.asyncio
    async def test_provisional_does_not_promote(self):
        """Guard #5: PASS_PROVISIONAL with can_submit NULL → ACTIVATE, never
        PROMOTE. pass_count counts the PROV alpha; can_submit_count stays 0."""
        eng, sf = await _setup_db()
        try:
            async with sf() as s:
                hid = await _mk_hyp(s)
                s.add(_alpha(hid, alpha_id="B1", quality_status="PASS_PROVISIONAL",
                             can_submit=None, is_sharpe=1.3))
                await s.commit()
            res = await _reconcile_async(grace_sec=60, window_days=7, session_factory=sf)
            assert res["promoted"] == 0
            assert res["activated"] == 1
            async with sf() as s:
                h = await s.get(Hypothesis, hid)
                assert h.status == "ACTIVE"          # NOT promoted
                assert h.pass_count == 1             # PROV counts as pass
                assert h.can_submit_count == 0       # but NOT submittable (censored)
        finally:
            await eng.dispose()

    @pytest.mark.asyncio
    async def test_attribution_stamped_on_quality_failures(self):
        """A hypothesis whose alphas all failed quality gates (alpha_failures)
        with 0 PASS → attribution='hypothesis'."""
        eng, sf = await _setup_db()
        try:
            async with sf() as s:
                hid = await _mk_hyp(s)
                # Quality fails (ran but missed gate) → hypothesis-attributable.
                for et in ("LOW_SHARPE", "LOW_FITNESS", "CONCENTRATED_WEIGHT"):
                    s.add(AlphaFailure(hypothesis_id=hid, error_type=et))
                # An alpha row too so created_at falls in-window (failures have no
                # created_at watermark hook — the hyp is discovered via its alpha).
                s.add(_alpha(hid, alpha_id="C0", quality_status="REJECTED", can_submit=False))
                await s.commit()
            res = await _reconcile_async(grace_sec=60, window_days=7, session_factory=sf)
            assert res["attributed"] == 1
            async with sf() as s:
                h = await s.get(Hypothesis, hid)
                assert h.attribution == "hypothesis"
                assert h.status == "ACTIVE"   # activated, not promoted (0 can_submit)
        finally:
            await eng.dispose()

    @pytest.mark.asyncio
    async def test_watermark_advances_and_idempotent(self):
        """Second run over the same already-processed alphas finds an empty
        window (watermark advanced past them) → no re-processing."""
        eng, sf = await _setup_db()
        try:
            async with sf() as s:
                hid = await _mk_hyp(s)
                s.add(_alpha(hid, alpha_id="D1", quality_status="PASS", can_submit=True))
                await s.commit()
            r1 = await _reconcile_async(grace_sec=60, window_days=7, session_factory=sf)
            assert r1["promoted"] == 1
            async with sf() as s:
                wm = (await s.execute(
                    SystemConfig.__table__.select().where(SystemConfig.config_key == _WM_KEY)
                )).first()
                assert wm is not None  # watermark persisted
            r2 = await _reconcile_async(grace_sec=60, window_days=7, session_factory=sf)
            assert r2["hypotheses"] == 0  # nothing new in-window
            assert r2.get("promoted", 0) == 0
        finally:
            await eng.dispose()

    @pytest.mark.asyncio
    async def test_flag_off_skips(self):
        # Default flag is OFF → the celery wrapper returns flag_off without DB.
        out = run_pool_cognitive_reconcile()
        assert out.get("skipped_reason") == "flag_off"
