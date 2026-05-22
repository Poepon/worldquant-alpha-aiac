"""Unit tests for plan a-streamed-wren (2026-05-21): operator visibility +
canonical-structure scaffold + pre-simulate degenerate/unknown-operator guards.

Covers:
    Tier 0 — build_operators_context no longer truncates the (insertion-last)
             Cross Sectional / Group operators; build_alpha_generation_prompt
             surfaces group_neutralize.
    Tier 1 — CROSS-SECTIONAL NEUTRALIZATION RULE rendered in the code-gen prompt.
    Tier 2a — ts_regression(x,x) / ts_corr(x,x) self-reference → hard finding.
    Tier 2b — reject_unknown_operators promotes unknown ops to hard only when
              opted in AND an operator set is known (footgun-safe otherwise).

Run with: pytest backend/tests/unit/test_operator_visibility_and_guards.py -v
"""

from backend.agents.prompts.base import (
    PromptContext,
    build_operators_context,
    build_strategy_constraints,
)
from backend.agents.prompts.generation import build_alpha_generation_prompt
from backend.alpha_semantic_validator import AlphaSemanticValidator, RuleId


def _catalog_with_group_last():
    """66-ish operator catalog mirroring production: Cross Sectional / Group
    operators sit AFTER 49 time-series/arithmetic ops (the bug positioned them
    at id 50-65 → truncated by the old 40/50 caps)."""
    ops = [{"name": f"ts_dummy_{i}", "category": "Time Series"} for i in range(49)]
    ops += [
        {"name": "winsorize", "category": "Cross Sectional"},
        {"name": "normalize", "category": "Cross Sectional"},
        {"name": "rank", "category": "Cross Sectional"},
        {"name": "zscore", "category": "Cross Sectional"},
        {"name": "group_scale", "category": "Group"},
        {"name": "group_neutralize", "category": "Group"},
    ]
    return ops


# ============================================================================
# Tier 0 — operator catalog visibility
# ============================================================================


class TestOperatorVisibility:
    def test_group_ops_rendered_despite_being_last(self):
        out = build_operators_context(_catalog_with_group_last())
        assert "group_neutralize" in out
        assert "group_scale" in out
        assert "rank" in out
        assert "zscore" in out
        # Category headers present (build groups by category).
        assert "Group" in out
        assert "Cross Sectional" in out

    def test_old_cap_would_have_truncated(self):
        # Documents the bug: with the legacy max_ops=40 the group ops (index
        # 49-54) are cut. Guards against a regression back to a low cap.
        out_old = build_operators_context(_catalog_with_group_last(), max_ops=40)
        assert "group_neutralize" not in out_old
        out_new = build_operators_context(_catalog_with_group_last())  # default 120
        assert "group_neutralize" in out_new

    def test_alpha_generation_prompt_surfaces_group_ops(self):
        ctx = PromptContext(
            fields=[{"id": "close", "type": "MATRIX"}],
            operators=_catalog_with_group_last(),
        )
        prompt = build_alpha_generation_prompt(ctx)
        assert "group_neutralize" in prompt

    def test_per_category_cap_keeps_workhorse_timeseries_ops(self):
        # Regression guard: callers pass the full catalog ORDER BY category,name,
        # so Time Series renders alphabetically. An inner per-category [:10] cap
        # would drop ts_mean/ts_rank/ts_zscore/ts_scale/ts_sum (they all sort
        # after ts_delta) — the workhorse operators. With >10 TS ops they must
        # still appear.
        ts_names = [
            "ts_arg_max", "ts_arg_min", "ts_av_diff", "ts_backfill", "ts_corr",
            "ts_count_nans", "ts_covariance", "ts_decay_linear", "ts_delay",
            "ts_delta", "ts_mean", "ts_product", "ts_quantile", "ts_rank",
            "ts_regression", "ts_scale", "ts_std_dev", "ts_step", "ts_sum",
            "ts_zscore",
        ]
        ops = [{"name": n, "category": "Time Series"} for n in ts_names]
        out = build_operators_context(ops)
        for workhorse in ("ts_mean", "ts_rank", "ts_zscore", "ts_scale", "ts_sum"):
            assert workhorse in out, f"{workhorse} dropped by per-category cap"

    def test_null_category_does_not_crash(self):
        # category column is nullable; a None key must not crash sorted().
        ops = [
            {"name": "ts_rank", "category": "Time Series"},
            {"name": "weird_op", "category": None},
        ]
        out = build_operators_context(ops)
        assert "weird_op" in out
        assert "ts_rank" in out


# ============================================================================
# Tier 1 — canonical-structure scaffold
# ============================================================================


class TestCanonicalStructureScaffold:
    def test_rule_present_in_constraints(self):
        out = build_strategy_constraints(PromptContext())
        assert "CROSS-SECTIONAL NEUTRALIZATION RULE" in out
        assert "group_neutralize" in out

    def test_group_arg_prohibition_present(self):
        # 2026-05-23: explicit negative constraint so the LLM stops using the
        # universe name (top3000) / a data field as the grouping argument.
        out = build_strategy_constraints(PromptContext())
        assert "NEVER the universe name" in out
        assert "top3000" in out

    def test_rule_present_in_full_prompt(self):
        ctx = PromptContext(
            fields=[{"id": "close", "type": "MATRIX"}],
            operators=_catalog_with_group_last(),
        )
        prompt = build_alpha_generation_prompt(ctx)
        assert "CROSS-SECTIONAL NEUTRALIZATION RULE" in prompt
        # Placeholder, not a real field name (so the LLM can't copy a
        # non-existent field verbatim).
        assert "<a listed field>" in prompt


# ============================================================================
# Tier 2a — degenerate self-reference
# ============================================================================


class TestDegenerateSelfReference:
    def _val(self):
        return AlphaSemanticValidator(
            fields=[
                {"id": "close", "type": "MATRIX"},
                {"id": "returns", "type": "MATRIX"},
            ],
            strict_field_check=False,
        )

    def test_ts_regression_self_reference_is_hard(self):
        r = self._val().validate("ts_regression(close, close, 10)")
        deg = [f for f in r.findings if f.rule_id == RuleId.DEGENERATE_SELF_REFERENCE]
        assert len(deg) == 1
        assert deg[0].severity == "hard"
        assert r.valid is False

    def test_ts_corr_self_reference_is_hard(self):
        r = self._val().validate("ts_corr(close, close, 20)")
        deg = [f for f in r.findings if f.rule_id == RuleId.DEGENERATE_SELF_REFERENCE]
        assert len(deg) == 1
        assert r.valid is False

    def test_distinct_args_not_flagged(self):
        r = self._val().validate("ts_regression(close, returns, 60)")
        deg = [f for f in r.findings if f.rule_id == RuleId.DEGENERATE_SELF_REFERENCE]
        assert deg == []

    def test_ts_covariance_self_reference_not_flagged(self):
        # cov(x, x) = variance is a real signal — must NOT be rejected.
        r = self._val().validate("ts_covariance(close, close, 20)")
        deg = [f for f in r.findings if f.rule_id == RuleId.DEGENERATE_SELF_REFERENCE]
        assert deg == []

    def test_uppercase_self_reference_flagged(self):
        # UPPERCASE alias dialects (Alpha191 etc.) must not bypass the guard.
        r = self._val().validate("TS_REGRESSION(close, close, 10)")
        deg = [f for f in r.findings if f.rule_id == RuleId.DEGENERATE_SELF_REFERENCE]
        assert len(deg) == 1
        assert r.valid is False

    def test_whitespace_and_two_arg_self_reference_flagged(self):
        # No-window (2-arg) call + padded whitespace must still compare equal.
        r = self._val().validate("ts_corr( close , close )")
        deg = [f for f in r.findings if f.rule_id == RuleId.DEGENERATE_SELF_REFERENCE]
        assert len(deg) == 1


# ============================================================================
# Tier 2b — unknown-operator promote-to-hard (opt-in, footgun-safe)
# ============================================================================


class TestUnknownOperatorGuard:
    def test_reject_on_with_known_set_is_hard(self):
        v = AlphaSemanticValidator(
            operators=["rank", "ts_rank", "close"],
            reject_unknown_operators=True,
        )
        r = v.validate("vec_stddev(close)")
        unk = [f for f in r.findings if f.rule_id == RuleId.UNKNOWN_OPERATOR]
        assert len(unk) == 1
        assert unk[0].severity == "hard"
        assert r.valid is False

    def test_default_stays_soft_backward_compat(self):
        # Mirrors the legacy contract (test_alpha_finding_format) — default
        # constructor leaves unknown ops soft / non-invalidating.
        v = AlphaSemanticValidator(operators=["rank"])
        r = v.validate("frobnicate(close)")
        unk = [f for f in r.findings if f.rule_id == RuleId.UNKNOWN_OPERATOR]
        assert len(unk) == 1
        assert unk[0].severity == "soft"

    def test_empty_operator_set_is_footgun_safe(self):
        # reject ON but no operator set known → nothing fires (never reject every
        # operator just because the registry wasn't synced/loaded).
        v = AlphaSemanticValidator(
            operators=["rank"], reject_unknown_operators=True,
        )
        v.allowed_operators = set()  # simulate empty/unloaded operator set
        r = v.validate("frobnicate(whatever)")
        unk = [f for f in r.findings if f.rule_id == RuleId.UNKNOWN_OPERATOR]
        assert unk == []
