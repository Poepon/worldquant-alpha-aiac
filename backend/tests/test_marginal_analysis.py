"""Unit tests for analyze_marginal_contribution (2026-05-24).

Pure submit-recommendation logic from before-and-after marginal deltas. The
decision is Sharpe-led: Δsharpe > 0 (lifts the merged portfolio) → SUBMIT,
Δsharpe < 0 (drags it) → SKIP, negligible → tie-break on fitness/returns.
"""
import pytest

from backend.marginal_analysis import analyze_marginal_contribution


def test_submit_when_sharpe_lifts_portfolio():
    a = analyze_marginal_contribution(
        {"sharpe": 0.05, "fitness": 0.03, "returns": 0.01,
         "turnover": -0.001, "drawdown": -0.001, "pnl": 120000.0},
        merged={"sharpe": 3.2},
    )
    assert a["recommendation"] == "SUBMIT"
    assert a["label"] == "推荐提交"
    assert a["signals"]["sharpe"] == 1
    assert a["marginal_score"] > 0
    assert any("Sharpe" in r for r in a["reasons"])


def test_skip_when_sharpe_drags_portfolio():
    """The 3qzdKPrz-like case: negative marginal sharpe → SKIP even though the
    standalone IS sharpe is high."""
    a = analyze_marginal_contribution(
        {"sharpe": -0.02, "fitness": -0.04, "returns": -0.0043,
         "turnover": -0.0031, "drawdown": 0.0011, "pnl": -212162.0},
        merged={"sharpe": 3.14},
    )
    assert a["recommendation"] == "SKIP"
    assert a["label"] == "不推荐提交"
    assert a["signals"]["sharpe"] == -1
    assert a["marginal_score"] < 0


def test_neutral_when_all_negligible():
    a = analyze_marginal_contribution(
        {"sharpe": 0.001, "fitness": 0.005, "returns": 0.0001,
         "turnover": 0.001, "drawdown": 0.0001, "pnl": 5.0},
    )
    assert a["recommendation"] == "NEUTRAL"
    assert a["signals"]["sharpe"] == 0


def test_negligible_sharpe_tiebreaks_on_fitness_returns():
    # Δsharpe in dead-band but fitness + returns clearly positive → SUBMIT
    a = analyze_marginal_contribution(
        {"sharpe": 0.002, "fitness": 0.10, "returns": 0.02, "drawdown": 0.0},
    )
    assert a["recommendation"] == "SUBMIT"
    # mirror: clearly negative secondary → SKIP
    b = analyze_marginal_contribution(
        {"sharpe": 0.0, "fitness": -0.10, "returns": -0.02},
    )
    assert b["recommendation"] == "SKIP"


def test_unknown_without_sharpe():
    a = analyze_marginal_contribution({"fitness": 0.1, "returns": 0.02})
    assert a["recommendation"] == "UNKNOWN"
    assert a["label"] == "数据不足"
    assert a["marginal_score"] is None
    b = analyze_marginal_contribution(None)
    assert b["recommendation"] == "UNKNOWN"


def test_lower_is_better_signal_direction():
    """turnover/drawdown: negative Δ (lower) is GOOD (+1), positive Δ is bad."""
    a = analyze_marginal_contribution(
        {"sharpe": 0.05, "turnover": -0.05, "drawdown": -0.02},
    )
    assert a["signals"]["turnover"] == 1
    assert a["signals"]["drawdown"] == 1
    b = analyze_marginal_contribution(
        {"sharpe": 0.05, "turnover": 0.05, "drawdown": 0.02},
    )
    assert b["signals"]["turnover"] == -1
    assert b["signals"]["drawdown"] == -1


def test_submit_with_worsening_drawdown_adds_caveat():
    a = analyze_marginal_contribution(
        {"sharpe": 0.05, "drawdown": 0.02},  # lifts sharpe but worsens drawdown
    )
    assert a["recommendation"] == "SUBMIT"
    assert any("回撤" in r for r in a["reasons"])


def test_bool_not_treated_as_number():
    """A stray bool must not be read as a numeric delta (bool ⊂ int in Python)."""
    a = analyze_marginal_contribution({"sharpe": True})
    assert a["recommendation"] == "UNKNOWN"
