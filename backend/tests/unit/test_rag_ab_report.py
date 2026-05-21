"""rag_ab_report stats helpers (2026-05-21).

Pure-function tests for the Wilson CI + two-proportion z-test used to gate the
A/B conclusion. (The SQL aggregation is validated by running the script against
live data; the denominator-excludes-skips logic lives in the SQL itself.)
"""
from __future__ import annotations

import importlib.util
import pathlib

_SCRIPT = pathlib.Path(__file__).resolve().parents[3] / "scripts" / "rag_ab_report.py"
_spec = importlib.util.spec_from_file_location("rag_ab_report", _SCRIPT)
rep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rep)


def test_wilson_ci_basic():
    lo, hi = rep._wilson_ci(50, 100)
    assert 0.0 <= lo < 0.5 < hi <= 1.0       # symmetric-ish around 0.5
    assert rep._wilson_ci(0, 0) == (0.0, 0.0)  # empty → no interval
    lo0, hi0 = rep._wilson_ci(0, 100)
    assert lo0 == 0.0 and hi0 < 0.1            # 0 successes → tight low band


def test_two_proportion_z_significant():
    # 30/100 vs 10/100 — clearly different
    z, p = rep._two_proportion_z(30, 100, 10, 100)
    assert abs(z) > 2 and p < 0.05


def test_two_proportion_z_no_difference():
    z, p = rep._two_proportion_z(20, 100, 20, 100)
    assert abs(z) < 1e-9 and p > 0.99


def test_two_proportion_z_empty_arm_safe():
    assert rep._two_proportion_z(0, 0, 5, 10) == (0.0, 1.0)
    assert rep._two_proportion_z(5, 10, 0, 0) == (0.0, 1.0)


def test_arms_constant():
    assert rep._ARMS == ("control", "category")


# --- continuous-metric stats (2026-05-21 measurement reinforcement) --------

def test_welch_identical_means_no_signal():
    t, p, df = rep._welch_from_summary(0.5, 1.0, 50, 0.5, 1.0, 50)
    assert abs(t) < 1e-9 and p > 0.99


def test_welch_clear_difference_significant():
    # mean diff 1.0, var 1.0, n 50 each → t = 1/sqrt(0.04) = 5 → p tiny
    t, p, df = rep._welch_from_summary(1.0, 1.0, 50, 0.0, 1.0, 50)
    assert t > 4 and p < 0.05 and df > 0


def test_welch_degenerate_small_n_safe():
    assert rep._welch_from_summary(1.0, 1.0, 1, 0.0, 1.0, 50) == (0.0, 1.0, 0.0)


def test_cohens_d_one_sd_apart():
    # pooled sd = 1 → d = (1-0)/1 = 1.0
    assert abs(rep._cohens_d(1.0, 1.0, 50, 0.0, 1.0, 50) - 1.0) < 1e-6


def test_cohens_d_degenerate_zero():
    assert rep._cohens_d(1.0, 0.0, 1, 0.0, 0.0, 1) == 0.0


def test_required_n_medium_effect():
    # d=0.5 at alpha .05 / power .80 → ~63 per arm
    assert rep._required_n_per_arm(0.5) == 63


def test_required_n_tiny_effect_is_huge():
    # negligible effect → enormous required n (matches the live d≈0.02 ⇒ ~30k)
    assert rep._required_n_per_arm(0.02) > 20000


def test_required_n_zero_effect_returns_none():
    assert rep._required_n_per_arm(0.0) is None
    assert rep._required_n_per_arm(None) is None
