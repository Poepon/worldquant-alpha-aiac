"""Unit tests for P2-B Five Pillars factor classifier.

来源: docs/alphagbm_skills_research_2026-05-15.md skill `compare`.

Pure-function module (no DB / Celery / FS dependencies). Tests cover the
normalize_pillar canonicalisation chain, _classify_field word-boundary
patterns (S2 fix — ``close_buy_volume`` must NOT be classified as
momentum), and the four-stage infer_pillar priority.
"""
from __future__ import annotations

import pytest

from backend.pillar_classifier import (
    FIELD_PATTERNS,
    OPERATOR_TO_PILLAR,
    PILLAR_VALUES,
    _classify_field,
    _extract_operators,
    _extract_field_tokens,
    infer_pillar,
    normalize_pillar,
)


# ---------------------------------------------------------------------------
# PILLAR_VALUES / OPERATOR_TO_PILLAR catalog invariants
# ---------------------------------------------------------------------------

class TestPillarValues:
    def test_six_pillars_present(self):
        assert PILLAR_VALUES == {
            "momentum", "value", "quality",
            "volatility", "sentiment", "other",
        }

    def test_operator_map_values_are_pillar_subsets(self):
        """Every operator's pillar set must be a subset of PILLAR_VALUES."""
        for op, pillars in OPERATOR_TO_PILLAR.items():
            assert pillars <= PILLAR_VALUES, (
                f"operator {op!r} maps to non-pillar values: "
                f"{pillars - PILLAR_VALUES}"
            )


# ---------------------------------------------------------------------------
# normalize_pillar
# ---------------------------------------------------------------------------

class TestNormalizePillar:
    def test_canonical_passes_through(self):
        for p in PILLAR_VALUES:
            assert normalize_pillar(p) == p

    def test_uppercase_lowercased(self):
        assert normalize_pillar("MOMENTUM") == "momentum"
        assert normalize_pillar("Value") == "value"

    def test_alias_mean_reversion_maps_to_momentum(self):
        assert normalize_pillar("mean_reversion") == "momentum"
        assert normalize_pillar("reversal") == "momentum"

    def test_alias_vol_maps_to_volatility(self):
        assert normalize_pillar("vol") == "volatility"
        assert normalize_pillar("risk") == "volatility"

    def test_unknown_returns_none(self):
        assert normalize_pillar("garbage") is None
        assert normalize_pillar("") is None
        assert normalize_pillar(None) is None
        assert normalize_pillar(123) is None  # non-string
        assert normalize_pillar("   ") is None


# ---------------------------------------------------------------------------
# _classify_field — S2 word-boundary regex
# ---------------------------------------------------------------------------

class TestClassifyField:
    def test_pure_close_is_momentum(self):
        assert _classify_field("close") == "momentum"

    def test_close_buy_volume_is_not_momentum(self):
        """S2 fix: ``close_buy_volume`` must NOT match ``^close$`` even though
        it shares the ``close`` prefix in substring sense."""
        result = _classify_field("close_buy_volume")
        assert result != "momentum"

    def test_field_pattern_word_boundary(self):
        """S2 verification: an analyst-style field containing 'close' as a
        non-anchor substring must not be misrouted into momentum."""
        # ``analyst_close_estimate`` doesn't start with 'close' (^close$) and
        # doesn't start with 'anl' either; falls through to None.
        result = _classify_field("analyst_close_estimate")
        assert result != "momentum"

    def test_eps_is_value(self):
        assert _classify_field("eps") == "value"

    def test_roe_is_quality(self):
        assert _classify_field("roe") == "quality"
        assert _classify_field("roic") == "quality"

    def test_snt_prefix_is_sentiment(self):
        assert _classify_field("snt1_metric") == "sentiment"
        assert _classify_field("anl4_revision") == "sentiment"

    def test_implied_volatility_is_volatility(self):
        assert _classify_field("implied_volatility") == "volatility"
        assert _classify_field("iv_30d") == "volatility"
        assert _classify_field("opt8_iv") == "volatility"


# ---------------------------------------------------------------------------
# _extract_operators / _extract_field_tokens
# ---------------------------------------------------------------------------

class TestExtractors:
    def test_extract_operators_simple(self):
        expr = "ts_rank(ts_delta(close, 5), 20)"
        ops = _extract_operators(expr)
        assert "ts_rank" in ops
        assert "ts_delta" in ops

    def test_extract_field_tokens_excludes_ops(self):
        expr = "ts_rank(close, 5)"
        fields = _extract_field_tokens(expr)
        assert "close" in fields
        assert "ts_rank" not in fields

    def test_extract_operators_empty_string(self):
        assert _extract_operators("") == []
        assert _extract_field_tokens("") == []


# ---------------------------------------------------------------------------
# infer_pillar
# ---------------------------------------------------------------------------

class TestInferPillar:
    def test_llm_emit_wins(self):
        """Stage 1: LLM-emit pillar trumps everything else."""
        result = infer_pillar(
            hypothesis_pillar="value",
            key_fields=["close"],  # would otherwise be momentum
            suggested_operators=["ts_delta"],
        )
        assert result == "value"

    def test_llm_emit_alias_normalized(self):
        result = infer_pillar(hypothesis_pillar="mean_reversion")
        assert result == "momentum"

    def test_expected_signal_value_hint(self):
        """Stage 2: expected_signal=value short-circuits to value."""
        result = infer_pillar(
            key_fields=["close"],
            suggested_operators=["ts_delta"],
            expected_signal="value",
        )
        assert result == "value"

    def test_expected_signal_momentum_hint(self):
        result = infer_pillar(expected_signal="mean_reversion")
        assert result == "momentum"

    def test_voting_eps_book_value_yields_value(self):
        """Stage 3: ts_mean (momentum/value split) + 2 value fields → value."""
        result = infer_pillar(
            key_fields=["eps", "book_value"],
            suggested_operators=["ts_mean"],
        )
        assert result == "value"

    def test_voting_volatility_via_operators(self):
        result = infer_pillar(
            suggested_operators=["ts_std_dev", "ts_skewness"],
            key_fields=["close"],  # 2.0 momentum vs 2.0 volatility ops
        )
        # 2 ops volatility (2.0) + 1 field momentum (2.0) — tie; but field
        # weight 2× per op makes momentum tie. Check it's one of them.
        assert result in ("volatility", "momentum")

    def test_voting_low_signal_falls_to_other(self):
        """Stage 4: bare neutral operators with no field hints → other."""
        result = infer_pillar(suggested_operators=["rank", "log"])
        assert result == "other"

    def test_empty_inputs_returns_other(self):
        result = infer_pillar()
        assert result == "other"

    def test_never_returns_none(self):
        """infer_pillar must always return a string ∈ PILLAR_VALUES."""
        result = infer_pillar()
        assert result in PILLAR_VALUES
        result = infer_pillar(expression="garbage_field()")
        assert result in PILLAR_VALUES

    def test_expression_only_input(self):
        """Pulls operators + fields from a bare expression string."""
        result = infer_pillar(
            expression="ts_delta(close, 5)",
        )
        # ts_delta = momentum (1.0) + close = momentum (2.0)
        assert result == "momentum"
