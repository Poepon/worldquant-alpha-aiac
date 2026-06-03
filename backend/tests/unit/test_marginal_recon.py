"""Unit tests for backend.marginal_recon — the methodology-audit kill-switch.

sign_agreement_stats reconciles the OFFLINE marginal ΔSharpe against BRAIN's
AUTHORITATIVE before-and-after Δsharpe. The verdict gates whether the offline
proxy is trustworthy enough to keep routing on (sign-agreement ≤ 60% ⇒ FALSIFY).
"""
from __future__ import annotations

import pytest

from backend.marginal_recon import (
    sign_agreement_stats,
    _spearman,
    KILL_SIGN_AGREEMENT,
    MIN_PAIRS_FOR_VERDICT,
)


# ---------------------------------------------------------------------------
# sign_agreement_stats — verdict bands
# ---------------------------------------------------------------------------


def _pairs(rate: float, n: int):
    """n pairs where `rate` fraction agree in sign (both +) and the rest disagree
    (predicted +, authoritative -). All |values| well above eps."""
    n_agree = round(rate * n)
    out = []
    for i in range(n):
        if i < n_agree:
            out.append((0.5, 0.5))     # same sign
        else:
            out.append((0.5, -0.5))    # opposite sign
    return out


def test_full_agreement_supported():
    stat = sign_agreement_stats(_pairs(1.0, 30))
    assert stat["n_pairs"] == 30
    assert stat["n_sign_compared"] == 30
    assert stat["sign_agreement_rate"] == 1.0
    assert stat["verdict"] == "supported"
    assert stat["kill_threshold"] == KILL_SIGN_AGREEMENT


def test_coin_flip_falsified():
    stat = sign_agreement_stats(_pairs(0.5, 30))
    assert stat["sign_agreement_rate"] == 0.5
    assert stat["verdict"] == "FALSIFIED"   # ≤ 0.60 kill threshold


def test_exactly_at_kill_threshold_is_falsified():
    # rate == 0.60 must FALSIFY (≤, not <).
    stat = sign_agreement_stats(_pairs(0.6, 30))
    assert abs(stat["sign_agreement_rate"] - 0.6) < 1e-9
    assert stat["verdict"] == "FALSIFIED"


def test_weak_band():
    # 0.60 < rate < 0.70 ⇒ weak.
    stat = sign_agreement_stats(_pairs(2 / 3, 30))
    assert 0.60 < stat["sign_agreement_rate"] < 0.70
    assert stat["verdict"] == "weak"


def test_supported_band_at_070():
    stat = sign_agreement_stats(_pairs(0.7, 30))
    assert stat["sign_agreement_rate"] == 0.7
    assert stat["verdict"] == "supported"


def test_insufficient_sample_below_min_pairs():
    stat = sign_agreement_stats(_pairs(1.0, MIN_PAIRS_FOR_VERDICT - 1))
    assert stat["verdict"] == "insufficient_sample"
    # rate is still computed, just not enough to rule.
    assert stat["sign_agreement_rate"] == 1.0


def test_empty_input():
    stat = sign_agreement_stats([])
    assert stat["n_pairs"] == 0
    assert stat["sign_agreement_rate"] is None
    assert stat["verdict"] == "insufficient_sample"


# ---------------------------------------------------------------------------
# near-zero handling + None / NaN dropping
# ---------------------------------------------------------------------------


def test_near_zero_dropped_from_sign_test():
    # A near-zero pair has meaningless sign → excluded from n_sign_compared,
    # but the well-signed pairs still drive the rate.
    pairs = _pairs(1.0, 20) + [(1e-12, 1e-12), (-1e-12, 0.0)]
    stat = sign_agreement_stats(pairs)
    assert stat["n_pairs"] == 22          # all kept for rank corr
    assert stat["n_sign_compared"] == 20  # near-zero excluded from sign test
    assert stat["sign_agreement_rate"] == 1.0


def test_none_and_nan_pairs_dropped():
    pairs = _pairs(1.0, 16) + [(None, 0.5), (0.5, None), (float("nan"), 0.5)]
    stat = sign_agreement_stats(pairs)
    assert stat["n_pairs"] == 16   # the 3 bad pairs removed entirely
    assert stat["verdict"] == "supported"


# ---------------------------------------------------------------------------
# spearman
# ---------------------------------------------------------------------------


def test_spearman_perfect_monotonic():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    ys = [10.0, 20.0, 30.0, 40.0, 50.0]
    r = _spearman(xs, ys)
    assert r is not None and abs(r - 1.0) < 1e-9


def test_spearman_perfect_inverse():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    ys = [50.0, 40.0, 30.0, 20.0, 10.0]
    r = _spearman(xs, ys)
    assert r is not None and abs(r + 1.0) < 1e-9


def test_spearman_constant_side_none():
    assert _spearman([1.0, 2.0, 3.0], [5.0, 5.0, 5.0]) is None


def test_spearman_too_few_points_none():
    assert _spearman([1.0, 2.0], [1.0, 2.0]) is None


def test_spearman_surfaced_in_stats():
    # Monotone agreement ⇒ spearman ~1 surfaced alongside the verdict.
    pairs = [(float(i), float(i) * 2.0) for i in range(1, 21)]
    stat = sign_agreement_stats(pairs)
    assert stat["spearman"] is not None and stat["spearman"] > 0.99
    assert stat["verdict"] == "supported"
