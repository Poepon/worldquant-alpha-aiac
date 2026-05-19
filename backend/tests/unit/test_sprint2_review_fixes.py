"""Sprint 2 F1-F11 review-fix verification tests (2026-05-20).

3-round fresh agent review (R1 correctness + R2 failure-mode + R3
integration) found 11 MUST issues. These tests pin each fix.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# F1: factor_lens residual_sharpe uses intercept (not mean(residuals))
# ---------------------------------------------------------------------------

def test_f1_residual_sharpe_uses_intercept_not_mean():
    """Previous code: residual_sharpe = mean(residuals) / std × √252.
    With intercept column, sum(residuals) ≡ 0 → mean ≡ 0 → metric useless.
    Fix: use intercept × √252 / std (annualized Jensen's alpha)."""
    from backend.services import factor_lens_service as fls

    # Construct an alpha with known intercept 0.001 + factor exposure
    n = 300
    dates = pd.date_range("2023-01-01", periods=n, freq="B")
    rng = np.random.default_rng(123)
    factor_returns = pd.DataFrame(
        {
            "size": rng.normal(0, 0.01, n),
            "value": rng.normal(0, 0.01, n),
            "momentum": rng.normal(0, 0.01, n),
            "quality": rng.normal(0, 0.01, n),
            "low_vol": rng.normal(0, 0.01, n),
        },
        index=dates,
    )
    # alpha = 0.001 daily drift + 1.5 × momentum + 0.5 × value + noise
    drift = 0.001
    noise = pd.Series(rng.normal(0, 0.005, n), index=dates)
    alpha = (
        drift
        + 1.5 * factor_returns["momentum"]
        + 0.5 * factor_returns["value"]
        + noise
    )

    res = fls.decompose(alpha, factor_returns)

    # intercept ≈ 0.001 (regression of construction)
    assert res.factor_exposures["_intercept"] == pytest.approx(0.001, abs=2e-4)
    # residual_sharpe = intercept × √252 / std ≈ 0.001 × 15.87 / 0.005 ≈ 3.17
    expected_sharpe = 0.001 * np.sqrt(252) / 0.005
    assert res.residual_sharpe == pytest.approx(expected_sharpe, rel=0.2)
    # Critical: residual_sharpe is NOT zero (the old bug)
    assert abs(res.residual_sharpe) > 0.5


def test_f1_zero_intercept_alpha_residual_sharpe_near_zero():
    """Alpha that's purely a factor combination + small noise (zero intercept)
    should give residual_sharpe near 0 — confirming intercept drives result."""
    from backend.services import factor_lens_service as fls
    n = 300
    dates = pd.date_range("2023-01-01", periods=n, freq="B")
    rng = np.random.default_rng(7)
    fdf = pd.DataFrame(
        {f: rng.normal(0, 0.01, n) for f in ("size", "value", "momentum", "quality", "low_vol")},
        index=dates,
    )
    alpha = 2.0 * fdf["momentum"] + 1.0 * fdf["value"] + pd.Series(rng.normal(0, 0.005, n), index=dates)
    res = fls.decompose(alpha, fdf)
    # intercept ≈ 0 (no drift in construction)
    assert abs(res.factor_exposures["_intercept"]) < 5e-4
    # residual_sharpe ≈ 0 → small
    assert abs(res.residual_sharpe) < 1.0


# ---------------------------------------------------------------------------
# F2: Alpha.capacity_usd_estimate now persists via values_dict
# ---------------------------------------------------------------------------

def test_f2_persistence_values_dict_pulls_capacity_from_metrics():
    """The B1 R11 stamp lives in alpha.metrics['capacity_usd_estimate'] after
    _evaluate_single_alpha. Persistence path now promotes that into the
    capacity_usd_estimate column. Source verifies the change."""
    import inspect
    from backend.agents.graph.nodes import persistence
    src = inspect.getsource(persistence)
    assert "capacity_usd_estimate=" in src, "persistence INSERT must include capacity_usd_estimate"
    assert "alpha.metrics.get(\"capacity_usd_estimate\")" in src, (
        "should pull from metrics dict (where evaluation stamps it)"
    )


# ---------------------------------------------------------------------------
# F3: alpha_scoring builds capacity lookup with explicit region
# ---------------------------------------------------------------------------

def test_f3_alpha_scoring_capacity_uses_explicit_region(monkeypatch):
    """sim_result has no top-level region — alpha_scoring must pass the
    function's region param + dig universe out of settings."""
    from backend.alpha_scoring import evaluate_alpha_comprehensive
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_CAPACITY_SCORE", True)
    monkeypatch.setattr(settings, "CAPACITY_SCORE_WEIGHT", 0.10)

    # BRAIN-shape sim_result: region only inside settings sub-dict
    sim = {
        "settings": {"universe": "TOP3000", "region": "USA"},
        "is": {
            "sharpe": 1.6,
            "fitness": 1.2,
            "turnover": 0.30,
            "drawdown": 0.10,
        },
        "os": {"sharpe": 1.5, "fitness": 1.1, "turnover": 0.30},
        # NOTE: no top-level region — would have given cap_norm=0 pre-fix
    }
    e = evaluate_alpha_comprehensive(sim, region="USA", use_brain_checks=False)

    # base composite × 0.9 + cap_norm × 0.10. cap_norm should be nontrivial
    # (USA TOP3000 lands in bucket 0.8) → composite > base × 0.9.
    base = (
        0.40 * e.sharpe_score + 0.25 * e.fitness_score
        + 0.15 * e.turnover_score + 0.20 * e.robustness_score
    )
    # If F3 fix didn't work: composite would equal base × 0.9 exactly.
    # With fix: composite = base × 0.9 + 0.8 × 0.10 = base × 0.9 + 0.08
    assert e.composite_score > base * 0.9 + 0.05


# ---------------------------------------------------------------------------
# F4: R13 mode_used filter is positive (only "ols_daily" passes)
# ---------------------------------------------------------------------------

def test_f4_factor_lens_empty_reasons_not_treated_as_zero_residual():
    """Every empty-residual reason returns mode_used != 'ols_daily'.
    Evaluation node must filter on positive form, not enumerated negatives."""
    import inspect
    from backend.agents.graph.nodes import evaluation as ev
    src = inspect.getsource(ev)
    # Positive filter present
    assert '_residual.mode_used != "ols_daily"' in src, (
        "R13 wire must use positive filter (only 'ols_daily' passes)"
    )


# ---------------------------------------------------------------------------
# F5: list([] or None) silent kill guard
# ---------------------------------------------------------------------------

def test_f5_factor_lens_factors_none_safety():
    """Old code did `list([] or None)` → TypeError. Fix uses explicit
    truthiness check."""
    import inspect
    from backend.agents.graph.nodes import evaluation as ev
    src = inspect.getsource(ev)
    # The dangerous pattern should be gone
    assert 'list(getattr(settings, "FACTOR_LENS_FACTORS", []) or None)' not in src
    # The fix pattern present
    assert "_r13_factors_raw" in src
    assert "list(_r13_factors_raw) if _r13_factors_raw else None" in src


# ---------------------------------------------------------------------------
# F6: BRAIN_AUTH_CIRCUIT short-circuit in R13 loop
# ---------------------------------------------------------------------------

def test_f6_r13_loop_checks_brain_auth_circuit():
    """R13 loop must break on BRAIN_AUTH_CIRCUIT.is_open() to avoid burning
    ~100min retry latency on auth-drop."""
    import inspect
    from backend.agents.graph.nodes import evaluation as ev
    src = inspect.getsource(ev)
    assert "BRAIN_AUTH_CIRCUIT" in src, "must import + check circuit breaker"
    assert "BRAIN_AUTH_CIRCUIT.is_open()" in src
    assert "_r13_circuit_breaks" in src or "circuit_break" in src.lower()


# ---------------------------------------------------------------------------
# F7: family_hard_ban coverage guard for missing-PnL members
# ---------------------------------------------------------------------------

def test_f7_hard_ban_low_coverage_skips_bucket():
    """Bucket where >30% of members lack PnL data should be SKIPPED, not
    silently letting them all bypass the ban."""
    from backend.family_classifier import apply_family_hard_ban

    @dataclass
    class _A:
        alpha_id: str
        expression: str
        metrics: Dict[str, Any] = field(default_factory=dict)
        quality_status: Optional[str] = "PENDING"

    a1 = _A("a1", "ts_rank(close, 60)", metrics={"sharpe": 1.8})
    a2 = _A("a2", "ts_rank(volume, 60)", metrics={"sharpe": 1.5})
    a3 = _A("a3", "ts_rank(returns, 60)", metrics={"sharpe": 1.4})
    # Matrix has only a1 — a2, a3 missing
    corr = pd.DataFrame(1.0, index=["a1"], columns=["a1"])

    bans = apply_family_hard_ban(
        [a1, a2, a3], pnl_corr_matrix=corr, threshold=0.65,
        min_coverage_ratio=0.7,
    )
    # 1/3 covered, below 0.7 → skip bucket → no bans
    assert bans == []


def test_f7_hard_ban_coverage_disabled_when_ratio_zero():
    """min_coverage_ratio=0 disables the guard (back-compat for existing tests)."""
    from backend.family_classifier import apply_family_hard_ban

    @dataclass
    class _A:
        alpha_id: str
        expression: str
        metrics: Dict[str, Any] = field(default_factory=dict)
        quality_status: Optional[str] = "PENDING"

    a1 = _A("a1", "ts_rank(close, 60)", metrics={"sharpe": 1.8})
    a2 = _A("a2", "ts_rank(volume, 60)", metrics={"sharpe": 1.5})
    corr = pd.DataFrame({"a1": [1.0, 0.80], "a2": [0.80, 1.0]}, index=["a1", "a2"])
    # Coverage 2/2 = 100% > anything → ban happens
    bans = apply_family_hard_ban(
        [a1, a2], pnl_corr_matrix=corr, threshold=0.65, min_coverage_ratio=0,
    )
    assert bans == [1]


# ---------------------------------------------------------------------------
# F8: family_hard_ban groups by (pillar, family_signature), not sig alone
# ---------------------------------------------------------------------------

def test_f8_hard_ban_different_pillars_not_cross_banned():
    """Two alphas with identical op-sequence but different pillars should
    NOT be cross-pillar-banned (R10 design: group by pillar+sig)."""
    from backend.family_classifier import apply_family_hard_ban

    @dataclass
    class _A:
        alpha_id: str
        expression: str
        metrics: Dict[str, Any] = field(default_factory=dict)
        quality_status: Optional[str] = "PENDING"

    # Both have ts_rank → same family_signature
    a1 = _A("a1", "ts_rank(close, 60)", metrics={"sharpe": 1.8, "pillar": "momentum"})
    a2 = _A("a2", "ts_rank(close, 60)", metrics={"sharpe": 1.5, "pillar": "mean_reversion"})
    corr = pd.DataFrame({"a1": [1.0, 0.95], "a2": [0.95, 1.0]}, index=["a1", "a2"])

    bans = apply_family_hard_ban(
        [a1, a2], pnl_corr_matrix=corr, threshold=0.65, min_coverage_ratio=0,
    )
    # Different pillars → different buckets → no ban
    assert bans == []


# ---------------------------------------------------------------------------
# F9: r10v2_pnl_corr_matrix field declared on MiningState
# ---------------------------------------------------------------------------

def test_f9_mining_state_has_r10v2_pnl_corr_matrix_field():
    """Sprint 3 follow-up wire targets this field. Pre-declare it on the
    Pydantic model so the writer doesn't fight validate_assignment."""
    from backend.agents.graph.state import MiningState
    fields = MiningState.model_fields
    assert "r10v2_pnl_corr_matrix" in fields


# ---------------------------------------------------------------------------
# F10: calibrate_r10 SQL does NOT reference non-existent column
# ---------------------------------------------------------------------------

def test_f10_calibrate_script_does_not_select_family_signature_column():
    """alphas has no family_signature column. The SQL must NOT reference it."""
    import inspect
    from scripts import calibrate_r10_pairwise_corr as script
    src = inspect.getsource(script.load_top_n_pass_alphas)
    # No SQL column reference
    assert "family_signature IS NOT NULL" not in src
    assert "SELECT id, alpha_id, is_sharpe, expression, family_signature" not in src
    # Python-side derive present
    assert "_fam_sig" in src


# ---------------------------------------------------------------------------
# F11: normalize docstring matches actual 6-band behavior
# ---------------------------------------------------------------------------

def test_f11_normalize_docstring_acknowledges_6_bands():
    from backend.services.capacity_estimator import normalize
    doc = normalize.__doc__ or ""
    # Old docstring claimed "5 log buckets" + 0.25 spacing
    assert "5 log buckets" not in doc or "6 bands" in doc
    # New docstring mentions the 0.2 spacing or 6 bands explicitly
    assert "6 bands" in doc or "0.2" in doc


# ---------------------------------------------------------------------------
# F13: R13 hard mode goes through finalize pass (stamp-only inline)
# ---------------------------------------------------------------------------

def test_f13_r13_hard_mode_uses_finalize_pass():
    """R13 hard mode no longer transitions quality_status inline — same
    stamp-only pattern as B3 R10 refactor."""
    import inspect
    from backend.agents.graph.nodes import evaluation as ev
    src = inspect.getsource(ev)
    assert "_r13_hard_failed" in src
    assert "R13-finalize" in src or "_r13_finalize_count" in src


# ---------------------------------------------------------------------------
# F14: snapshot probe cache prevents BRAIN PnL fetch when no snapshot
# ---------------------------------------------------------------------------

def test_f14_r13_snapshot_probe_cache_present():
    """When no parquet exists for a region, R13 should skip the BRAIN
    PnL fetch entirely (not pay 0.5-1s × N latency for nothing)."""
    import inspect
    from backend.agents.graph.nodes import evaluation as ev
    src = inspect.getsource(ev)
    assert "_has_snapshot" in src
    assert "_r13_snapshot_probe" in src
