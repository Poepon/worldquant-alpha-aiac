"""Unit-style tests for HypothesisHealthService + extended HypothesisService.

来源: docs/alphagbm_skills_research_2026-05-15.md skill `investment-thesis`.

Runs against live Postgres (the Hypothesis schema uses JSONB columns
that aiosqlite cannot render — same pattern as
``test_phase2_b7_hypothesis_service.py``). Skipped automatically when PG
isn't reachable on localhost:5433. Each test tags rows with a uuid
prefix; the session fixture cleans them up in a finally block.

These tests cover:
  - TestBaselineStamp  — _stamp_baseline_if_missing + mark_promoted hook
  - TestMarkTriggered  — dedup, FIFO cap, edge flip
  - TestUpdateThesisScore — history append + status persistence
  - TestCanCallLLM     — 24h gate vs 4h backoff (SFX-13)
  - TestScoreWithLLMOrFallback — three-segment fallback (MFX-6)
  - TestPersistResultEdge — audit row only on False→True edge (SFX-10)
"""
from __future__ import annotations

import os
import socket
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker


def _pg_reachable() -> bool:
    try:
        s = socket.create_connection(("localhost", 5433), timeout=1)
        s.close()
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_reachable(),
    reason="Postgres not reachable on localhost:5433 (hypothesis-health tests need JSONB)",
)


_PG_URL = os.environ.get(
    "TEST_PG_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5433/alpha_gpt",
)


# alpha_id is VARCHAR(20) — keep prefix short (≤8 chars so per-test suffix has room)
_TAG = f"_p2s{uuid.uuid4().hex[:4]}_"


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine(_PG_URL, echo=False)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as s:
            yield s
            try:
                from backend.models import (
                    Alpha,
                    Hypothesis,
                )
                # Order: alphas first (no FK to audit), then hypotheses
                # (CASCADE drops audit rows because the FK from
                # hypothesis_status_transitions.hypothesis_id to
                # hypotheses.id has ON DELETE CASCADE).
                await s.execute(
                    delete(Alpha).where(Alpha.alpha_id.like(f"{_TAG}%"))
                )
                await s.execute(
                    delete(Hypothesis).where(
                        Hypothesis.statement.like(f"{_TAG}%")
                    )
                )
                await s.commit()
            except Exception:
                await s.rollback()
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _mk_hyp(session, *, suffix="", **overrides):
    from backend.models import Hypothesis

    base = dict(
        statement=f"{_TAG}{suffix}",
        rationale="rationale",
        region="USA",
        kind="INVESTMENT_THESIS",
        target_tier=1,
        status="PROPOSED",
        is_active=True,
    )
    base.update(overrides)
    h = Hypothesis(**base)
    session.add(h)
    await session.flush()
    await session.refresh(h)
    return h


async def _mk_alpha(session, hyp_id, *, suffix="", **overrides):
    from backend.models import Alpha

    base = dict(
        alpha_id=f"{_TAG}{suffix}",
        expression="rank(close)",
        region="USA",
        universe="TOP3000",
        hypothesis_id=hyp_id,
        quality_status="PASS",
        is_sharpe=2.0,
        is_fitness=1.0,
        is_turnover=0.3,
    )
    base.update(overrides)
    a = Alpha(**base)
    session.add(a)
    await session.flush()
    await session.refresh(a)
    return a


# ===========================================================================
# Baseline stamp (MFX-1, MFX-3, MFX-4, SFX-14)
# ===========================================================================


class TestBaselineStamp:
    @pytest.mark.asyncio
    async def test_first_mark_promoted_stamps_baseline_avg(self, session):
        from backend.services.hypothesis_service import HypothesisService

        h = await _mk_hyp(session, suffix="bs-1", status="ACTIVE")
        await _mk_alpha(session, h.id, suffix="bs1a", is_sharpe=1.0)
        await _mk_alpha(session, h.id, suffix="bs1b", is_sharpe=2.0)
        await _mk_alpha(session, h.id, suffix="bs1c", is_sharpe=3.0)
        await session.commit()

        svc = HypothesisService(session)
        ok = await svc.mark_promoted(h.id)
        await session.commit()
        assert ok

        refreshed = await svc.get_by_id(h.id)
        bm = refreshed.baseline_metrics
        assert bm is not None
        assert bm["n_alphas"] == 3
        assert abs(bm["sharpe_avg"] - 2.0) < 1e-6  # avg of 1+2+3 = 2.0

    @pytest.mark.asyncio
    async def test_second_promote_does_not_overwrite(self, session):
        from backend.services.hypothesis_service import HypothesisService

        h = await _mk_hyp(session, suffix="bs-2", status="ACTIVE")
        await _mk_alpha(session, h.id, suffix="bs2a", is_sharpe=2.0)
        await _mk_alpha(session, h.id, suffix="bs2b", is_sharpe=2.0)
        await _mk_alpha(session, h.id, suffix="bs2c", is_sharpe=2.0)
        await session.commit()
        svc = HypothesisService(session)
        await svc.mark_promoted(h.id)
        await session.commit()
        h1 = await svc.get_by_id(h.id)
        baseline1 = h1.baseline_metrics

        # Add more PASS alphas + call mark_promoted again — must be no-op
        # because status is already PROMOTED (the WHERE clause filters
        # PROPOSED/ACTIVE only) AND baseline_metrics is now non-NULL.
        await _mk_alpha(session, h.id, suffix="bs2d", is_sharpe=10.0)
        await session.commit()
        await svc.mark_promoted(h.id)
        await session.commit()
        h2 = await svc.get_by_id(h.id)
        assert h2.baseline_metrics == baseline1

    @pytest.mark.asyncio
    async def test_only_prov_does_not_stamp(self, session):
        """SFX-14: if only PASS_PROVISIONAL alphas exist, baseline stays NULL."""
        from backend.services.hypothesis_service import HypothesisService

        h = await _mk_hyp(session, suffix="bs-3", status="ACTIVE")
        await _mk_alpha(
            session, h.id, suffix="bs3a",
            quality_status="PASS_PROVISIONAL", is_sharpe=1.0,
        )
        await session.commit()
        svc = HypothesisService(session)
        await svc.mark_promoted(h.id)
        await session.commit()
        h1 = await svc.get_by_id(h.id)
        assert h1.baseline_metrics is None

    @pytest.mark.asyncio
    async def test_single_pass_stamps_with_n_1(self, session):
        """Edge: a single PASS still stamps; T1 evaluator separately handles
        the n<3 small-sample skip — keep stamp logic narrow."""
        from backend.services.hypothesis_service import HypothesisService

        h = await _mk_hyp(session, suffix="bs-4", status="ACTIVE")
        await _mk_alpha(session, h.id, suffix="bs4a", is_sharpe=1.5)
        await session.commit()
        svc = HypothesisService(session)
        await svc.mark_promoted(h.id)
        await session.commit()
        h1 = await svc.get_by_id(h.id)
        assert h1.baseline_metrics is not None
        assert h1.baseline_metrics["n_alphas"] == 1

    @pytest.mark.asyncio
    async def test_savepoint_isolates_stamp_failure(self, session, monkeypatch):
        """MFX-3: a stamp exception does NOT roll back the PROMOTED UPDATE.
        We monkeypatch _stamp_baseline_if_missing to always raise and assert
        mark_promoted still returns True + status flipped."""
        from backend.services.hypothesis_service import HypothesisService

        h = await _mk_hyp(session, suffix="bs-5", status="ACTIVE")
        await _mk_alpha(session, h.id, suffix="bs5a", is_sharpe=1.0)
        await session.commit()

        svc = HypothesisService(session)

        async def boom(hid):
            raise RuntimeError("intentional test boom")

        monkeypatch.setattr(svc, "_stamp_baseline_if_missing", boom)
        ok = await svc.mark_promoted(h.id)
        await session.commit()
        assert ok is True
        refreshed = await svc.get_by_id(h.id)
        assert refreshed.status == "PROMOTED"
        assert refreshed.baseline_metrics is None  # stamp rolled back inside savepoint


# ===========================================================================
# mark_triggered (MFX-5)
# ===========================================================================


def _hit_dict(**kw):
    base = dict(
        type="dropped_sharpe_pct",
        threshold=-30.0,
        observed=-40.0,
        window_rounds=None,
        severity="orange",
        reason="r",
        hit_at=datetime.now(timezone.utc).isoformat(),
    )
    base.update(kw)
    return base


class TestMarkTriggered:
    @pytest.mark.asyncio
    async def test_first_hit_flips_flag(self, session):
        from backend.services.hypothesis_service import HypothesisService

        h = await _mk_hyp(session, suffix="mt-1", status="ACTIVE")
        await session.commit()
        svc = HypothesisService(session)
        ok = await svc.mark_triggered(h.id, hits=[_hit_dict()])
        await session.commit()
        assert ok
        ref = await svc.get_by_id(h.id)
        assert ref.is_triggered is True
        assert ref.triggered_at is not None
        assert len(ref.trigger_detail) == 1

    @pytest.mark.asyncio
    async def test_dedup_within_24h(self, session):
        from backend.services.hypothesis_service import HypothesisService

        h = await _mk_hyp(session, suffix="mt-2", status="ACTIVE")
        await session.commit()
        svc = HypothesisService(session)
        await svc.mark_triggered(h.id, hits=[_hit_dict()])
        await session.commit()
        await svc.mark_triggered(h.id, hits=[_hit_dict()])
        await session.commit()
        ref = await svc.get_by_id(h.id)
        assert len(ref.trigger_detail) == 1

    @pytest.mark.asyncio
    async def test_different_types_appended(self, session):
        from backend.services.hypothesis_service import HypothesisService

        h = await _mk_hyp(session, suffix="mt-3", status="ACTIVE")
        await session.commit()
        svc = HypothesisService(session)
        await svc.mark_triggered(
            h.id,
            hits=[
                _hit_dict(type="dropped_sharpe_pct"),
                _hit_dict(type="no_pass_in_n_rounds", window_rounds=5),
            ],
        )
        await session.commit()
        ref = await svc.get_by_id(h.id)
        assert len(ref.trigger_detail) == 2

    @pytest.mark.asyncio
    async def test_fifo_cap_applied(self, session, monkeypatch):
        from backend.services.hypothesis_service import HypothesisService
        from backend.config import settings as cfg

        monkeypatch.setattr(cfg, "TRIGGER_DETAIL_MAX_ENTRIES", 3)
        h = await _mk_hyp(session, suffix="mt-4", status="ACTIVE")
        await session.commit()
        svc = HypothesisService(session)
        # 5 distinct types — only 3 should survive cap
        await svc.mark_triggered(h.id, hits=[
            _hit_dict(type=f"t{i}", window_rounds=i) for i in range(5)
        ])
        await session.commit()
        ref = await svc.get_by_id(h.id)
        assert len(ref.trigger_detail) == 3

    @pytest.mark.asyncio
    async def test_empty_hits_is_noop(self, session):
        from backend.services.hypothesis_service import HypothesisService

        h = await _mk_hyp(session, suffix="mt-5", status="ACTIVE")
        await session.commit()
        svc = HypothesisService(session)
        ok = await svc.mark_triggered(h.id, hits=[])
        assert ok is False


# ===========================================================================
# update_thesis_score
# ===========================================================================


class TestUpdateThesisScore:
    def _score(self, **kw):
        from backend.services.hypothesis_health_service import LLMThesisScore

        base = dict(
            thesis_score=72,
            ai_feedback="ok",
            recommended_action="continue",
            reasons=["because"],
        )
        base.update(kw)
        return LLMThesisScore(**base)

    @pytest.mark.asyncio
    async def test_writes_score_and_history(self, session):
        from backend.services.hypothesis_service import HypothesisService

        h = await _mk_hyp(session, suffix="us-1", status="ACTIVE")
        await session.commit()
        svc = HypothesisService(session)
        scored_at = datetime.now(timezone.utc)
        ok = await svc.update_thesis_score(
            h.id, self._score(), scored_at=scored_at, status="ok",
        )
        await session.commit()
        assert ok
        ref = await svc.get_by_id(h.id)
        assert ref.thesis_score == 72.0
        assert ref.last_thesis_score_status == "ok"
        assert len(ref.thesis_score_history) == 1
        assert ref.thesis_score_history[0]["status"] == "ok"

    @pytest.mark.asyncio
    async def test_history_capped_at_20(self, session):
        from backend.services.hypothesis_service import HypothesisService

        h = await _mk_hyp(session, suffix="us-2", status="ACTIVE")
        await session.commit()
        svc = HypothesisService(session)
        for i in range(25):
            await svc.update_thesis_score(
                h.id, self._score(thesis_score=i),
                scored_at=datetime.now(timezone.utc) + timedelta(minutes=i),
                status="ok",
            )
            await session.commit()
        ref = await svc.get_by_id(h.id)
        assert len(ref.thesis_score_history) == 20
        # FIFO: oldest dropped — most recent score=24 still there
        assert ref.thesis_score_history[-1]["thesis_score"] == 24

    @pytest.mark.asyncio
    async def test_fallback_status_persisted(self, session):
        from backend.services.hypothesis_service import HypothesisService

        h = await _mk_hyp(session, suffix="us-3", status="ACTIVE")
        await session.commit()
        svc = HypothesisService(session)
        await svc.update_thesis_score(
            h.id, self._score(thesis_score=50),
            scored_at=datetime.now(timezone.utc),
            status="fallback_failed",
        )
        await session.commit()
        ref = await svc.get_by_id(h.id)
        assert ref.last_thesis_score_status == "fallback_failed"

    @pytest.mark.asyncio
    async def test_invalid_status_rejected(self, session):
        from backend.services.hypothesis_service import HypothesisService

        h = await _mk_hyp(session, suffix="us-4", status="ACTIVE")
        await session.commit()
        svc = HypothesisService(session)
        with pytest.raises(ValueError):
            await svc.update_thesis_score(
                h.id, self._score(), scored_at=datetime.now(timezone.utc),
                status="bogus",
            )


# ===========================================================================
# _can_call_llm (SFX-13)
# ===========================================================================


class TestCanCallLLM:
    def _svc(self, session, llm=None):
        from backend.services.hypothesis_health_service import (
            HypothesisHealthService,
        )

        return HypothesisHealthService(session, llm_service=llm)

    @pytest.mark.asyncio
    async def test_no_llm_returns_false(self, session):
        svc = self._svc(session, llm=None)
        h = SimpleNamespace(
            last_thesis_score_at=None, last_thesis_score_status=None,
        )
        ok = await svc._can_call_llm(h, datetime.now(timezone.utc))
        assert ok is False

    @pytest.mark.asyncio
    async def test_budget_exceeded(self, session):
        from backend.config import settings as cfg

        svc = self._svc(session, llm=AsyncMock())
        svc._token_used = cfg.THESIS_SCORE_PER_RUN_TOKEN_BUDGET
        h = SimpleNamespace(
            last_thesis_score_at=None, last_thesis_score_status=None,
        )
        ok = await svc._can_call_llm(h, datetime.now(timezone.utc))
        assert ok is False

    @pytest.mark.asyncio
    async def test_24h_gate_recent_ok(self, session):
        svc = self._svc(session, llm=AsyncMock())
        now = datetime.now(timezone.utc)
        h = SimpleNamespace(
            last_thesis_score_at=now - timedelta(hours=12),
            last_thesis_score_status="ok",
        )
        ok = await svc._can_call_llm(h, now)
        assert ok is False

    @pytest.mark.asyncio
    async def test_4h_backoff_recent_fallback(self, session):
        svc = self._svc(session, llm=AsyncMock())
        now = datetime.now(timezone.utc)
        h = SimpleNamespace(
            last_thesis_score_at=now - timedelta(hours=2),
            last_thesis_score_status="fallback_failed",
        )
        ok = await svc._can_call_llm(h, now)
        assert ok is False

    @pytest.mark.asyncio
    async def test_4h_backoff_passed_for_fallback(self, session):
        svc = self._svc(session, llm=AsyncMock())
        now = datetime.now(timezone.utc)
        h = SimpleNamespace(
            last_thesis_score_at=now - timedelta(hours=5),
            last_thesis_score_status="fallback_failed",
        )
        ok = await svc._can_call_llm(h, now)
        assert ok is True

    @pytest.mark.asyncio
    async def test_24h_gate_passed(self, session):
        svc = self._svc(session, llm=AsyncMock())
        now = datetime.now(timezone.utc)
        h = SimpleNamespace(
            last_thesis_score_at=now - timedelta(hours=25),
            last_thesis_score_status="ok",
        )
        ok = await svc._can_call_llm(h, now)
        assert ok is True


# ===========================================================================
# _score_with_llm_or_fallback (MFX-6 three-segment)
# ===========================================================================


class TestScoreWithLLMOrFallback:
    def _aggs(self):
        from backend.services.hypothesis_health_service import (
            HypothesisAggregates,
        )

        return HypothesisAggregates(
            hypothesis_id=1,
            related_alpha_count=5,
            current_sharpe_avg=1.2,
            current_pass_rate=0.3,
            stale_share=0.1,
            recent_rounds=[],
            baseline_metrics={"sharpe_avg": 2.0, "n_alphas": 5},
        )

    def _hyp(self):
        return SimpleNamespace(
            id=1, statement="test thesis", rationale=None, region="USA",
            kind="INVESTMENT_THESIS", target_tier=1, status="PROMOTED",
            expected_signal="momentum", confidence="medium", novelty="established",
        )

    @pytest.mark.asyncio
    async def test_success_returns_ok(self, session):
        from backend.services.hypothesis_health_service import (
            HypothesisHealthService,
        )

        llm = AsyncMock()
        llm.call.return_value = SimpleNamespace(
            success=True,
            parsed={
                "thesis_score": 80,
                "ai_feedback": "looks fine",
                "recommended_action": "monitor",
                "reasons": ["healthy enough"],
            },
            tokens_used=500,
            error=None,
        )
        svc = HypothesisHealthService(session, llm_service=llm)
        score, status = await svc._score_with_llm_or_fallback(
            self._hyp(), self._aggs(), [],
        )
        assert status == "ok"
        assert score.thesis_score == 80
        assert score.recommended_action == "monitor"
        assert svc._token_used == 500

    @pytest.mark.asyncio
    async def test_exception_returns_fallback_failed(self, session):
        from backend.services.hypothesis_health_service import (
            HypothesisHealthService,
        )

        llm = AsyncMock()
        llm.call.side_effect = ConnectionError("network down")
        svc = HypothesisHealthService(session, llm_service=llm)
        score, status = await svc._score_with_llm_or_fallback(
            self._hyp(), self._aggs(), [],
        )
        assert status == "fallback_failed"
        assert score.thesis_score == 50

    @pytest.mark.asyncio
    async def test_unparsed_returns_fallback_failed(self, session):
        from backend.services.hypothesis_health_service import (
            HypothesisHealthService,
        )

        llm = AsyncMock()
        llm.call.return_value = SimpleNamespace(
            success=True, parsed=None, tokens_used=10, error=None,
        )
        svc = HypothesisHealthService(session, llm_service=llm)
        score, status = await svc._score_with_llm_or_fallback(
            self._hyp(), self._aggs(), [],
        )
        assert status == "fallback_failed"

    @pytest.mark.asyncio
    async def test_invalid_action_returns_schema_invalid(self, session):
        from backend.services.hypothesis_health_service import (
            HypothesisHealthService,
        )

        llm = AsyncMock()
        llm.call.return_value = SimpleNamespace(
            success=True,
            parsed={
                "thesis_score": 80,
                "ai_feedback": "fine",
                "recommended_action": "quit",  # invalid
                "reasons": ["r"],
            },
            tokens_used=100,
            error=None,
        )
        svc = HypothesisHealthService(session, llm_service=llm)
        score, status = await svc._score_with_llm_or_fallback(
            self._hyp(), self._aggs(), [],
        )
        assert status == "fallback_schema_invalid"
        assert score.thesis_score == 50


# ===========================================================================
# _persist_result (SFX-10: audit row only on False→True edge)
# ===========================================================================


class TestPersistResultEdge:
    @staticmethod
    def _aggs(hid: int, current_sharpe_avg=None):
        """Build a minimal HypothesisAggregates for _persist_result tests."""
        from backend.services.hypothesis_health_service import (
            HypothesisAggregates,
        )
        return HypothesisAggregates(
            hypothesis_id=hid,
            related_alpha_count=0,
            current_sharpe_avg=current_sharpe_avg,
            current_pass_rate=None,
            stale_share=None,
            recent_rounds=[],
            baseline_metrics=None,
        )

    @pytest.mark.asyncio
    async def test_first_hit_writes_audit(self, session):
        from backend.models import HypothesisStatusTransition
        from backend.services.hypothesis_health_service import (
            HypothesisHealthService,
            TriggerHit,
        )

        h = await _mk_hyp(session, suffix="pre-1", status="ACTIVE")
        await session.commit()
        svc = HypothesisHealthService(session)
        hit = TriggerHit(
            type="t", threshold=-30.0, observed=-50.0, window_rounds=None,
            severity="orange", reason="r", hit_at=datetime.now(timezone.utc).isoformat(),
        )
        # Re-fetch so we have the up-to-date row
        h_fresh = (await session.execute(
            select(type(h)).where(type(h).id == h.id)
        )).scalar_one()
        aggs = self._aggs(h_fresh.id, current_sharpe_avg=1.23)
        await svc._persist_result(
            h_fresh, aggs, [hit], None, None, datetime.now(timezone.utc),
        )
        rows = (await session.execute(
            select(HypothesisStatusTransition).where(
                HypothesisStatusTransition.hypothesis_id == h.id,
            )
        )).scalars().all()
        assert len(rows) == 1
        assert rows[0].old_is_triggered is False
        assert rows[0].new_is_triggered is True
        # Audit's sharpe_at_transition uses fresh aggs value (P2 fix), not
        # the stale h.sharpe_avg cache.
        assert rows[0].sharpe_at_transition == pytest.approx(1.23)

    @pytest.mark.asyncio
    async def test_steady_state_no_new_audit(self, session):
        from backend.models import Hypothesis, HypothesisStatusTransition
        from backend.services.hypothesis_health_service import (
            HypothesisHealthService,
            TriggerHit,
        )

        h = await _mk_hyp(
            session, suffix="pre-2", status="ACTIVE",
            is_triggered=True,
            triggered_at=datetime.now(timezone.utc) - timedelta(days=2),
            trigger_detail=[],
        )
        await session.commit()
        svc = HypothesisHealthService(session)
        hit = TriggerHit(
            type="t2", threshold=-30.0, observed=-50.0, window_rounds=None,
            severity="orange", reason="r", hit_at=datetime.now(timezone.utc).isoformat(),
        )
        h_fresh = (await session.execute(
            select(Hypothesis).where(Hypothesis.id == h.id)
        )).scalar_one()
        aggs = self._aggs(h_fresh.id)
        await svc._persist_result(
            h_fresh, aggs, [hit], None, None, datetime.now(timezone.utc),
        )
        rows = (await session.execute(
            select(HypothesisStatusTransition).where(
                HypothesisStatusTransition.hypothesis_id == h.id,
            )
        )).scalars().all()
        assert len(rows) == 0  # SFX-10: no audit on steady-state

    @pytest.mark.asyncio
    async def test_no_hits_no_audit(self, session):
        from backend.models import HypothesisStatusTransition
        from backend.services.hypothesis_health_service import (
            HypothesisHealthService,
        )

        h = await _mk_hyp(session, suffix="pre-3", status="ACTIVE")
        await session.commit()
        svc = HypothesisHealthService(session)
        aggs = self._aggs(h.id)
        await svc._persist_result(
            h, aggs, [], None, None, datetime.now(timezone.utc),
        )
        rows = (await session.execute(
            select(HypothesisStatusTransition).where(
                HypothesisStatusTransition.hypothesis_id == h.id,
            )
        )).scalars().all()
        assert len(rows) == 0
