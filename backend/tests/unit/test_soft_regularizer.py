"""Unit tests for the soft-regularizer pure-math module (P1).

Covers complexity counting (distinct fields / total operator invocations),
the smooth complexity ramp, originality mapping, weighted blend with
renormalization over active legs, and the P(PASS) down-weight.
"""
import pytest

from backend.agents.services import soft_regularizer as sr


class TestCountComplexity:
    def test_nested_same_operator_counts_total_invocations(self):
        # ts_zscore appears twice → 3 total operator invocations, not 2 kinds.
        expr = "ts_zscore(group_neutralize(ts_zscore(rel_ret_cust, 20), pv13_1l_scibr), 20)"
        n_fields, n_ops = sr.count_complexity(expr)
        assert n_ops == 3
        # rel_ret_cust + pv13_1l_scibr (both data fields); numbers excluded.
        assert n_fields == 2

    def test_group_keyword_not_counted_as_field(self):
        expr = "group_neutralize(rank(ts_zscore(snt_social_value, 30)), industry)"
        n_fields, n_ops = sr.count_complexity(expr)
        assert n_ops == 3  # group_neutralize, rank, ts_zscore
        assert n_fields == 1  # snt_social_value only; 'industry' is a group key

    def test_distinct_fields_dedup(self):
        n_fields, n_ops = sr.count_complexity("add(close, close)")
        assert n_fields == 1
        assert n_ops == 1

    def test_empty_expression(self):
        assert sr.count_complexity("") == (0, 0)
        assert sr.count_complexity(None) == (0, 0)  # type: ignore[arg-type]


class TestComplexityPenalty:
    def test_below_c0_zero_penalty(self):
        # 2 fields, 3 ops → score 4 < c0=6 → 0
        assert sr.complexity_penalty(2, 3, c0=6.0, cmax=16.0) == 0.0

    def test_midrange_linear(self):
        # 3 fields, 8 ops → score 8 + 1.5 = 9.5 → (9.5-6)/(16-6) = 0.35
        assert sr.complexity_penalty(3, 8, c0=6.0, cmax=16.0) == pytest.approx(0.35)

    def test_above_cmax_saturates_to_one(self):
        assert sr.complexity_penalty(10, 20, c0=6.0, cmax=16.0) == 1.0

    def test_degenerate_config_no_penalty(self):
        # cmax <= c0 → guard returns 0 rather than dividing by <=0
        assert sr.complexity_penalty(5, 20, c0=10.0, cmax=10.0) == 0.0

    def test_complexity_score_convention(self):
        # matches alpha_semantic_validator: ops + 0.5*fields
        assert sr.complexity_score(2, 3) == pytest.approx(4.0)


class TestOriginalityPenalty:
    def test_none_distance_no_penalty(self):
        assert sr.originality_penalty(None) == 0.0

    def test_max_distance_no_penalty(self):
        assert sr.originality_penalty(1.0) == 0.0

    def test_zero_distance_full_penalty(self):
        assert sr.originality_penalty(0.0) == 1.0

    def test_partial(self):
        assert sr.originality_penalty(0.3) == pytest.approx(0.7)

    def test_clamps_out_of_range(self):
        assert sr.originality_penalty(-0.5) == 1.0
        assert sr.originality_penalty(1.5) == 0.0


class TestCombinePenalty:
    def test_equal_weights_two_legs(self):
        # w_align=0 → renormalize over complexity+originality (denom=1.0)
        out = sr.combine_penalty(0.4, 0.6, 0.0, w_complexity=0.5, w_originality=0.5)
        assert out == pytest.approx(0.5)

    def test_alignment_inert_when_zero_weight(self):
        # Supplying a nonzero alignment_pen with w_alignment=0 must NOT change
        # the result (P1 == P1+P2-with-zero-weight invariant).
        a = sr.combine_penalty(0.4, 0.6, 0.0, w_complexity=0.5, w_originality=0.5, w_alignment=0.0)
        b = sr.combine_penalty(0.4, 0.6, 0.99, w_complexity=0.5, w_originality=0.5, w_alignment=0.0)
        assert a == pytest.approx(b)

    def test_renormalizes_over_active_legs(self):
        # Only complexity active → result == complexity_pen regardless of others
        out = sr.combine_penalty(0.7, 0.2, 0.9, w_complexity=1.0, w_originality=0.0, w_alignment=0.0)
        assert out == pytest.approx(0.7)

    def test_three_legs_renormalized(self):
        out = sr.combine_penalty(0.3, 0.6, 0.9, w_complexity=1.0, w_originality=1.0, w_alignment=1.0)
        assert out == pytest.approx((0.3 + 0.6 + 0.9) / 3.0)

    def test_all_weights_zero_no_penalty(self):
        assert sr.combine_penalty(0.9, 0.9, 0.9, w_complexity=0.0, w_originality=0.0, w_alignment=0.0) == 0.0


class TestEffectivePPass:
    def test_lambda_zero_is_noop(self):
        assert sr.effective_p_pass(0.8, 0.9, 0.0) == pytest.approx(0.8)

    def test_partial_downweight(self):
        # 0.8 * (1 - 0.5*0.5) = 0.8 * 0.75 = 0.6
        assert sr.effective_p_pass(0.8, 0.5, 0.5) == pytest.approx(0.6)

    def test_full_penalty_full_lambda_suppresses(self):
        assert sr.effective_p_pass(0.8, 1.0, 1.0) == pytest.approx(0.0)

    def test_clamps(self):
        assert 0.0 <= sr.effective_p_pass(1.0, 1.0, 1.0) <= 1.0
        assert sr.effective_p_pass(0.0, 0.0, 0.5) == 0.0


class TestSoftRegResultMetrics:
    def test_shadow_omits_adjusted(self):
        res = sr.SoftRegResult(
            n_fields=2, n_operators=3, complexity_pen=0.1, originality_pen=0.2,
            alignment_pen=0.0, penalty=0.15, mode="shadow", p_pass_orig=0.7,
        )
        d = res.to_metrics_dict()
        assert d["_soft_reg_mode"] == "shadow"
        assert d["_soft_reg_n_operators"] == 3
        assert d["_soft_reg_penalty"] == pytest.approx(0.15)
        assert d["_soft_reg_p_pass_orig"] == pytest.approx(0.7)
        assert "_soft_reg_p_pass_adjusted" not in d  # not set in shadow

    def test_soft_includes_adjusted(self):
        res = sr.SoftRegResult(
            n_fields=2, n_operators=3, complexity_pen=0.1, originality_pen=0.2,
            alignment_pen=0.0, penalty=0.15, mode="soft", p_pass_orig=0.7,
            p_pass_adjusted=0.6,
        )
        d = res.to_metrics_dict()
        assert d["_soft_reg_p_pass_adjusted"] == pytest.approx(0.6)


class TestEvaluateCandidate:
    """The composed entry point used by the node — mirrors the wired block."""

    def test_shadow_leaves_p_pass_untouched(self):
        # A complex, duplicate-looking candidate: penalty > 0 but shadow must
        # NOT adjust P(PASS) (p_pass_adjusted stays None).
        res = sr.evaluate_candidate(
            "ts_rank(ts_zscore(ts_sum(ts_delta(close, 5), 20), 20), 10)",
            min_distance=0.1, p_pass=0.8,
            w_complexity=0.5, w_originality=0.5, c0=6.0, cmax=16.0, lam=0.5,
            mode="shadow",
        )
        assert res.p_pass_orig == pytest.approx(0.8)
        assert res.p_pass_adjusted is None
        assert res.penalty > 0.0  # legs still computed for calibration
        assert "_soft_reg_p_pass_adjusted" not in res.to_metrics_dict()

    def test_soft_downweights_consistently_with_primitives(self):
        expr = "ts_rank(ts_zscore(ts_sum(ts_delta(close, 5), 20), 20), 10)"
        res = sr.evaluate_candidate(
            expr, min_distance=0.1, p_pass=0.8,
            w_complexity=0.5, w_originality=0.5, c0=6.0, cmax=16.0, lam=0.5,
            mode="soft",
        )
        # Recompute via primitives and assert the composition matches.
        nf, no = sr.count_complexity(expr)
        cpen = sr.complexity_penalty(nf, no, c0=6.0, cmax=16.0)
        open_ = sr.originality_penalty(0.1)
        pen = sr.combine_penalty(cpen, open_, 0.0, w_complexity=0.5, w_originality=0.5)
        assert res.complexity_pen == pytest.approx(cpen)
        assert res.originality_pen == pytest.approx(open_)
        assert res.penalty == pytest.approx(pen)
        assert res.p_pass_adjusted == pytest.approx(sr.effective_p_pass(0.8, pen, 0.5))
        assert res.p_pass_adjusted <= res.p_pass_orig  # only ever down-weights

    def test_none_distance_zero_originality_leg(self):
        res = sr.evaluate_candidate(
            "rank(close)", min_distance=None, p_pass=0.9,
            w_complexity=0.5, w_originality=0.5, mode="soft",
        )
        assert res.originality_pen == 0.0
        # rank(close): 1 op, 1 field → score 1.5 < c0 → complexity_pen 0 too
        assert res.penalty == 0.0
        assert res.p_pass_adjusted == pytest.approx(0.9)  # no penalty → unchanged
