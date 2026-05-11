"""Tests for backend.factor_tier_classifier — tier classification + utilities.

Coverage targets (mirrors plan §"Tier 边界澄清"):
- T1: single ts_op over a single known field
- T2: cross-sectional / smoothing wrapper applied to T1 inner
- T3: trade_when entry-filter wrapping T2 (or any valid tier)
- None: multi-field arithmetic, single-layer rank on raw field, malformed

Note: these tests run without DB-backed OperatorRegistry (registry is
empty in unit-test context), so the classifier falls back to the curated
_BUILTIN_TS_OPS / _BUILTIN_GROUP_OPS / _BUILTIN_PURE_XS_OPS sets.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.factor_tier_classifier import (
    _dedup_and_validate,
    classify_tier,
    extract_tier1_seed,
    is_known_field,
    is_t1_expression,
    populate_known_fields,
)


# Make sure the field cache is primed for tests referencing fundamental fields
populate_known_fields({
    "fnd6_newa1v1300_at",
    "fnd6_newa2v1300_oancf",
    "fnd6_newa2v1300_ni",
    "return_equity",
    "return_assets",
    "asset_growth_1y",
    "earnings_yield",
    "book_to_market",
})


class TestT1Classification:
    """T1 = single ts_op(field, ...) — single field, top-level ts operator."""

    @pytest.mark.parametrize(
        "expr",
        [
            "ts_rank(close, 20)",
            "ts_zscore(returns, 5)",
            "ts_decay_linear(volume, 10)",
            "ts_mean(vwap, 60)",
            "ts_delta(close, 1)",
            "ts_delay(open, 5)",
            "ts_arg_max(high, 20)",
            "ts_std_dev(returns, 30)",
            "ts_quantile(close, 252)",
            "ts_sum(volume, 20)",
            "ts_rank(fnd6_newa1v1300_at, 20)",
            "ts_zscore(return_equity, 60)",
            "  ts_mean(close, 20)  ",  # whitespace tolerant
            "ts_rank(close,20)",  # no spaces
        ],
    )
    def test_t1_positive(self, expr: str):
        assert classify_tier(expr) == 1, f"expected T1: {expr}"
        assert is_t1_expression(expr)


class TestT2Classification:
    """T2 = cross-sectional / smoothing wrapper applied to T1 inner."""

    @pytest.mark.parametrize(
        "expr",
        [
            # group-based
            "group_neutralize(ts_rank(close, 20), industry)",
            "group_rank(ts_zscore(returns, 5), sector)",
            "group_zscore(ts_rank(close, 20), market)",
            "group_normalize(ts_mean(volume, 20), industry)",
            "group_demean(ts_zscore(returns, 5), subindustry)",
            # pure cross-sectional
            "rank(ts_rank(close, 20))",
            "zscore(ts_zscore(returns, 5))",
            "normalize(ts_decay_linear(close, 10))",
            "quantile(ts_rank(close, 20))",
            "winsorize(ts_zscore(returns, 5), std=4)",
            "signed_power(ts_rank(close, 20), 0.5)",
            # smoothing ts (nested ts is wrapper, not T1)
            "ts_decay_linear(ts_rank(close, 5), 10)",
            "ts_mean(ts_zscore(returns, 5), 10)",
            "ts_std_dev(ts_rank(close, 20), 10)",
            "ts_max(ts_zscore(returns, 5), 20)",
        ],
    )
    def test_t2_positive(self, expr: str):
        assert classify_tier(expr) == 2, f"expected T2: {expr}"


class TestT3Classification:
    """T3 = trade_when(condition, <T1/T2 expr>, exit) entry-filter."""

    @pytest.mark.parametrize(
        "expr",
        [
            # T3 wrapping T2
            "trade_when(volume > ts_mean(volume, 240), group_neutralize(ts_rank(close, 20), industry), -1)",
            "trade_when(abs(returns) > ts_std_dev(returns, 60) * 2, rank(ts_rank(close, 20)), -1)",
            # T3 wrapping T1 directly (still classified as T3 — the trade_when wrapper is what matters)
            "trade_when(volume > ts_mean(volume, 240), ts_rank(close, 20), -1)",
        ],
    )
    def test_t3_positive(self, expr: str):
        assert classify_tier(expr) == 3, f"expected T3: {expr}"


class TestTierBoundaryNone:
    """Forms that intentionally fall outside the tier hierarchy → None."""

    @pytest.mark.parametrize(
        "expr",
        [
            # multi-field arithmetic
            "divide(ts_count_nans(volume, 20), ts_count_nans(close, 20))",
            "subtract(ts_av_diff(close, 30), ts_av_diff(open, 30))",
            # single-layer cross-sectional on raw field (no T1 inner)
            "rank(close)",
            "zscore(returns)",
            "winsorize(close, std=4)",
            # multi-field group_neutralize (inner is not T1)
            "group_neutralize(close - open, industry)",
            # malformed / empty
            "",
            "   ",
        ],
    )
    def test_none_classification(self, expr: str):
        assert classify_tier(expr) is None, f"expected None: {expr!r}"


class TestExtractTier1Seed:
    """extract_tier1_seed strips one wrapper layer; T3 → T2 (not T1)."""

    def test_t2_to_t1_strip(self):
        # Simple cases — strip wrapper, get T1 inner
        assert extract_tier1_seed("group_neutralize(ts_rank(close, 20), industry)") == "ts_rank(close, 20)"
        assert extract_tier1_seed("rank(ts_zscore(returns, 5))") == "ts_zscore(returns, 5)"
        assert extract_tier1_seed("winsorize(ts_zscore(returns, 5), std=4)") == "ts_zscore(returns, 5)"
        assert extract_tier1_seed("ts_decay_linear(ts_rank(close, 5), 10)") == "ts_rank(close, 5)"

    def test_t3_strip_yields_t2_not_t1(self):
        """Plan note: T3 → strip → T2 (not T1). Caller must call twice for T1."""
        t3 = "trade_when(volume > ts_mean(volume, 240), group_neutralize(ts_rank(close, 20), industry), -1)"
        # First strip → T2
        t2 = extract_tier1_seed(t3)
        assert classify_tier(t2) == 2, f"expected T2 after one strip, got {t2!r} (tier={classify_tier(t2)})"
        # Second strip → T1
        t1 = extract_tier1_seed(t2)
        assert classify_tier(t1) == 1

    def test_t1_strip_returns_none(self):
        # T1 has no wrapper to strip
        assert extract_tier1_seed("ts_rank(close, 20)") is None

    def test_unwrappable_returns_none(self):
        # Multi-field T2 — strip would yield non-T1 inner
        assert extract_tier1_seed("group_neutralize(close - open, industry)") is None
        assert extract_tier1_seed("") is None


class TestIsKnownField:
    def test_builtin_fields(self):
        assert is_known_field("close")
        assert is_known_field("volume")
        assert is_known_field("vwap")

    def test_db_cache_fields(self):
        assert is_known_field("fnd6_newa1v1300_at")
        assert is_known_field("return_equity")

    def test_group_tokens_not_fields(self):
        # Group built-ins like industry/sector are NOT fields
        assert not is_known_field("industry")
        assert not is_known_field("sector")
        assert not is_known_field("market")

    def test_literals_not_fields(self):
        assert not is_known_field("20")
        assert not is_known_field("-5")
        assert not is_known_field("0.5")
        assert not is_known_field("true")
        assert not is_known_field("nan")

    def test_operators_not_fields(self):
        # Known operators must NOT be classified as fields
        assert not is_known_field("ts_rank")
        assert not is_known_field("group_neutralize")
        assert not is_known_field("rank")
        assert not is_known_field("trade_when")


class TestDedupAndValidate:
    def test_drops_duplicates(self):
        variants = [
            {"expression": "ts_rank(close, 20)", "wrapper_kind": "a"},
            {"expression": "ts_rank(close, 20)", "wrapper_kind": "b"},  # duplicate
            {"expression": "ts_rank(volume, 20)", "wrapper_kind": "c"},
        ]
        out = _dedup_and_validate(variants, target_tier=1, region="USA")
        assert len(out) == 2
        exprs = {v["expression"] for v in out}
        assert exprs == {"ts_rank(close, 20)", "ts_rank(volume, 20)"}

    def test_drops_tier_mismatch(self):
        # Generated for T2 but actually a T1 expression — should be dropped
        variants = [
            {"expression": "ts_rank(close, 20)", "wrapper_kind": "x"},  # T1, not T2
            {"expression": "rank(ts_rank(close, 20))", "wrapper_kind": "y"},  # T2 ✓
        ]
        out = _dedup_and_validate(variants, target_tier=2, region="USA")
        assert len(out) == 1
        assert out[0]["expression"] == "rank(ts_rank(close, 20))"

    def test_drops_empty_expression(self):
        variants = [
            {"expression": "", "wrapper_kind": "x"},
            {"expression": "ts_rank(close, 20)", "wrapper_kind": "y"},
        ]
        out = _dedup_and_validate(variants, target_tier=1, region="USA")
        assert len(out) == 1


class TestRobustness:
    """Regressions for edge cases that have bitten past parsers."""

    def test_extra_whitespace(self):
        assert classify_tier("  ts_rank( close , 20 )  ") == 1
        assert classify_tier("group_neutralize(  ts_rank(close, 20)  ,  industry  )") == 2

    def test_outer_parens_stripped(self):
        assert classify_tier("(ts_rank(close, 20))") == 1
        assert classify_tier("((rank(ts_rank(close, 20))))") == 2

    def test_unbalanced_parens_returns_none(self):
        assert classify_tier("ts_rank(close, 20") is None
        assert classify_tier("ts_rank close, 20)") is None

    def test_top_level_arithmetic_not_classifiable(self):
        # f(x) + g(y) is not a single top-level call — should be None
        assert classify_tier("ts_rank(close, 20) + ts_rank(volume, 20)") is None


class TestNegationWrapperTransparency:
    """multiply(-1, X), multiply(X, -1), subtract(0, X) are sign-flip wrappers
    that don't change tier — produced by PR5 sign-flip retry and
    genetic_optimizer's _mutate_sign mutation."""

    def test_multiply_neg1_first_arg(self):
        # multiply(-1, T1) → T1
        assert classify_tier("multiply(-1, ts_rank(close, 20))") == 1
        assert classify_tier("multiply(-1, ts_zscore(returns, 5))") == 1

    def test_multiply_neg1_second_arg(self):
        # multiply(T1, -1) → T1 (BRAIN allows either argument order)
        assert classify_tier("multiply(ts_rank(close, 20), -1)") == 1

    def test_subtract_zero(self):
        # subtract(0, T1) → T1 (semantic: 0 - X = -X)
        assert classify_tier("subtract(0, ts_rank(close, 20))") == 1

    def test_negated_t2_stays_t2(self):
        # Negating a T2 expression keeps it T2 — sign-flip preserves tier semantics
        assert classify_tier("multiply(-1, group_neutralize(ts_rank(close, 20), industry))") == 2
        assert classify_tier("multiply(-1, rank(ts_rank(close, 20)))") == 2

    def test_negated_inside_t2_stays_t2(self):
        # T2 wrapper around a negated T1 — still T2 because inner classify_tier=1
        assert classify_tier("group_neutralize(multiply(-1, ts_rank(close, 20)), industry)") == 2
        assert classify_tier("rank(multiply(-1, ts_zscore(returns, 5)))") == 2

    def test_negated_t3_stays_t3(self):
        # trade_when wrapping a negated T1/T2 — outer stays T3
        expr = "trade_when(volume > ts_mean(volume, 240), multiply(-1, ts_rank(close, 20)), -1)"
        assert classify_tier(expr) == 3

    def test_double_negation_returns_to_original(self):
        # multiply(-1, multiply(-1, X)) recurses twice → X tier
        assert classify_tier("multiply(-1, multiply(-1, ts_rank(close, 20)))") == 1

    def test_negation_around_unclassifiable_stays_none(self):
        # If inner is not classifiable, negation stays None.
        # Plan v5+ #2 (2026-05-07): divide(<field>, <field>) and
        # subtract(<field>, <field>) are now Quasi-T1 via structural
        # patterns. Use add(...) for an inner that REALLY is unclassifiable
        # (add not in structural patterns). multiply(-1, add(close, eps))
        # is itself a multiply(<num>, <field-tree>) structure that *might*
        # match — let's pick something completely off-piste.
        # Wrap a verifiably-None inner:
        assert classify_tier("multiply(-1, add(close, eps))") is None

    def test_negation_around_quasi_t1_recurses_to_t1(self):
        # Quasi-T1 patterns are tier=1 (Plan v5+ §"Quasi-T1 准一阶白名单 v1.0"),
        # so negation around them is transparent and yields T1.
        assert classify_tier("multiply(-1, divide(close, volume))") == 1
        assert classify_tier("multiply(-1, subtract(close, vwap))") == 1
        assert classify_tier("subtract(0, divide(close, eps))") == 1

    def test_extract_tier1_seed_handles_negation(self):
        # multiply(-1, T2) → strip negation → strip T2 wrapper → T1 kernel
        result = extract_tier1_seed(
            "multiply(-1, group_neutralize(ts_rank(close, 20), industry))"
        )
        # strips the outer multiply(-1, ...) first, then strips group_neutralize → ts_rank(close, 20)
        assert result == "ts_rank(close, 20)"

    def test_extract_seed_negated_t2_inner(self):
        # T2 with negated T1 inside → strip outer → multiply(-1, ts_rank(close, 20))
        result = extract_tier1_seed(
            "group_neutralize(multiply(-1, ts_rank(close, 20)), industry)"
        )
        # The inner is multiply(-1, T1) which classifies as T1, so it's a valid kernel
        assert result == "multiply(-1, ts_rank(close, 20))"

    def test_negated_t1_passes_t1_predicate(self):
        assert is_t1_expression("multiply(-1, ts_rank(close, 20))") is True
        assert is_t1_expression("multiply(ts_rank(close, 20), -1)") is True


# -----------------------------------------------------------------------------
# Real-data fixture probes (plan §"验证 / 单元测试": 50+ real samples)
#
# brain_alphas_4135.json captures all alpha expressions from the BRAIN account
# (refresh via scripts/dump_brain_alphas_for_fixture.py). The classifier runs
# without DB-backed DataField cache here, so most fundamental-field-driven
# alphas resolve to None — that is expected. These probes assert form-level
# invariants that hold regardless of field-identification:
#   - every classification call is total (no exception, returns 1/2/3/None)
#   - every `trade_when(...)` expression classifies as T3 (or stays None when
#     the wrapped body itself is unclassifiable, but never as T1/T2)
#   - tier counts cover all three tiers — fixture isn't degenerate
# -----------------------------------------------------------------------------

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "brain_alphas_4135.json"


@pytest.fixture(scope="module")
def brain_alpha_expressions() -> list[str]:
    if not _FIXTURE_PATH.exists():
        pytest.skip(
            f"BRAIN fixture missing at {_FIXTURE_PATH}; "
            "run scripts/dump_brain_alphas_for_fixture.py to generate."
        )
    data = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    return [d["expression"] for d in data if d.get("expression")]


class TestQuasiT1:
    """Quasi-T1 whitelist (Plan v5+ §"Quasi-T1 准一阶白名单 v1.0") — 15 finance-classical
    two-field arithmetic patterns admitted as tier-1 for mining-pipeline purposes.

    Coverage targets:
    - All 15 v1.0 patterns classify as T1
    - Surface variants (whitespace, case, redundant parens) still match
    - Off-whitelist patterns reject (None)
    - Adding any forbidden op (statistical ts_op / group_op) rejects
    - Quasi-T1 wrapped in T2 wrapper (group_neutralize / rank / etc.) → T2
    - Strict T1 behavior unchanged
    """

    @pytest.mark.parametrize(
        "expr",
        [
            # Q-PR-01 synthetic returns
            "subtract(divide(close, ts_delay(close, 1)), 1)",
            "subtract(divide(close, ts_delay(close, 5)), 1)",  # any int for d
            # Q-ID-01/02/03 intraday
            "divide(subtract(high, low), close)",
            "divide(subtract(close, low), subtract(high, low))",
            "divide(subtract(close, open), open)",
            # Q-VL valuation ratios
            "divide(close, eps)",
            "divide(close, book_value_per_share)",
            "divide(ebit, ev)",
            # Q-PV price-volume
            "divide(close, volume)",
            "divide(amount, cap)",
            # Q-FN financial ratios
            "divide(cfo, net_income)",
            "divide(cfo, cap)",
            "divide(sales, total_assets)",
            "divide(total_debt, total_equity)",
            # Q-GP overnight gap
            "divide(subtract(open, ts_delay(close, 1)), ts_delay(close, 1))",
            # Q-CR close-vwap deviation
            "subtract(close, vwap)",
        ],
    )
    def test_all_15_patterns_classify_as_t1(self, expr: str):
        assert classify_tier(expr) == 1, f"expected T1 (quasi): {expr}"
        assert is_t1_expression(expr)

    @pytest.mark.parametrize(
        "expr",
        [
            # whitespace variants
            "divide( close , eps )",
            "divide(close,eps)",
            "  divide(close, eps)  ",
            # redundant outer parens
            "(divide(close, eps))",
            "((divide(close, eps)))",
            # case-insensitive op
            "DIVIDE(close, eps)",
            "Divide(close, eps)",
            # case-insensitive field
            "divide(CLOSE, EPS)",
            # nested whitespace
            "subtract( divide( close , ts_delay( close , 1 ) ) , 1 )",
        ],
    )
    def test_surface_variants_still_match(self, expr: str):
        assert classify_tier(expr) == 1, f"surface variant should still classify as T1: {expr!r}"

    @pytest.mark.parametrize(
        "expr",
        [
            # `add(<field>, <field>)` — `add` is intentionally NOT in the
            # structural Quasi-T1 patterns added in #2 (field_interactions),
            # because price+price-like additions don't have clean financial
            # semantics. Stays None.
            "add(close, eps)",
        ],
    )
    def test_off_whitelist_arithmetic_not_quasi(self, expr: str):
        assert classify_tier(expr) is None, f"off-whitelist must be None: {expr}"

    @pytest.mark.parametrize(
        "expr",
        [
            # Plan v5+ #2 (2026-05-07): these were formerly None (off-whitelist)
            # but are now Quasi-T1 via structural <field>-wildcard patterns.
            # The classifier's job is "is this T1 by SHAPE"; financial
            # meaningfulness comes from field_interactions.yaml curation.
            # Quality is determined by simulation, not classifier.
            "divide(close, returns)",   # structural divide(<field>,<field>)
            "multiply(eps, sales)",     # structural multiply(<field>,<field>)
            "subtract(volume, vwap)",   # structural subtract(<field>,<field>)
            "divide(close, low)",       # structural divide(<field>,<field>)
        ],
    )
    def test_structural_patterns_accept_arbitrary_pairs(self, expr: str):
        """#2 structural patterns: any divide/subtract/multiply of two
        bare-identifier fields → T1 by shape."""
        assert classify_tier(expr) == 1, f"structural pair should be T1: {expr}"

    @pytest.mark.parametrize(
        "expr",
        [
            # adding statistical ts_op anywhere disqualifies
            "divide(ts_mean(close, 5), eps)",
            "divide(close, ts_zscore(eps, 20))",
            # group op
            "group_neutralize(divide(close, eps), industry)",  # this is T2 not quasi T1
            # cross-sectional op nested
            "divide(rank(close), eps)",
            # trade_when nested
            "divide(close, trade_when(volume > 0, eps, -1))",
        ],
    )
    def test_forbidden_ops_in_tree_disqualify_quasi(self, expr: str):
        # group_neutralize(quasi_t1_inner, industry) is T2, not None
        if expr.startswith("group_neutralize"):
            assert classify_tier(expr) == 2, f"quasi-T1 wrapped in group_op should be T2: {expr}"
        else:
            assert classify_tier(expr) is None, f"forbidden op must reject: {expr}"

    @pytest.mark.parametrize(
        "wrapper_form",
        [
            # group_*-style T2 wrappers around quasi T1 → T2
            "group_neutralize({inner}, industry)",
            "group_rank({inner}, sector)",
            "group_zscore({inner}, market)",
            # pure cross-sectional T2 wrappers
            "rank({inner})",
            "zscore({inner})",
            "winsorize({inner}, std=4)",
            "signed_power({inner}, 0.5)",
            # smoothing ts T2 wrappers
            "ts_decay_linear({inner}, 10)",
            "ts_mean({inner}, 20)",
        ],
    )
    @pytest.mark.parametrize(
        "inner",
        [
            "divide(close, eps)",
            "divide(subtract(high, low), close)",
            "subtract(close, vwap)",
        ],
    )
    def test_quasi_t1_wrapped_in_t2_classifies_as_2(self, wrapper_form: str, inner: str):
        expr = wrapper_form.format(inner=inner)
        assert classify_tier(expr) == 2, f"expected T2 (quasi-T1 + wrapper): {expr}"

    def test_quasi_t1_in_residualize_form_classifies_as_2(self):
        """subtract(quasi_T1, group_mean(quasi_T1, weight, group)) — cap-weighted
        residualize over a quasi-T1 kernel. Treated as a single T2 wrapper layer.
        """
        expr = "subtract(divide(close, eps), group_mean(divide(close, eps), cap, industry))"
        assert classify_tier(expr) == 2, f"residualize over quasi-T1 must be T2: {expr}"

    def test_quasi_t1_in_t3_trade_when_classifies_as_3(self):
        expr = "trade_when(volume > ts_mean(volume, 240), divide(close, eps), -1)"
        assert classify_tier(expr) == 3

    @pytest.mark.parametrize(
        "expr",
        [
            # strict T1 behavior unchanged
            "ts_rank(close, 20)",
            "ts_zscore(returns, 5)",
            "ts_decay_linear(volume, 10)",
            "ts_mean(vwap, 60)",
        ],
    )
    def test_strict_t1_unaffected(self, expr: str):
        assert classify_tier(expr) == 1

    @pytest.mark.parametrize(
        "expr",
        [
            # tier=None still works for these
            "rank(close)",
            "zscore(returns)",
            "subtract(ts_av_diff(close, 30), ts_av_diff(open, 30))",
        ],
    )
    def test_existing_none_behavior_unaffected(self, expr: str):
        assert classify_tier(expr) is None


class TestCompositeT2:
    """V-22.6 (2026-05-12) composite-field T2 path.

    Covers `ts_op(<preprocess>(<quasi_t1>, ...), w)` shapes where preprocess is
    any combination of winsorize / ts_backfill layers (or absent). These are
    NOT strict T2 (outer is statistical ts_op, not a smoothing/group wrapper)
    nor strict T1 (inner is multi-field arithmetic, not a single field), so
    they need their own classifier path.
    """

    @pytest.mark.parametrize(
        "expr",
        [
            # ts_op directly on Quasi-T1 (no preprocess)
            "ts_rank(divide(ebit, ev), 20)",
            "ts_zscore(divide(subtract(high, low), close), 60)",
            "ts_rank(divide(volume, cap), 20)",
            # Single preprocess layer
            "ts_rank(ts_backfill(divide(ebit, ev), 120), 20)",
            "ts_rank(winsorize(divide(ebit, ev), std=4), 20)",
            # Full V-22.6 wrap (winsorize ∘ ts_backfill ∘ quasi_t1)
            "ts_rank(winsorize(ts_backfill(divide(ebit, ev), 120), std=4), 20)",
            "ts_zscore(winsorize(ts_backfill(divide(subtract(high, low), close), 120), std=4), 60)",
            # Decay-linear outer (smoothing op, also covered by _is_t2_via_wrapper)
            "ts_decay_linear(winsorize(ts_backfill(divide(volume, cap), 120), std=4), 20)",
        ],
    )
    def test_composite_t2_classifies_as_2(self, expr: str):
        assert classify_tier(expr) == 2, f"expected T2 (composite): {expr}"

    def test_negation_of_composite_t2_still_t2(self):
        expr = "multiply(-1, ts_rank(divide(ebit, ev), 20))"
        assert classify_tier(expr) == 2

    @pytest.mark.parametrize(
        "expr",
        [
            # Stat ts_op inside preprocess → must reject (not bare Quasi-T1 inside)
            "ts_rank(ts_backfill(ts_mean(close, 5), 120), 20)",
            # Group op inside preprocess → must reject
            "ts_rank(ts_backfill(group_neutralize(close, industry), 120), 20)",
            # Outer is non-ts op → not composite T2 path
            "rank(divide(ebit, ev))",
        ],
    )
    def test_invalid_composite_forms_reject(self, expr: str):
        # Should NOT be classified as T2 via the composite path; some may be
        # None outright, none should be 1.
        tier = classify_tier(expr)
        assert tier != 1, f"composite-T2 rejects must not be T1: {expr}"

    def test_strict_t1_still_t1(self):
        # T1 must remain T1 (no accidental promotion to T2 via composite path)
        assert classify_tier("ts_rank(close, 20)") == 1
        assert classify_tier("ts_zscore(returns, 5)") == 1

    def test_strict_t2_via_wrapper_still_t2(self):
        # group_neutralize over T1 inner — pre-existing T2 path
        assert classify_tier("group_neutralize(ts_rank(close, 20), industry)") == 2


class TestCompositeFieldsLoader:
    """V-22.6 composite_fields loader sanity checks."""

    def test_yaml_loads(self):
        from backend.agents.seed_pool.composite_fields import list_composites
        items = list_composites()
        assert len(items) >= 10, "Expected at least 10 composites in seed pool"
        # Schema sanity
        for c in items:
            assert c.get("name"), f"composite missing name: {c}"
            assert c.get("composite_expr"), f"composite missing expr: {c}"
            assert c.get("required_fields"), f"composite missing fields: {c}"
            assert c.get("family"), f"composite missing family: {c}"

    def test_wrap_with_preprocess_shape(self):
        from backend.agents.seed_pool.composite_fields import wrap_with_preprocess
        out = wrap_with_preprocess("divide(ebit, ev)")
        assert out == "winsorize(ts_backfill(divide(ebit, ev), 120), std=4)"

    def test_eligibility_universal_fields_only(self):
        """Composites needing only PV fields are eligible when no fundamental
        fields are present in the available_fields set."""
        from backend.agents.seed_pool.composite_fields import (
            generate_composite_t1_candidates,
        )
        cands = generate_composite_t1_candidates(
            ts_ops=["ts_rank"],
            windows=[20],
            available_fields=[],  # no fundamental
            region="USA",
            max_per_composite=1,
        )
        # Only PV-only composites (intraday / gap / liquidity) should survive
        assert len(cands) > 0
        for c in cands:
            assert c["op"].startswith("composite_"), c
            assert c["field"].startswith("_composite_"), c

    def test_eligibility_with_fundamentals(self):
        """All 15 composites fire when fundamentals + PV are available."""
        from backend.agents.seed_pool.composite_fields import (
            generate_composite_t1_candidates, total_composite_count,
        )
        cands = generate_composite_t1_candidates(
            ts_ops=["ts_rank"],
            windows=[20],
            available_fields=[
                "eps", "ebit", "enterprise_value", "book_value_per_share_2",
                "revenue", "cash_flow_from_operations", "net_income_total_2",
                "fnd6_newa1v1300_at", "debt_lt", "fnd6_teq",
            ],
            region="USA",
            max_per_composite=1,
        )
        assert len(cands) == total_composite_count()

    def test_generated_candidates_classify_as_t2(self):
        """Every generated composite candidate must classify as T2 — without
        this, _dedup_and_validate (with allowed_tiers={1,2}) would drop them
        all and the V-22.6 branch contributes 0 alphas to mining."""
        from backend.agents.seed_pool.composite_fields import (
            generate_composite_t1_candidates,
        )
        cands = generate_composite_t1_candidates(
            ts_ops=["ts_rank", "ts_zscore"],
            windows=[20, 60],
            available_fields=[
                "eps", "ebit", "enterprise_value", "book_value_per_share_2",
                "revenue", "cash_flow_from_operations", "net_income_total_2",
                "fnd6_newa1v1300_at", "debt_lt", "fnd6_teq",
            ],
            region="USA",
            max_per_composite=2,
            apply_preprocess=True,  # legacy V-22.6 wrap form
        )
        assert cands, "expected at least one composite candidate"
        mismatched = [c for c in cands if classify_tier(c["expression"]) != 2]
        assert not mismatched, (
            f"composites must classify as T2; "
            f"{len(mismatched)} failed: {[c['expression'][:100] for c in mismatched[:3]]}"
        )

    def test_bare_form_default_no_preprocess(self):
        """V-22.6.1 default: bare `ts_op(<composite>, w)` without winsorize/
        ts_backfill wrap, to stay under BRAIN's 8-operator complexity limit.
        Bare composites must still classify as T2 via the classifier's
        _peel_composite_preprocess transparent layer."""
        from backend.agents.seed_pool.composite_fields import (
            generate_composite_t1_candidates,
        )
        cands = generate_composite_t1_candidates(
            ts_ops=["ts_rank"],
            windows=[20],
            available_fields=["eps", "ebit", "enterprise_value"],
            region="USA",
            max_per_composite=2,
            # apply_preprocess=False is the new default
        )
        assert cands, "expected at least one bare composite candidate"
        # Sanity: no preprocess wrap in the emitted expressions
        for c in cands:
            assert "winsorize" not in c["expression"], (
                f"bare form must not include winsorize: {c['expression']}"
            )
            assert "ts_backfill" not in c["expression"], (
                f"bare form must not include ts_backfill: {c['expression']}"
            )
            assert classify_tier(c["expression"]) == 2, (
                f"bare composite must classify as T2: {c['expression']}"
            )


class TestRealDataFixture:
    def test_classifier_total_on_real_alphas(self, brain_alpha_expressions):
        """classify_tier never raises on any real BRAIN expression."""
        valid = {None, 1, 2, 3}
        for expr in brain_alpha_expressions:
            tier = classify_tier(expr)
            assert tier in valid, f"unexpected tier={tier!r} for {expr[:80]}"

    def test_trade_when_never_misclassified_as_lower_tier(
        self, brain_alpha_expressions
    ):
        """trade_when at top level is T3 if the body is classifiable, else None.
        It must never be T1 or T2 — that would be a hierarchy violation."""
        tw = [e for e in brain_alpha_expressions if e.lstrip().startswith("trade_when(")]
        assert len(tw) > 0, "fixture has no trade_when samples — refresh fixture"
        for expr in tw:
            tier = classify_tier(expr)
            assert tier in (3, None), (
                f"trade_when classified as T{tier} (must be 3 or None): {expr[:120]}"
            )

    def test_tier_distribution_non_degenerate(self, brain_alpha_expressions):
        """Sanity: fixture covers all three tiers with meaningful counts."""
        counts = {1: 0, 2: 0, 3: 0, None: 0}
        for expr in brain_alpha_expressions:
            counts[classify_tier(expr)] += 1
        # Lower bounds chosen well below current observed counts (T1=213, T2=148, T3=65)
        # so a fixture refresh that shifts the mix doesn't false-fail the test.
        assert counts[1] >= 50, f"too few T1 samples: {counts[1]}"
        assert counts[2] >= 30, f"too few T2 samples: {counts[2]}"
        assert counts[3] >= 10, f"too few T3 samples: {counts[3]}"
        assert sum(counts.values()) == len(brain_alpha_expressions)

    def test_extract_tier1_seed_on_real_t2_alphas(self, brain_alpha_expressions):
        """For every T2-classified alpha in fixture, extract_tier1_seed should
        return either a T1 expression or None (per plan §"factor_tier_classifier"
        — the helper does single-layer strip; T2 → T1 is its primary use case.
        T3 input yields a T2-shape, not a T1, so the helper is documented to
        only be called on T2 expressions for the cold-start RAG fallback)."""
        t2_exprs = [e for e in brain_alpha_expressions if classify_tier(e) == 2]
        non_t1_seeds = []
        for expr in t2_exprs:
            seed = extract_tier1_seed(expr)
            if seed is not None and not is_t1_expression(seed):
                non_t1_seeds.append((expr[:80], seed[:80]))
        # Some T2 alphas may have unrecognized fundamental fields, causing the
        # stripped kernel to fail is_t1_expression — that's acceptable since
        # the RAG fallback drops such kernels. But the count should be tiny
        # relative to total T2 — most strips should produce valid T1 form.
        assert len(non_t1_seeds) <= len(t2_exprs) * 0.5, (
            f"too many T2 strips returned non-T1 seeds "
            f"({len(non_t1_seeds)}/{len(t2_exprs)}); first few: {non_t1_seeds[:3]}"
        )
