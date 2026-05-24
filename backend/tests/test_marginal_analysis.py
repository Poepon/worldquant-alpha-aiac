"""Unit tests for analyze_marginal_contribution (multi-dimensional, 2026-05-24).

The recommendation is a weighted scorecard across return + risk dimensions
(positive AND negative), NOT a Sharpe-only gate. Scales are calibrated from the
live |Δ| distribution; pnl_norm was dropped (collinear with returns) and
recent_yearly_sharpe is display+guardrail only (weight 0, collinear with sharpe).
These tests pin: a diversifier that lifts returns/margin + lowers drawdown is not
blanket-SKIPped, positive Sharpe cannot override severe risk/cost, guardrails only
DOWNGRADE (never upgrade), and neutral dims abstain from the composite.
"""
import pytest

from backend.marginal_analysis import (
    analyze_marginal_contribution,
    recent_yearly_sharpe_delta,
)


def _rec(deltas, **kw):
    return analyze_marginal_contribution(deltas, **kw)


def test_good_diversifier_submits_despite_negative_sharpe():
    """Δsharpe slightly negative (dilution) but returns/margin up + drawdown down
    → multi-dim SUBMIT (the core fix vs the old Sharpe-led SKIP)."""
    a = _rec({
        "sharpe": -0.03, "fitness": 0.02, "returns": 0.013, "margin": 0.0003,
        "drawdown": -0.003, "turnover": 0.004,
    }, merged={"sharpe": 3.16})
    assert a["recommendation"] == "SUBMIT"
    assert a["composite_score"] > 0
    assert "sharpe" in {n["metric"] for n in a["negatives"]}  # surfaced, not hidden
    assert "returns" in {p["metric"] for p in a["positives"]}


def test_all_negative_skips():
    a = _rec({
        "sharpe": -0.05, "fitness": -0.04, "returns": -0.02, "margin": -0.0005,
        "drawdown": 0.004, "turnover": 0.03,
    })
    assert a["recommendation"] == "SKIP"
    assert a["composite_score"] < 0


def test_positive_sharpe_cannot_override_severe_risk_blowup():
    """#4: Δsharpe positive but drawdown + turnover blow up → guardrail caps to at
    most NEUTRAL (positive must NOT silently SUBMIT over severe risk)."""
    a = _rec({
        "sharpe": 0.04, "fitness": 0.02, "returns": 0.005,
        "drawdown": 0.02, "turnover": 0.08,
    })
    assert a["recommendation"] != "SUBMIT"
    assert any("风险" in g or "成本" in g for g in a["guardrails"])


def test_routine_turnover_increase_does_not_trip_risk_guardrail():
    """Calibration regression: a median-ish +0.02 turnover increase must NOT trip
    the 'severe cost' guardrail (the old -0.7/scale-0.03 fired at the median)."""
    a = _rec({"sharpe": 0.10, "returns": 0.003, "turnover": 0.022, "drawdown": -0.002})
    assert not any("风险" in g or "成本" in g for g in a["guardrails"])
    assert a["recommendation"] == "SUBMIT"


def test_return_dilution_flags_and_caps():
    """Strong return dilution raises the guardrail. When the composite is already
    negative → SKIP (composite-driven). When other dims keep the composite
    positive, the guardrail caps it to NEUTRAL (never auto-SUBMIT a money-diluter,
    but don't hard-SKIP an alpha that lifts other dimensions)."""
    a = _rec({"sharpe": 0.05, "returns": -0.02})  # composite negative → SKIP
    assert a["recommendation"] == "SKIP"
    assert any("收益" in g for g in a["guardrails"])
    # strong +sharpe + lower drawdown keep composite positive, but return dilution
    # caps the verdict to NEUTRAL rather than SUBMIT
    b = _rec({"sharpe": 0.12, "drawdown": -0.006, "returns": -0.006})
    assert b["composite_score"] > 0
    assert b["recommendation"] == "NEUTRAL"
    assert any("收益" in g for g in b["guardrails"])


def test_recent_yearly_decay_caps_to_neutral():
    """Overall positives but recent-year marginal sharpe decaying → cannot SUBMIT.
    recent_yearly is weight 0 (abstains from composite) yet drives the guardrail."""
    a = _rec({"sharpe": 0.04, "returns": 0.02, "recent_yearly_sharpe": -0.08})
    assert a["recommendation"] != "SUBMIT"
    assert any("衰减" in g for g in a["guardrails"])


def test_guardrail_never_upgrades():
    """Guardrails only DOWNGRADE: a base-SKIP alpha + a NEUTRAL-cap guardrail
    (yearly decay) must stay SKIP, not rise to NEUTRAL."""
    a = _rec({"sharpe": -0.05, "returns": -0.02, "recent_yearly_sharpe": -0.08})
    assert a["recommendation"] == "SKIP"


def test_neutral_dim_abstains_from_composite():
    """A dim with |normalized| below the noise floor must not change the composite
    (it neither drags nor lifts — it abstains)."""
    base = _rec({"sharpe": 0.05, "returns": 0.02})
    with_neutral = _rec({"sharpe": 0.05, "returns": 0.02, "fitness": 0.005})
    assert with_neutral["composite_score"] == base["composite_score"]
    assert "fitness" in {n["metric"] for n in with_neutral["neutrals"]}


def test_scorecard_splits_positive_and_negative():
    a = _rec({"sharpe": -0.03, "returns": 0.02, "drawdown": -0.003, "turnover": 0.05})
    assert {p["metric"] for p in a["positives"]} >= {"returns", "drawdown"}
    assert {n["metric"] for n in a["negatives"]} >= {"sharpe", "turnover"}


def test_magnitude_is_preserved_in_composite():
    small = _rec({"sharpe": 0.05, "returns": 0.003})["composite_score"]
    big = _rec({"sharpe": 0.05, "returns": 0.02})["composite_score"]
    assert big > small


def test_lower_is_better_direction():
    a = _rec({"sharpe": 0.05, "turnover": -0.05, "drawdown": -0.02})
    assert a["signals"]["turnover"] == 1   # lower turnover = good
    assert a["signals"]["drawdown"] == 1   # lower drawdown = good
    b = _rec({"sharpe": 0.05, "turnover": 0.05, "drawdown": 0.02})
    assert b["signals"]["turnover"] == -1
    assert b["signals"]["drawdown"] == -1


def test_unknown_without_core_metrics():
    a = _rec({"turnover": -0.05, "drawdown": -0.02})  # no sharpe nor returns
    assert a["recommendation"] == "UNKNOWN"
    assert a["composite_score"] is None
    assert _rec(None)["recommendation"] == "UNKNOWN"


def test_nan_inf_treated_as_absent():
    a = _rec({"sharpe": float("nan"), "returns": float("inf")})
    assert a["recommendation"] == "UNKNOWN"


def test_bool_not_treated_as_number():
    assert _rec({"sharpe": True, "returns": False})["recommendation"] == "UNKNOWN"


# ── recent_yearly_sharpe_delta parser ────────────────────────────────────────

_YR_PROPS = [{"name": "year"}, {"name": "sharpe"}]


def test_yearly_parser_median_of_recent_years():
    block = {
        "before": {"schema": {"properties": _YR_PROPS},
                   "records": [["2022", 1.0], ["2023", 1.5], ["2024", 1.6]]},
        "after": {"schema": {"properties": _YR_PROPS},
                  "records": [["2022", 1.4], ["2023", 1.3], ["2024", 1.4]]},
    }
    # recent 2 years: 2023 Δ=-0.2, 2024 Δ=-0.2 → median -0.2
    assert recent_yearly_sharpe_delta(block, recent_n=2) == pytest.approx(-0.2, abs=1e-9)


def test_yearly_parser_handles_missing_or_unaligned():
    assert recent_yearly_sharpe_delta(None) is None
    assert recent_yearly_sharpe_delta({}) is None
    block = {
        "before": {"schema": {"properties": _YR_PROPS}, "records": [["2020", 1.0]]},
        "after": {"schema": {"properties": _YR_PROPS}, "records": [["2024", 1.0]]},
    }
    assert recent_yearly_sharpe_delta(block) is None


def test_yearly_parser_real_schema_column_order():
    """Live schema has sharpe at index 6 of 12 — parser must use schema, not a
    fixed index."""
    props = [{"name": n} for n in
             ["year", "pnl", "bookSize", "longCount", "shortCount", "turnover",
              "sharpe", "returns", "drawdown", "margin", "fitness", "stage"]]
    rec_b = ["2024", 355307.0, 2e7, 1524, 1285, 0.16, 1.40, 0.035, 0.026, 4e-4, 0.67, "IS"]
    rec_a = ["2024", 355307.0, 2e7, 1524, 1285, 0.16, 1.55, 0.035, 0.026, 4e-4, 0.67, "IS"]
    block = {"before": {"schema": {"properties": props}, "records": [rec_b]},
             "after": {"schema": {"properties": props}, "records": [rec_a]}}
    assert recent_yearly_sharpe_delta(block) == pytest.approx(0.15, abs=1e-9)
