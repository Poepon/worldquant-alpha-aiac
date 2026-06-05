"""node_evaluate (_evaluate_single_alpha) orthogonality_score wiring for FRESH
mined candidates — orthogonality-steered exploration Phase A (plan v4,
2026-06-05).

The shadow run recorded ZERO orthogonality scores because the only writer was
``1 - measured self_corr`` and ``get_with_fallback`` is UNKNOWN for every fresh
alpha (not in the local cache; BRAIN SELF async-PENDING). Redesign: when
self_corr is UNKNOWN, the flag is ON, and the alpha clears ``sharpe_min``, FETCH
the candidate PnL + correlate vs the cached pool (``compute_max_corr_vs_pool``)
and record ``orthogonality_score = 1 - max|corr|``.

Drives ``_evaluate_single_alpha`` directly with a fake brain + fake
correlation_service (this path is DB-free) to pin: (1) flag ON + UNKNOWN +
sharpe-pass → score recorded from the pool-corr helper; (2) flag OFF + UNKNOWN →
NOT recorded (byte-for-byte legacy); (3) below sharpe_min → helper not called
(cost + anti-gaming gate).
"""
import pytest

from backend.agents.graph.nodes import evaluation as ev
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
    def __init__(self, max_corr=0.4):
        self._max_corr = max_corr
        self.calls = []

    async def get_with_fallback(self, alpha_id, region="USA"):
        return (None, CorrSource.UNKNOWN)  # fresh candidate → never measured

    async def compute_max_corr_vs_pool(self, alpha_id, region, *, min_overlap_days=60):
        self.calls.append((alpha_id, region))
        return self._max_corr


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


def _mk_ctx(corr_svc, *, sharpe_min=1.5):
    state = MiningState(task_id=1, region="USA", universe="TOP3000",
                        dataset_id="ds1", pending_alphas=[], hypotheses=[], fields=[])
    return _EvalCtx(
        state=state, brain=_FakeBrain(), correlation_service=corr_svc,
        node_name="evaluate",
        sharpe_min=sharpe_min, fitness_min=1.2, turnover_min=0.01, turnover_max=0.7,
        max_correlation=0.7, check_self_corr=True, check_concentrated=False,
        prov_sharpe_min=1.25, prov_fitness_min=1.0, prov_turnover_min=0.01,
        prov_turnover_max=0.7, score_pass_threshold=0.8,
        score_optimize_threshold=0.3, corr_check_threshold=0.0,
    )


@pytest.fixture
def _flag(monkeypatch):
    def _set(val):
        monkeypatch.setattr(
            ev.settings, "ENABLE_ORTHOGONAL_PROMPT_STEERING", val, raising=False)
    return _set


@pytest.mark.asyncio
async def test_flag_on_unknown_self_corr_records_score_from_pool(_flag):
    """The shadow-run fix: flag ON + UNKNOWN self_corr + sharpe-pass → the helper
    runs and orthogonality_score = 1 - max|corr| lands on the fresh candidate."""
    _flag(True)
    svc = _FakeCorrSvc(max_corr=0.4)
    alpha = _mk_alpha()
    await _evaluate_single_alpha(alpha, _mk_ctx(svc))
    assert svc.calls == [("fresh-1", "USA")], "helper must run for the fresh candidate"
    assert alpha.metrics.get("orthogonality_score") == pytest.approx(0.6, abs=1e-6)


@pytest.mark.asyncio
async def test_flag_off_unknown_self_corr_records_nothing(_flag):
    """Flag OFF → only the measured-self_corr writer → UNKNOWN records nothing
    (byte-for-byte legacy; the helper is never called)."""
    _flag(False)
    svc = _FakeCorrSvc(max_corr=0.4)
    alpha = _mk_alpha()
    await _evaluate_single_alpha(alpha, _mk_ctx(svc))
    assert svc.calls == [], "helper must NOT run when flag OFF"
    assert "orthogonality_score" not in (alpha.metrics or {})


@pytest.mark.asyncio
async def test_flag_on_below_sharpe_min_skips_helper(_flag):
    """Flag ON but sharpe < sharpe_min → helper not called: orthogonality is
    measured only among PROMISING alphas (cost + anti-gaming)."""
    _flag(True)
    svc = _FakeCorrSvc(max_corr=0.4)
    alpha = _mk_alpha(sharpe=1.0)  # < sharpe_min 1.5
    await _evaluate_single_alpha(alpha, _mk_ctx(svc, sharpe_min=1.5))
    assert svc.calls == [], "helper must be gated on sharpe>=sharpe_min"
    assert "orthogonality_score" not in (alpha.metrics or {})
