"""B3 R10-v2 apply_family_hard_ban + stamp finalize unit tests.

Coverage:
  - apply_family_hard_ban: pairwise corr ≥ τ within same family → ban
    lower-scoring sibling
  - apply_family_hard_ban: cross-family pairs ignored (different
    family_signature → not compared)
  - apply_family_hard_ban: no corr matrix supplied → []  (soft-skip)
  - apply_family_hard_ban: terminal-FAIL alphas excluded from pair set
  - apply_family_hard_ban: threshold out of [0,1] returns []
  - apply_family_hard_ban: missing alpha_id from corr matrix index →
    pair silently skipped (KeyError tolerant)
  - apply_family_hard_ban: idempotent — calling twice returns same set
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import pandas as pd
import pytest

from backend.family_classifier import apply_family_hard_ban


@dataclass
class _MockAlpha:
    """Minimal alpha shape that apply_family_hard_ban needs."""
    alpha_id: str
    expression: str
    metrics: Dict[str, Any] = field(default_factory=dict)
    quality_status: Optional[str] = "PENDING"


def _corr_matrix(pairs: Dict[tuple, float]) -> pd.DataFrame:
    """Build a symmetric corr DataFrame from {(a, b): corr} dict.

    Indexed by string alpha_id on both axes; diag = 1.0 implicit.
    """
    ids = sorted(set(k for pair in pairs for k in pair))
    df = pd.DataFrame(1.0, index=ids, columns=ids)
    for (a, b), c in pairs.items():
        df.at[a, b] = c
        df.at[b, a] = c
    return df


# ---------------------------------------------------------------------------
# Basic pairs
# ---------------------------------------------------------------------------

def test_hard_ban_same_family_high_corr_bans_lower_scorer():
    """Two alphas, same family_signature, corr=0.80 ≥ τ=0.65 → ban the
    lower-scoring one."""
    a = _MockAlpha("a1", "ts_rank(close, 60)", metrics={"sharpe": 1.8})
    b = _MockAlpha("a2", "ts_rank(volume, 60)", metrics={"sharpe": 1.5})
    # same op skeleton ts_rank, same family
    corr = _corr_matrix({("a1", "a2"): 0.80})
    bans = apply_family_hard_ban([a, b], pnl_corr_matrix=corr, threshold=0.65)
    assert bans == [1]  # b is lower-scoring → banned


def test_hard_ban_below_threshold_no_action():
    a = _MockAlpha("a1", "ts_rank(close, 60)", metrics={"sharpe": 1.8})
    b = _MockAlpha("a2", "ts_rank(volume, 60)", metrics={"sharpe": 1.5})
    corr = _corr_matrix({("a1", "a2"): 0.40})
    assert apply_family_hard_ban([a, b], pnl_corr_matrix=corr, threshold=0.65) == []


def test_hard_ban_at_exact_threshold_bans():
    """Boundary check: corr == τ should trigger (>= comparison)."""
    a = _MockAlpha("a1", "ts_rank(close, 60)", metrics={"sharpe": 1.8})
    b = _MockAlpha("a2", "ts_rank(volume, 60)", metrics={"sharpe": 1.5})
    corr = _corr_matrix({("a1", "a2"): 0.65})
    assert apply_family_hard_ban([a, b], pnl_corr_matrix=corr, threshold=0.65) == [1]


# ---------------------------------------------------------------------------
# Family isolation
# ---------------------------------------------------------------------------

def test_hard_ban_different_families_not_compared():
    """Different family_signature → no ban even if PnL corr is 0.99."""
    a = _MockAlpha("a1", "ts_rank(close, 60)", metrics={"sharpe": 1.8})
    # different operator skeleton (rank instead of ts_rank) → different family
    b = _MockAlpha("a2", "rank(close)", metrics={"sharpe": 1.5})
    corr = _corr_matrix({("a1", "a2"): 0.99})
    assert apply_family_hard_ban([a, b], pnl_corr_matrix=corr, threshold=0.65) == []


def test_hard_ban_three_alpha_chain_same_family():
    """3 alphas same family, pairwise corr (1↔2)=0.8 (2↔3)=0.7 (1↔3)=0.6 with τ=0.65.

    Sort by sharpe DESC. Process a1 (highest) → survivor. a2 vs a1=0.8 ≥ τ →
    ban. a3 vs a1=0.6 < τ → keep.
    """
    a = _MockAlpha("a1", "ts_rank(close, 60)", metrics={"sharpe": 2.0})
    b = _MockAlpha("a2", "ts_rank(volume, 60)", metrics={"sharpe": 1.7})
    c = _MockAlpha("a3", "ts_rank(returns, 60)", metrics={"sharpe": 1.5})
    corr = _corr_matrix({
        ("a1", "a2"): 0.80,
        ("a1", "a3"): 0.60,
        ("a2", "a3"): 0.70,
    })
    bans = apply_family_hard_ban([a, b, c], pnl_corr_matrix=corr, threshold=0.65)
    assert bans == [1]  # only a2 banned (a3 vs a1 below threshold)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_hard_ban_no_corr_matrix_returns_empty():
    a = _MockAlpha("a1", "ts_rank(close, 60)", metrics={"sharpe": 1.8})
    b = _MockAlpha("a2", "ts_rank(volume, 60)", metrics={"sharpe": 1.5})
    assert apply_family_hard_ban([a, b], pnl_corr_matrix=None, threshold=0.65) == []


def test_hard_ban_empty_alphas_returns_empty():
    assert apply_family_hard_ban([], pnl_corr_matrix=pd.DataFrame(), threshold=0.65) == []


def test_hard_ban_threshold_out_of_range_returns_empty():
    a = _MockAlpha("a1", "ts_rank(close, 60)", metrics={"sharpe": 1.8})
    b = _MockAlpha("a2", "ts_rank(volume, 60)", metrics={"sharpe": 1.5})
    corr = _corr_matrix({("a1", "a2"): 0.99})
    # Out of [0,1]
    assert apply_family_hard_ban([a, b], pnl_corr_matrix=corr, threshold=1.5) == []
    assert apply_family_hard_ban([a, b], pnl_corr_matrix=corr, threshold=-0.1) == []


def test_hard_ban_terminal_fail_alphas_excluded():
    """Alphas already in FAIL status do not occupy a sibling slot —
    a3 (FAIL) should be ignored even with high corr to a1."""
    a = _MockAlpha("a1", "ts_rank(close, 60)", metrics={"sharpe": 1.8})
    b = _MockAlpha("a2", "ts_rank(volume, 60)", metrics={"sharpe": 1.5})
    c = _MockAlpha("a3", "ts_rank(returns, 60)", metrics={"sharpe": 1.2}, quality_status="FAIL")
    corr = _corr_matrix({
        ("a1", "a2"): 0.80,  # ban a2
        ("a1", "a3"): 0.99,  # would have banned a3 but a3 is FAIL → excluded
    })
    bans = apply_family_hard_ban([a, b, c], pnl_corr_matrix=corr, threshold=0.65)
    assert bans == [1]


def test_hard_ban_alpha_missing_from_corr_matrix_silently_skipped():
    """Corr matrix only has a1; lookup a1↔a2 KeyError → pair skipped (no crash)."""
    a = _MockAlpha("a1", "ts_rank(close, 60)", metrics={"sharpe": 1.8})
    b = _MockAlpha("a2", "ts_rank(volume, 60)", metrics={"sharpe": 1.5})
    # Only a1 in corr matrix
    corr = pd.DataFrame(1.0, index=["a1"], columns=["a1"])
    bans = apply_family_hard_ban([a, b], pnl_corr_matrix=corr, threshold=0.65)
    # a2 has no entry → KeyError swallowed → no ban (cannot prove correlation)
    assert bans == []


def test_hard_ban_idempotent():
    """Calling twice on the same input returns the same ban set."""
    a = _MockAlpha("a1", "ts_rank(close, 60)", metrics={"sharpe": 1.8})
    b = _MockAlpha("a2", "ts_rank(volume, 60)", metrics={"sharpe": 1.5})
    corr = _corr_matrix({("a1", "a2"): 0.80})
    first = apply_family_hard_ban([a, b], pnl_corr_matrix=corr, threshold=0.65)
    second = apply_family_hard_ban([a, b], pnl_corr_matrix=corr, threshold=0.65)
    assert first == second == [1]


def test_hard_ban_alpha_without_id_skipped():
    """Alpha with no alpha_id AND no id is silently skipped."""
    a = _MockAlpha("a1", "ts_rank(close, 60)", metrics={"sharpe": 1.8})
    # alpha_id = None, no .id attribute either
    b = _MockAlpha(alpha_id="", expression="ts_rank(volume, 60)", metrics={"sharpe": 1.5})
    b.alpha_id = None
    corr = _corr_matrix({("a1", "fake"): 0.99})
    # a2 has no id → not added to groups → no ban
    assert apply_family_hard_ban([a, b], pnl_corr_matrix=corr, threshold=0.65) == []


def test_hard_ban_empty_expression_skipped():
    """Alpha with empty expression has family_signature=<empty> → skipped."""
    a = _MockAlpha("a1", "", metrics={"sharpe": 1.8})
    b = _MockAlpha("a2", "", metrics={"sharpe": 1.5})
    corr = _corr_matrix({("a1", "a2"): 0.99})
    assert apply_family_hard_ban([a, b], pnl_corr_matrix=corr, threshold=0.65) == []
