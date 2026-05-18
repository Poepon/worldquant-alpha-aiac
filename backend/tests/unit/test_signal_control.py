"""
Unit tests for signal-vs-control dual-run attribution:
  - derive_control_expression  (backend/alpha_expression_utils.py)
  - determine_attribution_dual_run (backend/agents/prompts/alignment.py)

来源: docs/alphagbm_skills_research_2026-05-15.md P0 — signal-vs-control 双跑归因
"""

import pytest
from backend.alpha_expression_utils import derive_control_expression
from backend.agents.prompts.alignment import determine_attribution_dual_run


# =============================================================================
# derive_control_expression
# =============================================================================

class TestDeriveControlExpression:
    """Tests for derive_control_expression: T1 signal core → raw field,
    structural wrappers preserved."""

    # ------------------------------------------------------------------
    # T1 expressions: whole expression IS the signal core → bare field
    # ------------------------------------------------------------------

    def test_t1_ts_rank(self):
        result = derive_control_expression("ts_rank(close, 20)")
        assert result == "close"

    def test_t1_ts_zscore(self):
        result = derive_control_expression("ts_zscore(returns, 5)")
        assert result == "returns"

    def test_t1_ts_mean(self):
        result = derive_control_expression("ts_mean(volume, 10)")
        assert result == "volume"

    def test_t1_ts_delta(self):
        result = derive_control_expression("ts_delta(close, 1)")
        assert result == "close"

    # ------------------------------------------------------------------
    # T2 via group wrapper
    # ------------------------------------------------------------------

    def test_t2_group_neutralize(self):
        result = derive_control_expression("group_neutralize(ts_rank(close, 20), industry)")
        assert result == "group_neutralize(close, industry)"

    def test_t2_group_rank(self):
        # T1 inner ts_zscore(volume, 10) stripped to its bare field; wrapper kept.
        result = derive_control_expression("group_rank(ts_zscore(volume, 10), sector)")
        assert result == "group_rank(volume, sector)"

    def test_t2_group_rank_exact(self):
        result = derive_control_expression("group_rank(ts_zscore(returns, 5), sector)")
        assert result == "group_rank(returns, sector)"

    # ------------------------------------------------------------------
    # T2 via pure cross-sectional wrapper
    # ------------------------------------------------------------------

    def test_t2_rank_over_t1(self):
        result = derive_control_expression("rank(ts_rank(close, 20))")
        assert result == "rank(close)"

    def test_t2_zscore_over_t1(self):
        result = derive_control_expression("zscore(ts_mean(volume, 10))")
        assert result == "zscore(volume)"

    # ------------------------------------------------------------------
    # T2 via smoothing ts wrapper
    # ------------------------------------------------------------------

    def test_t2_smoothing_ts_decay_linear(self):
        result = derive_control_expression("ts_decay_linear(ts_rank(close, 5), 10)")
        assert result == "ts_decay_linear(close, 10)"

    def test_t2_smoothing_ts_mean_over_t1(self):
        # ts_mean(ts_zscore(close,5), 3) — inner ts_mean wraps a T1 ts_zscore
        result = derive_control_expression("ts_mean(ts_zscore(returns, 5), 3)")
        assert result == "ts_mean(returns, 3)"

    # ------------------------------------------------------------------
    # T3 trade_when
    # ------------------------------------------------------------------

    def test_t3_trade_when_with_t1_inner(self):
        result = derive_control_expression(
            "trade_when(volume > adv20, ts_rank(close, 20), -1)"
        )
        assert result == "trade_when(volume > adv20, close, -1)"

    def test_t3_trade_when_with_t2_inner(self):
        result = derive_control_expression(
            "trade_when(volume > adv20, group_neutralize(ts_rank(close, 20), industry), -1)"
        )
        assert result == "trade_when(volume > adv20, group_neutralize(close, industry), -1)"

    # ------------------------------------------------------------------
    # Negation wrappers
    # ------------------------------------------------------------------

    def test_negation_multiply_minus1_left(self):
        result = derive_control_expression("multiply(-1, ts_rank(close, 20))")
        assert result == "multiply(-1, close)"

    def test_negation_multiply_minus1_right(self):
        result = derive_control_expression("multiply(ts_rank(close, 20), -1)")
        assert result == "multiply(-1, close)"

    def test_negation_subtract_zero(self):
        result = derive_control_expression("subtract(0, ts_rank(close, 20))")
        assert result == "multiply(-1, close)"

    def test_negated_t2(self):
        result = derive_control_expression(
            "multiply(-1, group_neutralize(ts_rank(close, 20), industry))"
        )
        assert result == "multiply(-1, group_neutralize(close, industry))"

    # ------------------------------------------------------------------
    # Returns None: Quasi-T1, tier None, unknown structure
    # ------------------------------------------------------------------

    def test_quasi_t1_divide_two_fields(self):
        # Quasi-T1 pattern — divide(field, field) — has no single ts_op signal core
        result = derive_control_expression("divide(ebit, ev)")
        assert result is None

    def test_tier_none_multi_field_arithmetic(self):
        # rank(close) has tier None (rank over raw field, no T1 inner) → no control
        result = derive_control_expression("rank(close)")
        assert result is None

    def test_empty_string(self):
        assert derive_control_expression("") is None

    def test_none_like_whitespace(self):
        assert derive_control_expression("   ") is None

    def test_bare_field_has_no_control(self):
        # A bare field is not T1 (no ts_op) — no structural wrapper, no signal core to strip
        assert derive_control_expression("close") is None

    # ------------------------------------------------------------------
    # Round-trip / structural validity checks
    # ------------------------------------------------------------------

    def test_t1_control_is_bare_identifier(self):
        """Control of T1 should be a bare field, not a function call."""
        for expr in [
            "ts_rank(close, 20)",
            "ts_zscore(returns, 5)",
            "ts_mean(volume, 10)",
        ]:
            ctl = derive_control_expression(expr)
            assert ctl is not None
            assert "(" not in ctl, f"Expected bare field, got {ctl!r}"

    def test_t2_control_preserves_wrapper_op(self):
        """Control of a T2 expression should start with the same wrapper op."""
        pairs = [
            ("group_neutralize(ts_rank(close, 20), industry)", "group_neutralize"),
            ("rank(ts_rank(close, 20))", "rank"),
            ("ts_decay_linear(ts_rank(close, 5), 10)", "ts_decay_linear"),
        ]
        for expr, expected_op in pairs:
            ctl = derive_control_expression(expr)
            assert ctl is not None, f"Expected control for {expr!r}"
            assert ctl.startswith(expected_op + "("), (
                f"Expected {expected_op}(...), got {ctl!r}"
            )

    def test_t3_control_starts_with_trade_when(self):
        ctl = derive_control_expression(
            "trade_when(volume > adv20, ts_rank(close, 20), -1)"
        )
        assert ctl is not None
        assert ctl.startswith("trade_when(")

    def test_control_does_not_contain_ts_op(self):
        """After stripping T1 core the control should not contain any ts_ aggregation."""
        ts_agg_ops = ["ts_rank", "ts_zscore", "ts_mean", "ts_std_dev", "ts_delta"]
        expressions = [
            "ts_rank(close, 20)",
            "group_neutralize(ts_rank(close, 20), industry)",
            "rank(ts_rank(volume, 5))",
            "trade_when(volume > adv20, ts_rank(close, 20), -1)",
        ]
        for expr in expressions:
            ctl = derive_control_expression(expr)
            assert ctl is not None, f"Expected a control for {expr!r}"
            for op in ts_agg_ops:
                assert op not in ctl, (
                    f"Control {ctl!r} still contains ts-op {op!r} (input: {expr!r})"
                )


# =============================================================================
# determine_attribution_dual_run
# =============================================================================

class TestDetermineAttributionDualRun:
    """Tests for determine_attribution_dual_run(signal_result, control_result, delta_min).

    Decision rule:
      Δ >= +threshold  → "hypothesis"
      |Δ| < threshold  → "implementation"
      Δ <= −threshold  → "both"
    """

    # ------------------------------------------------------------------
    # Core attribution decisions
    # ------------------------------------------------------------------

    def test_large_positive_delta_is_hypothesis(self):
        sig = {"sharpe": 2.0}
        ctl = {"sharpe": 0.5}   # delta = 1.5 >> threshold=0.3
        attr, conf, evid = determine_attribution_dual_run(sig, ctl, delta_sharpe_min=0.3)
        assert attr == "hypothesis"
        assert conf > 0.5

    def test_small_delta_is_implementation(self):
        sig = {"sharpe": 1.5}
        ctl = {"sharpe": 1.4}   # delta = 0.1 < threshold=0.3
        attr, conf, evid = determine_attribution_dual_run(sig, ctl, delta_sharpe_min=0.3)
        assert attr == "implementation"

    def test_negative_delta_is_both(self):
        sig = {"sharpe": 1.0}
        ctl = {"sharpe": 1.8}   # delta = −0.8 << −threshold=−0.3
        attr, conf, evid = determine_attribution_dual_run(sig, ctl, delta_sharpe_min=0.3)
        assert attr == "both"

    def test_near_zero_delta_is_implementation(self):
        sig = {"sharpe": 1.2}
        ctl = {"sharpe": 1.2}   # delta = 0.0
        attr, conf, evid = determine_attribution_dual_run(sig, ctl, delta_sharpe_min=0.3)
        assert attr == "implementation"

    def test_exactly_at_positive_threshold_is_hypothesis(self):
        """Boundary: Δ == threshold → 'hypothesis' (>= is inclusive)."""
        sig = {"sharpe": 1.8}
        ctl = {"sharpe": 1.5}   # delta = exactly 0.3
        attr, conf, evid = determine_attribution_dual_run(sig, ctl, delta_sharpe_min=0.3)
        assert attr == "hypothesis"

    def test_exactly_at_negative_threshold_is_both(self):
        """Boundary: Δ == −threshold → 'both' (<= is inclusive)."""
        sig = {"sharpe": 1.2}
        ctl = {"sharpe": 1.5}   # delta = exactly −0.3
        attr, conf, evid = determine_attribution_dual_run(sig, ctl, delta_sharpe_min=0.3)
        assert attr == "both"

    def test_just_below_threshold_is_implementation(self):
        sig = {"sharpe": 1.0 + 0.299}
        ctl = {"sharpe": 1.0}
        attr, _, _ = determine_attribution_dual_run(sig, ctl, delta_sharpe_min=0.3)
        assert attr == "implementation"

    # ------------------------------------------------------------------
    # Missing / invalid sharpe values — graceful degradation
    # ------------------------------------------------------------------

    def test_missing_sharpe_keys_no_exception(self):
        """Both dicts missing 'sharpe' → treated as 0, delta=0 → 'implementation'."""
        attr, conf, evid = determine_attribution_dual_run({}, {}, delta_sharpe_min=0.3)
        assert attr == "implementation"
        assert isinstance(conf, float)
        assert isinstance(evid, list)

    def test_none_sharpe_treated_as_zero(self):
        sig = {"sharpe": None}
        ctl = {"sharpe": None}
        attr, _, _ = determine_attribution_dual_run(sig, ctl, delta_sharpe_min=0.3)
        assert attr == "implementation"

    def test_signal_sharpe_none_ctl_positive(self):
        sig = {"sharpe": None}
        ctl = {"sharpe": 1.5}   # delta = 0 − 1.5 = −1.5 → "both"
        attr, _, _ = determine_attribution_dual_run(sig, ctl, delta_sharpe_min=0.3)
        assert attr == "both"

    # ------------------------------------------------------------------
    # Evidence content
    # ------------------------------------------------------------------

    def test_evidence_contains_three_numeric_fields(self):
        sig = {"sharpe": 2.0}
        ctl = {"sharpe": 0.8}
        _, _, evid = determine_attribution_dual_run(sig, ctl, delta_sharpe_min=0.3)
        assert any("signal_sharpe" in e for e in evid)
        assert any("control_sharpe" in e for e in evid)
        assert any("delta_sharpe" in e for e in evid)

    def test_evidence_has_at_least_4_entries(self):
        """First 3 numeric fields + 1 interpretation line."""
        sig = {"sharpe": 1.5}
        ctl = {"sharpe": 0.5}
        _, _, evid = determine_attribution_dual_run(sig, ctl, delta_sharpe_min=0.3)
        assert len(evid) >= 4

    # ------------------------------------------------------------------
    # Confidence — classification certainty (Direction A)
    # 0.5 on a decision boundary, rising to 1.0 deep in any zone.
    # ------------------------------------------------------------------

    def test_confidence_decreases_toward_boundary_in_implementation_zone(self):
        """Direction A: within the implementation band, confidence is highest
        deep in the zone (Δ≈0) and falls toward 0.5 as Δ nears the threshold."""
        ctl = {"sharpe": 1.0}
        _, conf_deep, _ = determine_attribution_dual_run(
            {"sharpe": 1.05}, ctl, delta_sharpe_min=0.5
        )  # delta=0.05 — deep in the implementation zone
        _, conf_mid, _ = determine_attribution_dual_run(
            {"sharpe": 1.20}, ctl, delta_sharpe_min=0.5
        )  # delta=0.20
        _, conf_near, _ = determine_attribution_dual_run(
            {"sharpe": 1.45}, ctl, delta_sharpe_min=0.5
        )  # delta=0.45 — near the threshold boundary
        assert conf_deep > conf_mid > conf_near, (
            f"Expected confidence to fall toward boundary: "
            f"{conf_deep:.3f} > {conf_mid:.3f} > {conf_near:.3f}"
        )

    def test_confidence_at_boundary_is_minimum(self):
        """Direction A: exactly on a decision boundary (|Δ| == threshold) the
        verdict is a near-coin-flip → confidence is at its 0.5 minimum."""
        sig = {"sharpe": 1.3}
        ctl = {"sharpe": 1.0}   # delta = 0.3 == threshold
        _, conf, _ = determine_attribution_dual_run(sig, ctl, delta_sharpe_min=0.3)
        assert conf == pytest.approx(0.5, abs=1e-9)

    def test_confidence_deep_in_zone_is_high(self):
        """Direction A: at Δ=0 (deepest in the implementation zone, furthest
        from either boundary) confidence reaches its 1.0 maximum."""
        sig = {"sharpe": 1.0}
        ctl = {"sharpe": 1.0}   # delta = 0.0
        _, conf, _ = determine_attribution_dual_run(sig, ctl, delta_sharpe_min=0.3)
        assert conf == pytest.approx(1.0, abs=1e-9)

    def test_confidence_is_between_zero_and_one(self):
        """confidence must always be ∈ [0, 1]."""
        cases = [
            ({"sharpe": 5.0}, {"sharpe": 0.0}, 0.3),
            ({"sharpe": 0.1}, {"sharpe": 0.1}, 0.3),
            ({"sharpe": 0.0}, {"sharpe": 3.0}, 0.1),
        ]
        for sig, ctl, thr in cases:
            _, conf, _ = determine_attribution_dual_run(sig, ctl, delta_sharpe_min=thr)
            assert 0.0 <= conf <= 1.0, f"confidence={conf} out of range for {sig}/{ctl}"

    # ------------------------------------------------------------------
    # Return-type contract
    # ------------------------------------------------------------------

    def test_returns_tuple_of_correct_types(self):
        attr, conf, evid = determine_attribution_dual_run(
            {"sharpe": 1.5}, {"sharpe": 0.8}, delta_sharpe_min=0.3
        )
        assert isinstance(attr, str)
        assert isinstance(conf, float)
        assert isinstance(evid, list)
        assert all(isinstance(e, str) for e in evid)

    def test_attribution_value_is_valid_token(self):
        valid_tokens = {"hypothesis", "implementation", "both"}
        for sig_sharpe, ctl_sharpe in [(2.0, 0.5), (1.3, 1.3), (0.5, 1.5)]:
            attr, _, _ = determine_attribution_dual_run(
                {"sharpe": sig_sharpe}, {"sharpe": ctl_sharpe}, delta_sharpe_min=0.3
            )
            assert attr in valid_tokens, f"Unexpected attribution {attr!r}"
