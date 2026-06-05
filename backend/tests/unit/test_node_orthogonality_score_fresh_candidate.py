"""node_evaluate (_evaluate_single_alpha) orthogonality_score persistence —
orthogonality-steered exploration Phase A (plan v4, 2026-06-05).

The ONLY real bug the redesign fixed: orthogonality_score was stamped on a fresh
``alpha.metrics`` copy and then CLOBBERED by the end-of-function rebuild
``alpha.metrics = {**metrics, ...}`` (evaluation.py:895) — so it never persisted,
even when self_corr was measured. The fix writes into the ``metrics`` LOCAL that
the rebuild spreads. (The earlier ``compute_max_corr_vs_pool`` helper was removed
as redundant: get_with_fallback's LOCAL tier / calc_self_corr already fetches a
fresh candidate's PnL + correlates it vs the pool — confirmed live via
``_self_corr_source=local`` on a real sharpe-2.01 candidate.)

These tests drive the REAL _evaluate_single_alpha with a fake corr_service to
pin: (1) measured self_corr → orthogonality_score = 1 - self_corr SURVIVES the
:895 rebuild into the final alpha.metrics; (2) UNKNOWN self_corr → not recorded.
"""
import pytest

from backend.agents.graph.nodes.evaluation import (
    _evaluate_single_alpha,
    _EvalCtx,
    CorrSource,
)
from backend.agents.graph.state import AlphaCandidate, MiningState


class _FakeBrain:
    async def check_correlation(self, alpha_id, check_type="PROD"):
        return {"status_code": 200, "data": {"max": 0.1}}


class _FakeCorrSvc:
    """Returns a fixed (corr, source) from get_with_fallback — mimics the LOCAL
    tier (calc_self_corr) having measured the candidate vs the pool."""

    def __init__(self, corr, source):
        self._corr = corr
        self._source = source

    async def get_with_fallback(self, alpha_id, region="USA"):
        return (self._corr, self._source)


def _mk_alpha(sharpe=1.8, fitness=1.5, turnover=0.2, alpha_id="fresh-1"):
    checks = [
        {"name": "LOW_SHARPE", "result": "PASS", "limit": 1.25, "value": sharpe},
        {"name": "LOW_FITNESS", "result": "PASS", "limit": 1.0, "value": fitness},
        {"name": "HIGH_TURNOVER", "result": "PASS", "limit": 0.7, "value": turnover},
        {"name": "LOW_TURNOVER", "result": "PASS", "limit": 0.01, "value": turnover},
    ]
    a = AlphaCandidate(
        expression=f"ts_rank(close, 20) /* {alpha_id} */",
        is_simulated=True, simulation_success=True, alpha_id=alpha_id,
        metrics={"sharpe": sharpe, "fitness": fitness, "turnover": turnover,
                 "returns": 0.18, "drawdown": 0.05, "checks": checks,
                 "can_submit": True},
    )
    a.quality_status = "PENDING"
    return a


def _mk_ctx(corr_svc):
    state = MiningState(task_id=1, region="USA", universe="TOP3000",
                        dataset_id="ds1", pending_alphas=[], hypotheses=[], fields=[])
    return _EvalCtx(
        state=state, brain=_FakeBrain(), correlation_service=corr_svc,
        node_name="evaluate",
        sharpe_min=1.5, fitness_min=1.2, turnover_min=0.01, turnover_max=0.7,
        max_correlation=0.7, check_self_corr=True, check_concentrated=False,
        prov_sharpe_min=1.25, prov_fitness_min=1.0, prov_turnover_min=0.01,
        prov_turnover_max=0.7, score_pass_threshold=0.8,
        score_optimize_threshold=0.3, corr_check_threshold=0.0,
    )


@pytest.mark.asyncio
async def test_measured_self_corr_persists_through_metrics_rebuild():
    """The clobber-fix regression guard: a MEASURED self_corr (LOCAL) →
    orthogonality_score = 1 - self_corr must SURVIVE the end-of-function
    alpha.metrics = {**metrics, ...} rebuild (:895). Before the fix it was
    written to a soon-clobbered copy and never persisted."""
    svc = _FakeCorrSvc(corr=0.3, source=CorrSource.LOCAL)
    alpha = _mk_alpha()
    await _evaluate_single_alpha(alpha, _mk_ctx(svc))
    # 1 - 0.3 = 0.7 — present on the FINAL (rebuilt) alpha.metrics, not clobbered
    assert alpha.metrics.get("orthogonality_score") == pytest.approx(0.7, abs=1e-6)


@pytest.mark.asyncio
async def test_unknown_self_corr_records_nothing():
    """UNKNOWN (PnL-not-ready / stale cache) → no orthogonality_score (the 0.0
    default would falsely read as fully orthogonal)."""
    svc = _FakeCorrSvc(corr=None, source=CorrSource.UNKNOWN)
    alpha = _mk_alpha()
    await _evaluate_single_alpha(alpha, _mk_ctx(svc))
    assert "orthogonality_score" not in (alpha.metrics or {})
