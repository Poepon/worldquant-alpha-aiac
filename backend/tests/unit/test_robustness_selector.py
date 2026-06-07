"""Unit tests for backend/robustness_selector.py (#39 submit selector).

Core thesis under test: a LONE-SPIKE alpha (one good sub-period, losing in the
rest) must be flagged FRAGILE even when its FULL-window Sharpe is high — that is
the OS-decay risk BRAIN's hidden-OS design makes us guard against pre-submit.
"""
import math

from backend.robustness_selector import (
    _ann_sharpe,
    subperiod_sharpes,
    max_drawdown,
    robustness_metrics,
    robustness_score,
    robustness_verdict,
    assess_from_pnl_rows,
)


def _alt(mean: float, dev: float, n: int):
    """Alternating series with population mean `mean`, population std `dev`
    (deviations ±dev) → annualised Sharpe = mean/dev·√252, deterministic."""
    return [mean + dev if i % 2 == 0 else mean - dev for i in range(n)]


# --------------------------------------------------------------------------- #
# primitives
# --------------------------------------------------------------------------- #
def test_ann_sharpe_matches_formula():
    s = _alt(1.0, 0.5, 100)            # mean 1.0, std 0.5
    expected = 1.0 / 0.5 * math.sqrt(252)
    assert math.isclose(_ann_sharpe(s), expected, rel_tol=1e-9)


def test_ann_sharpe_none_on_zero_vol():
    assert _ann_sharpe([1.0] * 50) is None       # zero variance
    assert _ann_sharpe([1.0]) is None            # < 2 obs


def test_subperiod_count_and_remainder():
    subs = subperiod_sharpes(_alt(1.0, 0.5, 240), k=6)
    assert len(subs) == 6                          # 240 / 6 = 40 each, all measurable


def test_max_drawdown_monotone_up_is_zero():
    assert max_drawdown([1.0, 2.0, 3.0]) == 0.0    # cumulative only rises
    # one drawdown of -5 then recovery
    md = max_drawdown([10.0, -5.0, -5.0, 20.0])
    assert md == -10.0                             # peak 10 → trough 0


# --------------------------------------------------------------------------- #
# the headline case: lone spike is FRAGILE despite high full Sharpe
# --------------------------------------------------------------------------- #
def test_lone_spike_is_fragile_even_with_high_full_sharpe():
    # 5 losing sub-periods + 1 huge winner.
    losing = _alt(-0.1, 0.3, 200)                  # sub-period Sharpe ≈ -5.3 (deep neg)
    spike = _alt(5.0, 0.3, 40)                      # sub-period Sharpe huge positive
    pnl = losing + spike
    m = robustness_metrics(pnl, k=6, min_overlap=200)
    assert m is not None
    # full-window Sharpe is POSITIVE (the trap) ...
    assert m["full_sharpe"] > 0
    # ... but consistency is awful: only 1/6 sub-periods positive, worst deeply neg
    assert m["frac_positive_subperiods"] <= 0.2
    assert m["min_subperiod_sharpe"] <= -1.0
    assert robustness_verdict(m) == "FRAGILE"
    assert robustness_score(m) < 0.4


def test_consistent_alpha_is_robust():
    pnl = _alt(1.0, 0.5, 240)                       # every day positive-drift, all 6 subs positive
    m = robustness_metrics(pnl, k=6, min_overlap=200)
    assert m is not None
    assert m["frac_positive_subperiods"] == 1.0
    assert m["min_subperiod_sharpe"] > 0
    assert robustness_verdict(m) == "ROBUST"
    assert robustness_score(m) > 0.9


def test_moderate_between_robust_and_fragile():
    # 5 positive sub-periods (Sharpe ≈ +0.5) + 1 mildly negative (Sharpe ≈ -0.5):
    # worst -0.5 ∈ (fragile -1.0, robust -0.1) → neither FRAGILE nor ROBUST.
    pos = _alt(0.0315, 1.0, 40)                      # Sharpe ≈ +0.50
    neg = _alt(-0.0315, 1.0, 40)                     # Sharpe ≈ -0.50
    pnl = pos * 5 + neg                              # 240 days, 6 aligned sub-periods
    m = robustness_metrics(pnl, k=6, min_overlap=200)
    assert m is not None
    assert -1.0 < m["min_subperiod_sharpe"] < -0.1   # not fragile, not robust
    assert robustness_verdict(m) == "MODERATE"


def test_robustness_score_bounds_and_midpoint():
    # min_subperiod=0, frac=0.5 → consistency .5, worst .5 → score .5
    m = {"frac_positive_subperiods": 0.5, "min_subperiod_sharpe": 0.0}
    assert robustness_score(m, worst_ref=1.0) == 0.5
    # clip: very negative worst → 0 contribution from worst
    m2 = {"frac_positive_subperiods": 1.0, "min_subperiod_sharpe": -10.0}
    sc = robustness_score(m2, worst_ref=1.0)
    assert 0.0 <= sc <= 1.0
    assert sc == 0.5                                 # 0.5·1.0 + 0.5·0.0


# --------------------------------------------------------------------------- #
# thin / degenerate
# --------------------------------------------------------------------------- #
def test_thin_series_returns_none():
    assert robustness_metrics(_alt(1.0, 0.5, 50), k=6, min_overlap=200) is None
    assert robustness_metrics([], k=6, min_overlap=200) is None


def test_zero_vol_full_window_returns_none():
    assert robustness_metrics([1.0] * 300, k=6, min_overlap=200) is None


# --------------------------------------------------------------------------- #
# assess_from_pnl_rows (the router entry point)
# --------------------------------------------------------------------------- #
def test_assess_from_pnl_rows_groups_and_classifies():
    import datetime as _dt
    base = _dt.date(2020, 1, 1)

    def rows_for(aid, series):
        return [(aid, base + _dt.timedelta(days=i), v) for i, v in enumerate(series)]

    robust = rows_for(1, _alt(1.0, 0.5, 240))
    fragile = rows_for(2, _alt(-0.1, 0.3, 200) + _alt(5.0, 0.3, 40))
    thin = rows_for(3, _alt(1.0, 0.5, 50))          # too short → absent

    out = assess_from_pnl_rows(robust + fragile + thin, k=6, min_overlap=200)
    assert set(out.keys()) == {1, 2}                 # alpha 3 skipped (thin)
    assert out[1]["robustness_verdict"] == "ROBUST"
    assert out[2]["robustness_verdict"] == "FRAGILE"
    assert out[1]["robustness_score"] > out[2]["robustness_score"]


def test_assess_empty_rows():
    assert assess_from_pnl_rows([], k=6, min_overlap=200) == {}
