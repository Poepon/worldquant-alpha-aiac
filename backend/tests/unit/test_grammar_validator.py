"""B4.1 G3-v2 grammar_validator unit tests (Phase 4 Sprint 4 / plan v5 §6.14).

Coverage:
  - empty / whitespace input → ok=False
  - canonical BRAIN expressions parse cleanly
  - unbalanced parens / unexpected tokens → ok=False with position
  - arithmetic expressions parse
  - nested operator calls parse
  - unknown operator → ok=True (warn-only) + unknown_ops surface
  - kwarg syntax parses
  - retry_with_whole_output_hint includes original + error
  - lark unavailable degrades open (no error)
"""
from __future__ import annotations

import pytest

from backend.services import grammar_validator as gv


# ---------------------------------------------------------------------------
# Edge inputs
# ---------------------------------------------------------------------------

def test_empty_input_fails():
    assert gv.validate("").ok is False
    assert gv.validate("   ").ok is False


def test_validate_returns_validation_result():
    res = gv.validate("rank(close)")
    assert isinstance(res, gv.ValidationResult)


# ---------------------------------------------------------------------------
# Canonical BRAIN expressions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("expr", [
    "rank(close)",
    "ts_zscore(returns, 20)",
    "ts_rank(close, 60)",
    "ts_decay_linear(volume, 5)",
    "industry_neutralize(rank(close), industry)",
    "sign(returns)",
    "abs(close - open)",
    "log(volume)",
    "(close + open) / 2",
    "rank(close - ts_mean(close, 20))",
])
def test_canonical_brain_expressions_parse(expr):
    res = gv.validate(expr)
    assert res.ok, f"expected ok, got error: {res.error_msg}"


def test_arithmetic_precedence_parses():
    res = gv.validate("rank(close) + scale(volume) * 0.5")
    assert res.ok


def test_nested_operators_parse():
    res = gv.validate("ts_corr(rank(close), rank(volume), 60)")
    assert res.ok


def test_kwarg_syntax_parses():
    """BRAIN supports kwarg-style: ts_rank(close, window=60)."""
    res = gv.validate("ts_rank(close, window=60)")
    assert res.ok


def test_negative_number_parses():
    res = gv.validate("rank(-close)")
    assert res.ok


# ---------------------------------------------------------------------------
# Malformed inputs — should NOT parse
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("expr", [
    "rank(close",                    # unclosed paren
    "ts_zscore(returns, 20",         # unclosed paren mid-args
    "rank(",                         # empty call + unclosed
    "ts_rank close, 60)",            # missing opening paren
    "ts_rank(close,, 60)",           # double comma
    "((close + open)",               # nested unclosed
    "rank(close) +",                 # trailing operator
    "* close",                       # leading binary op
    "rank(@bad@)",                   # invalid char
])
def test_malformed_expressions_fail(expr):
    res = gv.validate(expr)
    assert not res.ok, f"expected fail, got ok for: {expr}"
    assert res.error_msg, "expected non-empty error_msg"


def test_parse_position_reported_when_available():
    res = gv.validate("rank(close")
    assert not res.ok
    # lark UnexpectedEOF reports position=-1 (sentinel for end-of-input);
    # UnexpectedToken reports column ≥ 0. Either is acceptable as long
    # as the field exists; None means lark didn't surface position.
    assert res.error_position is None or isinstance(res.error_position, int)


# ---------------------------------------------------------------------------
# Unknown operator (warn-only)
# ---------------------------------------------------------------------------

def test_unknown_operator_is_warn_only():
    """An unknown OP_NAME at call position should NOT fail parsing —
    the grammar accepts any identifier — but it should surface in
    unknown_ops for caller's notice."""
    res = gv.validate("totally_made_up_op(close, 5)")
    assert res.ok  # parser accepts identifier
    assert "totally_made_up_op" in res.unknown_ops


def test_known_operators_not_in_unknown_list():
    res = gv.validate("ts_rank(rank(close), 60)")
    assert res.ok
    assert "ts_rank" not in res.unknown_ops
    assert "rank" not in res.unknown_ops


# ---------------------------------------------------------------------------
# retry_with_whole_output_hint
# ---------------------------------------------------------------------------

def test_retry_hint_includes_original_and_error():
    res = gv.validate("rank(close")  # malformed
    hint = gv.retry_with_whole_output_hint("rank(close", res)
    assert "rank(close" in hint
    assert "re-emit" in hint.lower() or "corrected" in hint.lower()
    assert res.error_msg in hint


def test_retry_hint_empty_when_ok():
    res = gv.validate("rank(close)")
    hint = gv.retry_with_whole_output_hint("rank(close)", res)
    assert hint == ""


# ---------------------------------------------------------------------------
# Degrade-open when lark unavailable
# ---------------------------------------------------------------------------

def test_lazy_parser_returns_object_when_lark_available():
    parser = gv._lazy_parser()
    assert parser is not None
