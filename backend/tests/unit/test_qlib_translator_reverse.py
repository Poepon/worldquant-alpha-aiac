"""Phase 3 Q10 PR1a: brain_to_qlib reverse translator (2026-05-18).

Plan: ~/.claude/plans/phase3-q10-pyqlib-prescreen-2026-05-18.md §7.1.

Covers reverse-translation operator/field mapping + untranslatable cascade
(unknown ops/fields → None) + ts_delay sign-flip + nested calls + lru_cache.

These tests do NOT require pyqlib or live OHLCV data — translator is pure
string manipulation.
"""
from __future__ import annotations

import pytest

from backend.qlib_translator import (
    BRAIN_TO_QLIB_FIELD,
    BRAIN_TO_QLIB_OPERATORS,
    brain_to_qlib,
)


# ---------------------------------------------------------------------------
# §7.1 #1-#3 — operator basics + TRAP #1 sign-flip
# ---------------------------------------------------------------------------

def test_brain_to_qlib_ts_delay_sign_flip():
    """ts_delay(close, 5) → Ref($close, -5) (TRAP #1 reversed)."""
    assert brain_to_qlib("ts_delay(close, 5)") == "Ref($close, -5)"


def test_brain_to_qlib_ts_rank_no_translation():
    """ts_rank(volume, 20) → Rank($volume, 20) (no semantic flip — TRAP #2)."""
    assert brain_to_qlib("ts_rank(volume, 20)") == "Rank($volume, 20)"


def test_brain_to_qlib_negative_window_arg():
    """ts_delay(close, -3) → Ref($close, 3) (handle negative both directions)."""
    assert brain_to_qlib("ts_delay(close, -3)") == "Ref($close, 3)"


# ---------------------------------------------------------------------------
# §7.1 #4-#6 — field translation
# ---------------------------------------------------------------------------

def test_brain_to_qlib_field_close():
    """Bare `close` → $close in a leaf-arg position via nested rewrite."""
    assert brain_to_qlib("ts_mean(close, 20)") == "Mean($close, 20)"


def test_brain_to_qlib_field_returns_synthetic():
    """returns = (today - yesterday) / yesterday = $close/Ref($close,-1) - 1.

    Convention check: qlib_prescreen.py:212 uses target/anchor - 1.
    Earlier form Ref($close,-1)/$close-1 was inverted (yesterday/today - 1)
    which would sign-flip Sharpe/IC for any alpha using BRAIN `returns` field.
    """
    assert brain_to_qlib("ts_mean(returns, 20)") == "Mean($close/Ref($close,-1)-1, 20)"


def test_brain_to_qlib_adv20_synthetic():
    """adv20 is a synthetic — Mean($volume, 20)."""
    assert brain_to_qlib("ts_rank(adv20, 10)") == "Rank(Mean($volume, 20), 10)"


# ---------------------------------------------------------------------------
# §7.1 #7-#10 — untranslatable cascade
# ---------------------------------------------------------------------------

def test_brain_to_qlib_unknown_field_returns_none():
    """fnd28_assets is not in the whitelist → None."""
    # Even wrapped in a known op, cascade returns None
    assert brain_to_qlib("ts_mean(fnd28_assets, 20)") is None


def test_brain_to_qlib_group_neutralize_returns_none():
    """group_neutralize is explicitly None in the table."""
    assert brain_to_qlib("group_neutralize(close, sector)") is None


def test_brain_to_qlib_trade_when_returns_none():
    """trade_when is execution-layer, not a feature → None."""
    assert brain_to_qlib("trade_when(close, volume, vwap)") is None


def test_brain_to_qlib_op_chain_one_unknown_returns_none():
    """ts_mean(group_neutralize(x, g), 5) → None (cascade)."""
    assert brain_to_qlib("ts_mean(group_neutralize(close, sector), 5)") is None


# ---------------------------------------------------------------------------
# §7.1 #11-#14 — composite + arithmetic
# ---------------------------------------------------------------------------

def test_brain_to_qlib_nested_call():
    """ts_mean(ts_rank(close, 10), 5) → Mean(Rank($close, 10), 5)."""
    assert brain_to_qlib("ts_mean(ts_rank(close, 10), 5)") == "Mean(Rank($close, 10), 5)"


def test_brain_to_qlib_signed_power():
    """signed_power(x, 2) → SignedPower(x, 2)."""
    assert brain_to_qlib("signed_power(close, 2)") == "SignedPower($close, 2)"


def test_brain_to_qlib_arithmetic():
    """multiply(divide(close, open), 100) → Mul(Div($close, $open), 100)."""
    assert brain_to_qlib("multiply(divide(close, open), 100)") == "Mul(Div($close, $open), 100)"


def test_brain_to_qlib_with_constants():
    """add(close, 1.5) → Add($close, 1.5)."""
    assert brain_to_qlib("add(close, 1.5)") == "Add($close, 1.5)"


# ---------------------------------------------------------------------------
# §7.1 #15-#17 — defensive guards
# ---------------------------------------------------------------------------

def test_brain_to_qlib_empty_expression():
    """Empty string → None."""
    assert brain_to_qlib("") is None


def test_brain_to_qlib_lru_cache_hit():
    """Repeated call with same (expr, region) hits lru_cache.

    Public contract: lru_cache(maxsize=1024) memoizes by (brain_expr, region).
    Use the wrapped function's cache_info() to confirm hit count.
    """
    brain_to_qlib.cache_clear()
    expr = "ts_mean(close, 20)"
    _ = brain_to_qlib(expr)
    _ = brain_to_qlib(expr)
    _ = brain_to_qlib(expr)
    info = brain_to_qlib.cache_info()
    assert info.hits >= 2
    assert info.misses == 1


def test_brain_to_qlib_region_unused_v1():
    """v1.0 single shared table — USA / CHN produce identical output."""
    brain_to_qlib.cache_clear()
    a = brain_to_qlib("ts_mean(close, 20)", region="USA")
    b = brain_to_qlib("ts_mean(close, 20)", region="CHN")
    assert a == b == "Mean($close, 20)"


# ---------------------------------------------------------------------------
# §7.1 #18-#20 — edge cases
# ---------------------------------------------------------------------------

def test_brain_to_qlib_unbalanced_paren_returns_none():
    """Mismatched parens → None (defensive; promised never to raise)."""
    assert brain_to_qlib("ts_mean(close, 20") is None


def test_brain_to_qlib_rank_cross_sectional():
    """rank(x) single-arg → Rank($x). qlib lacks native cross-sectional
    Rank but the syntax is the same — pandas-engine emulates via groupby.
    """
    assert brain_to_qlib("rank(close)") == "Rank($close)"


def test_brain_to_qlib_table_contains_expected_keys():
    """Sanity — the public reverse-mapping tables contain the headline ops."""
    for op in ["ts_mean", "ts_rank", "ts_delay", "add", "if_else"]:
        assert op in BRAIN_TO_QLIB_OPERATORS
    for op in ["group_neutralize", "trade_when", "winsorize"]:
        assert BRAIN_TO_QLIB_OPERATORS[op] is None
    for field in ["close", "open", "high", "low", "volume", "vwap"]:
        assert BRAIN_TO_QLIB_FIELD[field] == f"${field}"
    assert BRAIN_TO_QLIB_FIELD["fnd28"] is None  # cascade-trigger
