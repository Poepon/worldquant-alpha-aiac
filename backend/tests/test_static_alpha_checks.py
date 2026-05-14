"""Tests for backend/static_alpha_checks.py (V-P0 2026-05-15).

The three expression-only V-16 checks (look-ahead bias, divide-by-zero,
overfit-window) were moved out of node_evaluate so they run pre-simulate inside
node_validate — no metrics, no sharpe>3 gate. This file covers the extracted
pure functions plus a lightweight node_validate integration check.
"""
from __future__ import annotations

import pytest

from backend.static_alpha_checks import (
    check_divide_by_zero,
    check_lookahead_bias,
    check_overfit_window,
    run_static_suspicion_checks,
)


class TestCheckDivideByZero:
    @pytest.mark.parametrize("expr,expected", [
        ("divide(close, returns)", True),
        ("divide(eps, volume)", True),
        ("divide(close, fnd6_newa2v1300_ni)", True),       # net_income real name
        ("divide(x, ts_mean(returns, 5))", True),          # V-26.69: nested denom
        ("divide(close, eps)", False),
        ("divide(close, cap)", False),
        ("divide(close, vwap)", False),
    ])
    def test_risky_denominator_detection(self, expr, expected):
        result = check_divide_by_zero(expr)
        assert (result is not None) is expected

    def test_empty_expr(self):
        assert check_divide_by_zero("") is None
        assert check_divide_by_zero(None) is None


class TestCheckLookaheadBias:
    def test_actual_eps_without_ts_delay_flagged(self):
        result = check_lookahead_bias("rank(actual_eps_value_quarterly)")
        assert result is not None

    def test_actual_eps_with_ts_delay_safe(self):
        # ts_delay directly wraps the field
        assert check_lookahead_bias(
            "rank(ts_delay(actual_eps_value_quarterly, 1))"
        ) is None

    def test_sibling_ts_delay_does_not_mitigate(self):
        # V-26.70: a sibling ts_delay on another field must NOT count as wrapping
        assert check_lookahead_bias(
            "add(ts_delay(close, 1), actual_eps_value)"
        ) is not None

    def test_no_announcement_field_no_flag(self):
        assert check_lookahead_bias("ts_rank(close, 20)") is None

    def test_fam_earn_date_flagged(self):
        result = check_lookahead_bias(
            "trade_when(fam_earn_date, ts_rank(close, 20), -1)"
        )
        assert result is not None

    def test_empty_expr(self):
        assert check_lookahead_bias("") is None
        assert check_lookahead_bias(None) is None


class TestCheckOverfitWindow:
    def test_standard_windows_safe(self):
        assert check_overfit_window(
            "ts_rank(close, 20) + ts_mean(returns, 60)"
        ) is None

    def test_non_standard_window_flagged(self):
        result = check_overfit_window("ts_rank(close, 137)")
        assert result is not None
        assert "137" in result

    def test_multiple_weird_windows(self):
        result = check_overfit_window(
            "ts_zscore(returns, 47) * ts_rank(close, 113)"
        )
        assert result is not None
        assert "47" in result and "113" in result

    def test_window_1_ignored(self):
        assert check_overfit_window("ts_delay(close, 1)") is None

    def test_empty_expr(self):
        assert check_overfit_window("") is None
        assert check_overfit_window(None) is None


class TestRunStaticSuspicionChecks:
    def test_clean_expression_no_flags(self):
        assert run_static_suspicion_checks("ts_rank(close, 20)") == []

    def test_empty_expression(self):
        assert run_static_suspicion_checks("") == []
        assert run_static_suspicion_checks(None) == []

    def test_lookahead_is_hard_severity(self):
        # No sharpe gate — a plain expression triggers it.
        flags = run_static_suspicion_checks("rank(actual_eps_value_quarterly)")
        lookahead = [f for f in flags if f["check"] == "lookahead_bias"]
        assert len(lookahead) == 1
        assert lookahead[0]["severity"] == "hard"
        assert lookahead[0]["evidence"]

    def test_divide_by_zero_is_soft_severity(self):
        flags = run_static_suspicion_checks("divide(close, returns)")
        divide = [f for f in flags if f["check"] == "divide_by_zero"]
        assert len(divide) == 1
        assert divide[0]["severity"] == "soft"

    def test_overfit_window_is_soft_severity(self):
        flags = run_static_suspicion_checks("ts_rank(close, 137)")
        overfit = [f for f in flags if f["check"] == "overfit_window"]
        assert len(overfit) == 1
        assert overfit[0]["severity"] == "soft"

    def test_flag_shape(self):
        flags = run_static_suspicion_checks("rank(actual_eps_value_quarterly)")
        for f in flags:
            assert set(f.keys()) == {"check", "severity", "evidence"}

    def test_multiple_flags_in_one_expression(self):
        # divide-by-zero (soft) + overfit-window (soft) in the same expression
        flags = run_static_suspicion_checks("divide(close, ts_mean(returns, 137))")
        names = {f["check"] for f in flags}
        assert "divide_by_zero" in names
        assert "overfit_window" in names


class TestNodeValidateIntegration:
    """Lightweight check that node_validate applies the static checks pre-simulate."""

    @pytest.mark.asyncio
    async def test_static_checks_applied_in_node_validate(self):
        from backend.agents.graph.nodes.validation import node_validate
        from backend.agents.graph.state import AlphaCandidate, MiningState

        state = MiningState(
            task_id=1,
            region="USA",
            universe="TOP3000",
            dataset_id="x",
            fields=[
                {"id": "close"},
                {"id": "returns"},
                {"id": "actual_eps_value_quarterly"},
            ],
            pending_alphas=[
                AlphaCandidate(expression="ts_rank(close, 20)"),               # clean
                AlphaCandidate(expression="rank(actual_eps_value_quarterly)"),  # HARD look-ahead
                AlphaCandidate(expression="divide(close, returns)"),            # SOFT divide
            ],
        )

        result = await node_validate(state, config=None)
        alphas = result["pending_alphas"]
        by_expr = {a.expression: a for a in alphas}

        # clean expression stays valid, no static warning
        clean = by_expr["ts_rank(close, 20)"]
        assert clean.is_valid is True

        # HARD look-ahead → invalidated → routes to SELF_CORRECT
        lookahead = by_expr["rank(actual_eps_value_quarterly)"]
        assert lookahead.is_valid is False
        assert "lookahead" in (lookahead.validation_error or "").lower()

        # SOFT divide-by-zero → still valid, annotated as a warning
        divide = by_expr["divide(close, returns)"]
        assert divide.is_valid is True
        assert "divide_by_zero" in (divide.validation_error or "")
