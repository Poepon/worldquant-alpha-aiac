"""Tests for V-16 suspicion-mode checks (sharpe > 3.0 audit).

V-P0 (2026-05-15): the three expression-only checks (divide-by-zero,
look-ahead bias, overfit-window) moved to backend/static_alpha_checks.py and
now run pre-simulate inside node_validate. `_run_suspicion_checks` keeps only
the metric-dependent checks:
  3. Survivorship bias — info-only flag (BRAIN universe selection)
  4. Cost vacuum       — turnover > 0.50 + sharpe > 5
  6. Outlier metrics   — returns > 100% / drawdown=0 with non-trivial sharpe /
                          fitness/sharpe inconsistency

The migrated static checks (Risk 1/2/5) are covered by
test_static_alpha_checks.py; the `_v16_check_*` names below are re-export
shims kept for backward compatibility — these tests also verify the shims.

Severity:
  hard — downgrades PASS → PASS_PROVISIONAL
  soft — annotates only
  info — manual review only
"""
from __future__ import annotations

import pytest

from backend.agents.graph.nodes.evaluation import (
    V16_SUSPICION_THRESHOLD,
    _run_suspicion_checks,
    _v16_check_cost_vacuum,
    _v16_check_divide_by_zero,
    _v16_check_lookahead,
    _v16_check_outliers,
    _v16_check_overfit_window,
)


class TestThreshold:
    def test_below_threshold_returns_empty(self):
        # sharpe ≤ 3.0 → no checks run, empty list
        assert _run_suspicion_checks({"sharpe": 2.99}, "ts_rank(close, 20)") == []

    def test_at_threshold_no_trigger(self):
        # Strictly > 3.0
        assert _run_suspicion_checks({"sharpe": 3.0}, "ts_rank(close, 20)") == []

    def test_above_threshold_triggers(self):
        # Even a clean expression gets the survivorship-info flag
        flags = _run_suspicion_checks({"sharpe": 3.5}, "ts_rank(close, 20)")
        names = [f["check"] for f in flags]
        assert "survivorship_bias" in names

    def test_threshold_constant_visible(self):
        assert V16_SUSPICION_THRESHOLD == 3.0


class TestDivideByZero:
    @pytest.mark.parametrize("expr,expected", [
        ("divide(close, returns)", True),
        ("divide(eps, volume)", True),
        ("divide(close, fnd6_newa2v1300_ni)", True),  # net_income real name
        ("divide(close, eps)", False),
        ("divide(close, cap)", False),                # cap unlikely 0
        ("divide(close, vwap)", False),
    ])
    def test_risky_denominator_detection(self, expr, expected):
        result = _v16_check_divide_by_zero(expr)
        if expected:
            assert result is not None
        else:
            assert result is None

    def test_empty_expr(self):
        assert _v16_check_divide_by_zero("") is None
        assert _v16_check_divide_by_zero(None) is None


class TestLookahead:
    def test_actual_eps_without_ts_delay_flagged(self):
        result = _v16_check_lookahead("rank(actual_eps_value_quarterly)")
        assert result is not None
        assert "actual_eps_value" in result.lower() or "ts_delay" in result.lower()

    def test_actual_eps_with_ts_delay_safe(self):
        # ts_delay wraps the field
        assert _v16_check_lookahead(
            "rank(ts_delay(actual_eps_value_quarterly, 1))"
        ) is None

    def test_no_announcement_field_no_flag(self):
        assert _v16_check_lookahead("ts_rank(close, 20)") is None

    def test_fam_earn_date_flagged(self):
        result = _v16_check_lookahead(
            "trade_when(fam_earn_date, ts_rank(close, 20), -1)"
        )
        assert result is not None


class TestCostVacuum:
    def test_high_turnover_high_sharpe_flagged(self):
        result = _v16_check_cost_vacuum({"turnover": 0.65, "sharpe": 6.0})
        assert result is not None

    def test_high_turnover_normal_sharpe_safe(self):
        # turnover>0.5 but sharpe<5 → not the cost-vacuum pattern
        assert _v16_check_cost_vacuum({"turnover": 0.65, "sharpe": 1.5}) is None

    def test_low_turnover_high_sharpe_safe(self):
        # Low turnover doesn't fit the cost-vacuum pattern
        assert _v16_check_cost_vacuum({"turnover": 0.10, "sharpe": 6.0}) is None


class TestOverfitWindow:
    def test_standard_windows_safe(self):
        # All standard sizes
        assert _v16_check_overfit_window(
            "ts_rank(close, 20) + ts_mean(returns, 60)"
        ) is None

    def test_non_standard_window_flagged(self):
        result = _v16_check_overfit_window("ts_rank(close, 137)")
        assert result is not None
        assert "137" in result

    def test_multiple_weird_windows(self):
        result = _v16_check_overfit_window(
            "ts_zscore(returns, 47) * ts_rank(close, 113)"
        )
        assert result is not None
        assert "47" in result
        assert "113" in result

    def test_window_1_ignored(self):
        # ts_delay(_, 1) is universal; not flagged
        assert _v16_check_overfit_window("ts_delay(close, 1)") is None


class TestOutliers:
    def test_extreme_returns_flagged(self):
        flags = _v16_check_outliers({"returns": 1.5, "sharpe": 5.0, "drawdown": 0.1})
        assert any("returns" in f for f in flags)

    def test_zero_drawdown_with_sharpe_flagged(self):
        flags = _v16_check_outliers({"returns": 0.3, "sharpe": 2.0, "drawdown": 0})
        assert any("drawdown" in f for f in flags)

    def test_zero_drawdown_zero_sharpe_safe(self):
        # drawdown=0 with sharpe≈0 (e.g., empty alpha) — not the anomaly we care about
        flags = _v16_check_outliers({"returns": 0.0, "sharpe": 0.1, "drawdown": 0})
        assert flags == []

    def test_fitness_sharpe_inconsistency(self):
        # fitness>10 with sharpe<5 — BRAIN-side score inconsistency
        flags = _v16_check_outliers({"fitness": 13.0, "sharpe": 2.5, "drawdown": 0.1})
        assert any("fitness" in f.lower() for f in flags)

    def test_normal_metrics_clean(self):
        flags = _v16_check_outliers({
            "returns": 0.20, "sharpe": 4.0,
            "drawdown": 0.15, "fitness": 3.5,
        })
        assert flags == []


class TestEnd2End:
    """_run_suspicion_checks end-to-end on representative cases."""

    def test_yp2qnnvw_pattern_caught(self):
        # The actual spike 2.0 leak: train=8.37 / test=0 / drawdown=0
        # via sign-flip multiply(-1, ts_zscore(...))
        metrics = {
            "sharpe": 8.37, "fitness": 13.62,
            "turnover": 0.6667, "returns": 0.40,
            "drawdown": 0.0,  # outlier signal
        }
        flags = _run_suspicion_checks(
            metrics, "multiply(-1, ts_zscore(analyst_revision_rank_derivative, 5))"
        )
        names = [f["check"] for f in flags]
        # Should fire: cost_vacuum (turnover 0.67 + sharpe 8.37) + outlier (drawdown=0)
        assert "cost_vacuum" in names
        assert "outlier_metric" in names
        # And survivorship_bias info
        assert "survivorship_bias" in names

    def test_clean_high_sharpe_only_info(self):
        # sharpe > 3 but otherwise clean — only the info-level survivorship flag
        metrics = {
            "sharpe": 4.0, "fitness": 3.5,
            "turnover": 0.10, "returns": 0.20,
            "drawdown": 0.15,
        }
        flags = _run_suspicion_checks(metrics, "ts_rank(close, 20)")
        severities = [f["severity"] for f in flags]
        assert "hard" not in severities
        assert "info" in severities

    def test_clean_low_sharpe_skipped(self):
        # sharpe ≤ threshold → skip entirely
        metrics = {"sharpe": 1.5, "drawdown": 0.0, "turnover": 0.65}
        flags = _run_suspicion_checks(metrics, "divide(close, returns)")
        assert flags == []

    def test_lookahead_no_longer_in_suspicion_checks(self):
        # V-P0: look-ahead bias moved to node_validate (pre-simulate). It must
        # NOT appear in the post-simulate _run_suspicion_checks output anymore,
        # even at sharpe > 3.0.
        metrics = {"sharpe": 4.5, "drawdown": 0.10, "fitness": 3.0, "turnover": 0.20}
        flags = _run_suspicion_checks(
            metrics, "rank(actual_eps_value_quarterly)"
        )
        assert [f for f in flags if f["check"] == "lookahead_bias"] == []

    def test_overfit_window_no_longer_in_suspicion_checks(self):
        # V-P0: overfit-window moved to node_validate (pre-simulate).
        metrics = {"sharpe": 4.0, "drawdown": 0.10, "fitness": 3.0, "turnover": 0.15}
        flags = _run_suspicion_checks(metrics, "ts_rank(close, 137)")
        assert [f for f in flags if f["check"] == "overfit_window"] == []

    def test_divide_by_zero_no_longer_in_suspicion_checks(self):
        # V-P0: divide-by-zero moved to node_validate (pre-simulate).
        metrics = {"sharpe": 4.0, "drawdown": 0.10, "fitness": 3.0, "turnover": 0.15}
        flags = _run_suspicion_checks(metrics, "divide(close, returns)")
        assert [f for f in flags if f["check"] == "divide_by_zero"] == []

    def test_non_dict_metrics_safe(self):
        # Defensive: malformed metrics
        assert _run_suspicion_checks(None, "ts_rank(close, 20)") == []
        assert _run_suspicion_checks([], "ts_rank(close, 20)") == []
