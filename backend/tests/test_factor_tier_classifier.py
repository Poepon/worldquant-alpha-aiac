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
