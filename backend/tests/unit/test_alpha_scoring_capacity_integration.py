"""B1 R11 alpha_scoring integration with capacity_estimator (Sprint 2).

Verifies flag ON/OFF switches in:
  - evaluate_alpha_comprehensive: composite_score normalizes sum=1.0
    when flag ON, byte-identical to historical 4-dim when flag OFF
  - calculate_alpha_score: capacity bonus added when flag ON, score
    byte-identical when flag OFF
"""
from __future__ import annotations

import pytest

from backend.alpha_scoring import (
    calculate_alpha_score,
    evaluate_alpha_comprehensive,
)


def _mock_sim_result(*, turnover: float = 0.30) -> dict:
    """Minimal sim_result shape that evaluate_alpha_comprehensive accepts.

    Sufficient to compute all 4 base scores + (when capacity_score is ON)
    pull region/universe/turnover for capacity_estimator.
    """
    return {
        "region": "USA",
        "universe": "TOP3000",
        "settings": {"universe": "TOP3000"},
        "is": {
            "sharpe": 1.6,
            "fitness": 1.2,
            "turnover": turnover,
            "drawdown": 0.10,
            "margin": 0.001,
            "longCount": 800,
            "shortCount": 800,
            "checks": [],
        },
        "os": {
            "sharpe": 1.5,
            "fitness": 1.1,
            "turnover": turnover,
            "drawdown": 0.10,
        },
        "is_stats": {"sharpe": 1.6, "fitness": 1.2, "turnover": turnover},
        # backward-compat top-level keys some code paths read directly
        "sharpe": 1.6,
        "fitness": 1.2,
        "turnover": turnover,
    }


# ---------------------------------------------------------------------------
# evaluate_alpha_comprehensive
# ---------------------------------------------------------------------------

def test_evaluate_comprehensive_off_flag_baseline_unchanged(monkeypatch):
    """Flag OFF → composite_score equals the historical 4-dim formula."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_CAPACITY_SCORE", False)
    sim = _mock_sim_result()
    e = evaluate_alpha_comprehensive(sim, use_brain_checks=False)
    # Recompute the 4-dim formula directly
    expected = (
        0.40 * e.sharpe_score
        + 0.25 * e.fitness_score
        + 0.15 * e.turnover_score
        + 0.20 * e.robustness_score
    )
    assert e.composite_score == pytest.approx(expected, rel=1e-9)


def test_evaluate_comprehensive_on_flag_normalizes_sum_to_one(monkeypatch):
    """Flag ON → composite is a convex combo of base + capacity, sum=1."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_CAPACITY_SCORE", True)
    monkeypatch.setattr(settings, "CAPACITY_SCORE_WEIGHT", 0.10)

    sim = _mock_sim_result()
    e = evaluate_alpha_comprehensive(sim, use_brain_checks=False)

    base = (
        0.40 * e.sharpe_score
        + 0.25 * e.fitness_score
        + 0.15 * e.turnover_score
        + 0.20 * e.robustness_score
    )
    # base × 0.9 + cap_norm × 0.10
    from backend.services import capacity_estimator as cap
    cap_norm = cap.normalize(cap.estimate_from_alpha_dict(sim))
    expected = base * 0.90 + cap_norm * 0.10
    assert e.composite_score == pytest.approx(expected, rel=1e-9)


def test_evaluate_comprehensive_on_flag_capacity_changes_composite(monkeypatch):
    """Same 4 base scores but capacities in DIFFERENT log buckets →
    different composite (capacity term differs)."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_CAPACITY_SCORE", True)
    monkeypatch.setattr(settings, "CAPACITY_SCORE_WEIGHT", 0.10)

    # Big-capacity universe: USA TOP3000 → ~$6B → bucket 0.8
    sim_big = _mock_sim_result()
    # Small-capacity universe: USA ILLIQUID_MINVOL1M (5e6 × 500 × 0.10 = $2.5e8)
    # → bucket 0.6
    sim_small = _mock_sim_result()
    sim_small["universe"] = "ILLIQUID_MINVOL1M"
    sim_small["settings"]["universe"] = "ILLIQUID_MINVOL1M"

    e_big = evaluate_alpha_comprehensive(sim_big, use_brain_checks=False)
    e_small = evaluate_alpha_comprehensive(sim_small, use_brain_checks=False)
    # big-capacity → higher composite
    assert e_big.composite_score > e_small.composite_score


def test_evaluate_comprehensive_capacity_soft_falls_on_bad_region(monkeypatch):
    """ENABLE flag ON but sim missing region → capacity = 0, composite
    degrades to base × 0.9 (no exception)."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_CAPACITY_SCORE", True)
    monkeypatch.setattr(settings, "CAPACITY_SCORE_WEIGHT", 0.10)

    sim = _mock_sim_result()
    sim.pop("region", None)
    sim.pop("universe", None)
    sim["settings"] = {}
    e = evaluate_alpha_comprehensive(sim, use_brain_checks=False)
    base = (
        0.40 * e.sharpe_score
        + 0.25 * e.fitness_score
        + 0.15 * e.turnover_score
        + 0.20 * e.robustness_score
    )
    # Missing region → capacity=0 → composite = base*0.9 + 0 = base*0.9
    assert e.composite_score == pytest.approx(base * 0.90, rel=1e-9)


# ---------------------------------------------------------------------------
# calculate_alpha_score
# ---------------------------------------------------------------------------

def test_calculate_alpha_score_off_flag_baseline_unchanged(monkeypatch):
    """Flag OFF → score equals historical formula."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_CAPACITY_SCORE", False)
    sim = _mock_sim_result()
    s = calculate_alpha_score(sim)
    # Roughly: 0.55×1.5 + 0.25×1.6 + 0.20×1.2 = 0.825 + 0.4 + 0.24 = 1.465
    # (penalties zero since prod_corr=0, turnover 0.30 < 0.50)
    assert s == pytest.approx(1.465, rel=1e-3)


def test_calculate_alpha_score_on_flag_adds_capacity_bonus(monkeypatch):
    """Flag ON → score = OFF-score + cap_w × cap_norm."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_CAPACITY_SCORE", True)
    monkeypatch.setattr(settings, "CAPACITY_SCORE_WEIGHT", 0.10)
    sim = _mock_sim_result()
    monkeypatch.setattr(settings, "ENABLE_CAPACITY_SCORE", False)
    base = calculate_alpha_score(sim)
    monkeypatch.setattr(settings, "ENABLE_CAPACITY_SCORE", True)
    on = calculate_alpha_score(sim)
    from backend.services import capacity_estimator as cap
    cap_norm = cap.normalize(cap.estimate_from_alpha_dict(sim))
    assert on == pytest.approx(base + 0.10 * cap_norm, rel=1e-6)


def test_calculate_alpha_score_custom_weights_skips_capacity(monkeypatch):
    """When caller passes explicit weights= dict, capacity bonus is NOT
    applied (caller has full control over the formula)."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_CAPACITY_SCORE", True)
    monkeypatch.setattr(settings, "CAPACITY_SCORE_WEIGHT", 0.10)
    sim = _mock_sim_result()
    weights = {
        "test_sharpe": 0.55,
        "train_sharpe": 0.25,
        "fitness": 0.20,
        "prod_corr_penalty": 0.30,
        "turnover_penalty": 0.15,
        "investability_penalty": 0.20,
    }
    s_with_weights = calculate_alpha_score(sim, weights=weights)
    monkeypatch.setattr(settings, "ENABLE_CAPACITY_SCORE", False)
    s_baseline = calculate_alpha_score(sim, weights=weights)
    # Same since custom weights bypass capacity branch
    assert s_with_weights == pytest.approx(s_baseline, rel=1e-9)
