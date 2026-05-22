"""Tests for the RD-Agent CoSTEER loop-closure fixes (2026-05-22).

Breaks fixed:
  1+3 (same root): _node_hypothesis_inject_consumed left current_hypothesis_id
      None → mutated-hypothesis alphas were unlinked (0/10108 referenced
      IMPROVEMENT_RULE) AND the next mutation had no parent (chain depth ≤1).
      Fix propagates consumed['hypothesis_id'] → current_hypothesis_id(s).
  2: r1b_retry_log.outcome was never filled (355/355 pending). New
     reconcile_r1b_outcomes matches log rows to the alphas they produced.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from backend.tasks.r1b_outcome_reconcile import _outcome_from_alphas, _reconcile_async


# =============================================================================
# Break 1+3 — inject path propagates the mutated hypothesis id
# =============================================================================
@pytest.mark.asyncio
class TestInjectConsumedPropagatesId:
    async def _run(self, consumed):
        from backend.agents.graph.nodes import generation as gen
        state = SimpleNamespace(dataset_id="pv1", task_id=1, region="USA")
        with patch.object(gen, "record_trace", new=AsyncMock(return_value={})):
            return await gen._node_hypothesis_inject_consumed(
                state=state, consumed=consumed, config=None,
                trace_service=None, start_time=0.0, node_name="hypothesis",
            )

    async def test_propagates_hypothesis_id(self):
        out = await self._run({
            "statement": "momentum reversal on fundamentals",
            "rationale": "r", "hypothesis_id": 4242, "selected_datasets": ["pv1"],
        })
        # The mutated hypothesis id flows into the round so its alphas link to
        # it (Break 3) and a further mutation finds a parent (Break 1).
        assert out["current_hypothesis_id"] == 4242
        assert out["current_hypothesis_ids"] == [4242]
        assert out["hypotheses"][0]["statement"].startswith("momentum")

    async def test_no_id_stays_none(self):
        # INSERT had failed upstream → no id → graceful None (legacy behavior).
        out = await self._run({"statement": "x", "rationale": "r"})
        assert out["current_hypothesis_id"] is None
        assert out["current_hypothesis_ids"] == []


# =============================================================================
# Break 2 — outcome reconciliation
# =============================================================================
class TestOutcomeFromAlphas:
    def test_empty_is_pending(self):
        assert _outcome_from_alphas([]) == (None, None)

    def test_pass_with_max_sharpe(self):
        assert _outcome_from_alphas([("FAIL", 0.1, {}), ("PASS", 1.2, {}), ("PASS", 1.8, {})]) == ("pass", 1.8)

    def test_fail_when_no_pass(self):
        assert _outcome_from_alphas([("FAIL", None, {})]) == ("fail", None)

    def test_presim_skip_excluded_stays_pending(self):
        # Not a real sim → no signal → pending (reconciled later).
        assert _outcome_from_alphas([("PASS", 2.0, {"_pre_brain_skip": True})]) == (None, None)

    def test_provisional_counts_as_pass(self):
        assert _outcome_from_alphas([("PASS_PROVISIONAL", 1.3, {})]) == ("pass", 1.3)


class _SharedSessionCM:
    def __init__(self, s):
        self._s = s

    async def __aenter__(self):
        return self._s

    async def __aexit__(self, *e):
        return False


@pytest_asyncio.fixture
def session_factory(db_session):
    return lambda: _SharedSessionCM(db_session)


_LOG_ID = [0]


async def _mk_log(db, **kw):
    # BigInteger PK does not autoincrement on sqlite (only INTEGER PK does);
    # production PG uses a sequence. Assign an explicit id for the fixture.
    from backend.models.r1b_retry import R1bRetryLog
    _LOG_ID[0] += 1
    row = R1bRetryLog(id=_LOG_ID[0], outcome="pending", **kw)
    db.add(row)
    await db.flush()
    return row


async def _mk_alpha(db, **kw):
    from backend.models import Alpha
    kw.setdefault("quality_status", "PENDING")
    a = Alpha(region="USA", universe="TOP3000", expression=kw.pop("expression", "x"),
              human_feedback="NONE", **kw)
    db.add(a)
    await db.flush()
    return a


@pytest.mark.asyncio
class TestReconcile:
    async def test_mutate_reconciled_via_hypothesis_id(self, db_session, session_factory):
        from backend.models import Alpha, Hypothesis
        h = Hypothesis(region="USA", statement="mutated", kind="IMPROVEMENT_RULE", status="PROPOSED")
        db_session.add(h)
        await db_session.flush()
        log = await _mk_log(
            db_session, task_id=1, round_idx=2, attempt_type="mutate_hyp",
            original_expression_hash="orig", new_hypothesis_id=h.id,
        )
        # Two real-sim alphas linked to the mutated hypothesis: one PASS.
        await _mk_alpha(db_session, hypothesis_id=h.id, quality_status="FAIL", is_sharpe=0.2, metrics={})
        a2 = await _mk_alpha(db_session, hypothesis_id=h.id, is_sharpe=1.6, metrics={})
        a2.quality_status = "PASS"
        await db_session.commit()

        out = await _reconcile_async(max_rows=100, session_factory=session_factory)
        assert out["pass"] == 1
        refreshed = (await db_session.execute(
            select(type(log)).where(type(log).id == log.id)
        )).scalar_one()
        assert refreshed.outcome == "pass"
        assert refreshed.outcome_sharpe == 1.6

    async def test_retry_reconciled_via_expression(self, db_session, session_factory):
        log = await _mk_log(
            db_session, task_id=7, round_idx=1, attempt_type="retry_impl",
            original_expression_hash="orig2", new_expression="rank(ts_zscore(close,60))",
        )
        a = await _mk_alpha(db_session, task_id=7, expression="rank(ts_zscore(close,60))",
                            is_sharpe=None, metrics={})
        a.quality_status = "FAIL"
        await db_session.commit()

        out = await _reconcile_async(max_rows=100, session_factory=session_factory)
        assert out["fail"] == 1

    async def test_retry_fail_via_alpha_failures_fallback(self, db_session, session_factory):
        from backend.models import AlphaFailure
        log = await _mk_log(
            db_session, task_id=9, round_idx=1, attempt_type="retry_impl",
            original_expression_hash="o4", new_expression="zscore(returns)",
        )
        # No alpha in `alphas`; the rewrite BRAIN-failed → alpha_failures.
        db_session.add(AlphaFailure(
            task_id=9, expression="zscore(returns)", error_type="SIMULATION_ERROR",
        ))
        await db_session.commit()
        out = await _reconcile_async(max_rows=100, session_factory=session_factory)
        assert out["fail"] == 1

    async def test_presim_skip_failure_not_counted(self, db_session, session_factory):
        from backend.models import AlphaFailure
        await _mk_log(
            db_session, task_id=11, round_idx=1, attempt_type="retry_impl",
            original_expression_hash="o5", new_expression="rank(open)",
        )
        # Only a PRESIM_SKIP failure → never hit BRAIN → stay pending.
        db_session.add(AlphaFailure(
            task_id=11, expression="rank(open)", error_type="PRESIM_SKIP",
        ))
        await db_session.commit()
        out = await _reconcile_async(max_rows=100, session_factory=session_factory)
        assert out["reconciled"] == 0 and out["still_pending"] == 1

    async def test_unsimulated_stays_pending(self, db_session, session_factory):
        from backend.models import Hypothesis
        h = Hypothesis(region="USA", statement="m2", kind="IMPROVEMENT_RULE", status="PROPOSED")
        db_session.add(h)
        await db_session.flush()
        log = await _mk_log(
            db_session, task_id=1, round_idx=1, attempt_type="mutate_hyp",
            original_expression_hash="o3", new_hypothesis_id=h.id,
        )
        # Only a pre-sim-skip alpha → no real sim → must stay pending.
        await _mk_alpha(db_session, hypothesis_id=h.id, quality_status="FAIL",
                        metrics={"_pre_brain_skip": True})
        await db_session.commit()

        out = await _reconcile_async(max_rows=100, session_factory=session_factory)
        assert out["reconciled"] == 0 and out["still_pending"] == 1
