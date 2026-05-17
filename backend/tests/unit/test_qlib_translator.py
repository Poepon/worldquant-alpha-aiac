"""Unit tests for backend/qlib_translator.py (Phase 0 Q3, v1.5 single-version).

Covers:
    - Each mapped operator translates to its BRAIN equivalent
    - Three trap fixes (Ref negative arg / Rank time-series / $-prefix strip)
    - End-to-end nested-expression translation
    - parse_pattern_operators happy-path on translated outputs
    - translate_batch error isolation (unknown op doesn't kill the batch)

NO pyqlib dependency — all inputs are hand-written Qlib-style strings.
"""
from __future__ import annotations

import pytest

from backend.external_knowledge import parse_pattern_operators
from backend.qlib_translator import (
    QLIB_TO_BRAIN_OPERATORS,
    _split_args,
    _strip_field_prefix,
    translate,
    translate_batch,
)


# --------------------------------------------------------------------------- #
# 1. Each mapped operator — round-trip a minimal call
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("qlib_op,brain_op", [
    ("Mean",       "ts_mean"),
    ("Sum",        "ts_sum"),
    ("Std",        "ts_std_dev"),
    ("Max",        "ts_max"),
    ("Min",        "ts_min"),
    ("Corr",       "ts_corr"),
    ("Cov",        "ts_covariance"),
    ("IdxMax",     "ts_argmax"),
    ("IdxMin",     "ts_argmin"),
    ("ZScore",     "ts_zscore"),
    ("WMA",        "ts_decay_linear"),
])
def test_rolling_operator_round_trip(qlib_op, brain_op):
    """Rolling Qlib op with (datafield, window) args."""
    qlib_expr = f"{qlib_op}($close, 20)"
    result = translate(qlib_expr)
    assert result == f"{brain_op}(close, 20)", f"{qlib_expr} → {result}"


@pytest.mark.parametrize("qlib_op,brain_op", [
    ("Add",   "add"),
    ("Sub",   "subtract"),
    ("Mul",   "multiply"),
    ("Div",   "divide"),
    ("Less",  "less"),
    ("Greater", "greater"),
])
def test_binary_operator_round_trip(qlib_op, brain_op):
    """Element-wise binary ops with two datafield args."""
    qlib_expr = f"{qlib_op}($close, $open)"
    result = translate(qlib_expr)
    assert result == f"{brain_op}(close, open)", f"{qlib_expr} → {result}"


@pytest.mark.parametrize("qlib_op,brain_op", [
    ("Abs",  "abs"),
    ("Sign", "sign"),
    ("Log",  "log"),
])
def test_unary_operator_round_trip(qlib_op, brain_op):
    qlib_expr = f"{qlib_op}($close)"
    assert translate(qlib_expr) == f"{brain_op}(close)"


# --------------------------------------------------------------------------- #
# 2. Three trap fixes
# --------------------------------------------------------------------------- #
def test_trap_ref_negative_arg():
    """TRAP #1: Ref(x, -N) absolute-values the negative window."""
    assert translate("Ref($close, -5)") == "ts_delay(close, 5)"
    assert translate("Ref($close, 5)") == "ts_delay(close, 5)"
    # nested
    assert translate("Ref(Mean($close, 10), -3)") == "ts_delay(ts_mean(close, 10), 3)"


def test_trap_rank_is_time_series():
    """TRAP #2: Qlib Rank IS time-series percentile, maps to BRAIN ts_rank
    (NOT BRAIN rank, which is cross-sectional)."""
    assert translate("Rank($close, 20)") == "ts_rank(close, 20)"


def test_trap_dollar_prefix_stripped():
    """TRAP #3: $close → close, $vwap → vwap, etc."""
    assert _strip_field_prefix("$close") == "close"
    assert _strip_field_prefix("$volume") == "volume"
    assert _strip_field_prefix("$vwap") == "vwap"
    # Multiple in one expression
    assert _strip_field_prefix("($close - $open) / $close") == "(close - open) / close"


# --------------------------------------------------------------------------- #
# 3. End-to-end nested expressions
# --------------------------------------------------------------------------- #
def test_nested_rolling_calls():
    """Std(Mean(x, w1), w2) — nested time-series ops."""
    result = translate("Std(Mean($close, 5), 10)")
    assert result == "ts_std_dev(ts_mean(close, 5), 10)"


def test_nested_with_arithmetic():
    """Mixed function call + arithmetic + datafields."""
    result = translate("Corr(($close - $open) / $open, $volume, 30)")
    assert result == "ts_corr((close - open) / open, volume, 30)"


def test_if_else_translation():
    """If(cond, a, b) → if_else with recursive args."""
    result = translate("If(Less($close, $open), 1, -1)")
    assert result == "if_else(less(close, open), 1, -1)"


def test_leaf_expression_passes_through():
    """Plain arithmetic without Qlib ops just gets $-prefix removed."""
    assert translate("($close - $open) / ($high - $low)") == "(close - open) / (high - low)"
    assert translate("1") == "1"
    assert translate("$close") == "close"


# --------------------------------------------------------------------------- #
# 4. parse_pattern_operators on translated output
# --------------------------------------------------------------------------- #
def test_translated_outputs_have_extractable_operators():
    """Round-trip Qlib → BRAIN → parse_pattern_operators should yield ops."""
    cases = [
        ("Mean($close, 20)",           {"ts_mean"}),
        ("Corr($close, $volume, 30)",  {"ts_corr"}),
        ("Std(Mean($close, 5), 10)",   {"ts_std_dev", "ts_mean"}),
        ("If(Less($close, $open), 1, -1)", {"if_else", "less"}),
    ]
    for qlib_expr, expected_ops in cases:
        brain_expr = translate(qlib_expr)
        actual_ops = set(parse_pattern_operators(brain_expr))
        assert expected_ops.issubset(actual_ops), (
            f"qlib={qlib_expr} brain={brain_expr} expected_ops={expected_ops} actual={actual_ops}"
        )


# --------------------------------------------------------------------------- #
# 5. translate_batch error isolation
# --------------------------------------------------------------------------- #
def test_translate_batch_isolates_unknown_op():
    """Unknown Qlib operator → ("", error_msg); other rows still translate."""
    inputs = [
        "Mean($close, 5)",
        "FrobulateXyz($close, 99)",  # not in QLIB_TO_BRAIN_OPERATORS
        "Sum($volume, 10)",
    ]
    out = translate_batch(inputs)
    assert out[0] == ("ts_mean(close, 5)", None)
    assert out[1][0] == "" and "FrobulateXyz" in out[1][1]
    assert out[2] == ("ts_sum(volume, 10)", None)


def test_translate_batch_isolates_bad_arity():
    """A wrong-arity Ref call raises NotImplementedError; doesn't kill the batch."""
    inputs = ["Mean($close, 20)", "Ref($close)", "Sum($volume, 5)"]
    out = translate_batch(inputs)
    assert out[0] == ("ts_mean(close, 20)", None)
    assert out[1][0] == "" and "Ref expects 2 args" in out[1][1]
    assert out[2] == ("ts_sum(volume, 5)", None)


# --------------------------------------------------------------------------- #
# 6. _split_args paren-respecting splitter
# --------------------------------------------------------------------------- #
def test_split_args_respects_parens():
    assert _split_args("a, b, c") == ["a", "b", "c"]
    assert _split_args("a, f(x, y), c") == ["a", "f(x, y)", "c"]
    assert _split_args("f(g(h(1, 2), 3), 4), 5") == ["f(g(h(1, 2), 3), 4)", "5"]
    assert _split_args("") == []


# --------------------------------------------------------------------------- #
# 7. Mapping table sanity — no dangling None/empty values
# --------------------------------------------------------------------------- #
def test_no_empty_string_or_none_mappings():
    bad = [(q, b) for q, b in QLIB_TO_BRAIN_OPERATORS.items()
           if not b or not isinstance(b, str)]
    assert not bad, f"Found dangling mappings: {bad}"
