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
