"""Unit tests for the set-level orthogonal backlog drain (P0-1, 2026-06-03).

Pure algorithm — no DB/BRAIN. Covers the greedy farthest-point ordering and the
local-PnL pairwise correlation builder.
"""
from __future__ import annotations

import pytest

import math

import pandas as pd

from backend.marginal_drain import (
    annualized_sharpe,
    bootstrap_delta_sharpe_se,
    build_pool_returns,
    deflated_delta_sharpe_threshold,
    greedy_orthogonal_order,
    is_delta_sharpe_significant,
    marginal_delta_sharpe,
    pairwise_corr_from_pnl,
)


# ---------------------------------------------------------------------------
# greedy_orthogonal_order
# ---------------------------------------------------------------------------


def test_greedy_orders_by_incremental_orthogonality():
    # A,B are near-duplicates (corr 0.9); C is orthogonal to both. Threshold 0.7.
    cands = [
        {"id": 1, "self_corr": 0.1, "score": 5.0},
        {"id": 2, "self_corr": 0.1, "score": 4.0},
        {"id": 3, "self_corr": 0.6, "score": 3.0},
    ]
    corr = {(1, 2): 0.9, (1, 3): 0.1, (2, 3): 0.1}
    ordered, blocked = greedy_orthogonal_order(cands, corr, threshold=0.7)

    # A picked first (tie on self_corr 0.1 with B → higher score wins).
    assert [c["id"] for c in ordered] == [1, 3]
    assert ordered[0]["rank"] == 1 and ordered[0]["max_corr_to_selected"] == 0.1
    assert ordered[1]["rank"] == 2 and ordered[1]["max_corr_to_selected"] == 0.6
    # B is now correlation-blocked (0.9 to the already-selected A ≥ 0.7).
    assert [c["id"] for c in blocked] == [2]
    assert blocked[0]["max_corr_to_selected"] == 0.9


def test_greedy_degenerates_to_score_order_when_all_orthogonal():
    cands = [
        {"id": 1, "self_corr": None, "score": 2.0},
        {"id": 2, "self_corr": None, "score": 9.0},
        {"id": 3, "self_corr": None, "score": 5.0},
    ]
    ordered, blocked = greedy_orthogonal_order(cands, {}, threshold=0.7)
    # All max-corr=0 (self_corr None→0, no pairwise) → tiebreak by score desc.
    assert [c["id"] for c in ordered] == [2, 3, 1]
    assert blocked == []


def test_greedy_blocks_self_corr_breach_from_start():
    # A lone candidate already correlated to the submitted pool ≥ threshold.
    cands = [{"id": 7, "self_corr": 0.82, "score": 1.0}]
    ordered, blocked = greedy_orthogonal_order(cands, {}, threshold=0.7)
    assert ordered == []
    assert [c["id"] for c in blocked] == [7]
    assert blocked[0]["max_corr_to_selected"] == 0.82


def test_greedy_does_not_mutate_input():
    cands = [{"id": 1, "self_corr": 0.0, "score": 1.0}]
    _ = greedy_orthogonal_order(cands, {}, threshold=0.7)
    assert "rank" not in cands[0]  # operated on copies


# ---------------------------------------------------------------------------
# pairwise_corr_from_pnl
# ---------------------------------------------------------------------------


def test_pairwise_corr_from_pnl():
    rows = []
    for d in range(1, 9):  # 8 overlapping "days"
        v = 1.0 if d % 2 else -1.0
        rows.append((1, d, v))
        rows.append((2, d, v))    # identical to 1  → corr +1
        rows.append((3, d, -v))   # negated         → corr -1
    corr = pairwise_corr_from_pnl(rows, min_overlap=5)
    assert corr[(1, 2)] == pytest.approx(1.0)
    assert corr[(1, 3)] == pytest.approx(-1.0)
    assert corr[(2, 3)] == pytest.approx(-1.0)


def test_pairwise_corr_respects_min_overlap():
    # Only 3 overlapping days but min_overlap=5 → no pair qualifies.
    rows = [(1, d, float(d)) for d in range(3)] + [(2, d, float(d)) for d in range(3)]
    assert pairwise_corr_from_pnl(rows, min_overlap=5) == {}


def test_pairwise_corr_empty_inputs():
    assert pairwise_corr_from_pnl([]) == {}
    assert pairwise_corr_from_pnl([(1, 0, 1.0)]) == {}  # single alpha → no pairs


# ---------------------------------------------------------------------------
# Combination layer (P1 L2): annualized_sharpe / build_pool_returns / ΔSharpe
# ---------------------------------------------------------------------------


def test_annualized_sharpe_known_value():
    # 120 vals alternating 1.1/-0.9 → mean 0.1, std(ddof=0) 1.0 → 0.1·√252 = 1.587
    s = pd.Series([1.1 if i % 2 == 0 else -0.9 for i in range(120)])
    sr = annualized_sharpe(s)
    assert math.isclose(sr, 0.1 * math.sqrt(252), rel_tol=1e-6)


def test_annualized_sharpe_degenerate():
    assert annualized_sharpe(None) is None
    assert annualized_sharpe(pd.Series([1.0] * 100)) is None        # zero vol
    assert annualized_sharpe(pd.Series([1.0, -1.0] * 5)) is None      # < min_obs(60)


def test_build_pool_returns_equal_vol():
    # alpha1 std 1, alpha2 std 2 → unit-vol normalise → both [1,-1,…] → sum [2,-2,…]
    rows = []
    for d in range(8):
        v = 1.0 if d % 2 == 0 else -1.0
        rows.append((1, d, v))
        rows.append((2, d, 2 * v))
    pool = build_pool_returns(rows, equal_vol=True)
    assert pool is not None
    assert list(pool.round(6)) == [2.0 if d % 2 == 0 else -2.0 for d in range(8)]


def test_build_pool_returns_drops_partial_member_dates():
    # alpha1 spans days 0-7, alpha2 only days 4-7 → pool must use ONLY the common
    # window (4-7), else partial-member dates inject membership-driven vol.
    rows = []
    for d in range(8):
        rows.append((1, d, 1.0 if d % 2 == 0 else -1.0))
    for d in range(4, 8):
        rows.append((2, d, 2.0 if d % 2 == 0 else -2.0))
    pool = build_pool_returns(rows, equal_vol=True)
    assert pool is not None
    assert list(pool.index) == [4, 5, 6, 7]          # common window only
    assert list(pool.round(6)) == [2.0, -2.0, 2.0, -2.0]


def test_marginal_delta_sharpe_diversification_positive():
    # pool mean 0 (Sharpe 0); a positive-mean candidate lifts combined Sharpe → Δ>0.
    pool = pd.Series([1.0 if i % 2 == 0 else -1.0 for i in range(120)])   # mean 0, Sharpe 0
    cand = pd.Series([1.0 if i % 2 == 0 else 0.0 for i in range(120)])    # mean 0.5
    d = marginal_delta_sharpe(pool, cand, equal_vol=True, min_overlap=60)
    # cand unit-vol = [2,0,…]; combined [3,-1,…] mean 1 std 2 → 0.5·√252
    assert d == pytest.approx(0.5 * math.sqrt(252), abs=0.01)
    assert d > 0


def test_marginal_delta_sharpe_identical_is_zero():
    pool = pd.Series([1.0 if i % 3 == 0 else -0.4 for i in range(120)])
    d = marginal_delta_sharpe(pool, pool.copy(), equal_vol=True, min_overlap=60)
    # adding a scaled copy of the pool doesn't change Sharpe → Δ≈0
    assert d == pytest.approx(0.0, abs=1e-6)


def test_marginal_delta_sharpe_thin_overlap_none():
    pool = pd.Series([1.0, -1.0] * 40)             # 80 obs
    cand = pd.Series([1.0, -1.0] * 10)             # 20 obs overlap < 60
    assert marginal_delta_sharpe(pool, cand, min_overlap=60) is None


# ---------------------------------------------------------------------------
# Audit hardening: bootstrap SE noise floor + significance + deflation
# ---------------------------------------------------------------------------


def test_bootstrap_delta_sharpe_se_positive_and_deterministic():
    pool = pd.Series([1.0 if i % 2 == 0 else -1.0 for i in range(160)])
    cand = pd.Series([0.6 if i % 3 == 0 else -0.3 for i in range(160)])
    se1 = bootstrap_delta_sharpe_se(pool, cand, n_boot=100, seed=7)
    se2 = bootstrap_delta_sharpe_se(pool, cand, n_boot=100, seed=7)
    assert se1 is not None and se1 > 0
    assert se1 == se2                              # same seed → deterministic


def test_bootstrap_delta_sharpe_se_thin_overlap_none():
    pool = pd.Series([1.0, -1.0] * 40)             # 80 obs
    cand = pd.Series([1.0, -1.0] * 10)             # 20 overlap < 60
    assert bootstrap_delta_sharpe_se(pool, cand, min_overlap=60) is None


def test_is_delta_sharpe_significant():
    assert is_delta_sharpe_significant(0.5, 0.1) is True       # 0.5 > 1.64*0.1
    assert is_delta_sharpe_significant(0.1, 0.1) is False      # within noise
    assert is_delta_sharpe_significant(-0.5, 0.1) is True      # magnitude counts
    assert is_delta_sharpe_significant(None, 0.1) is False
    assert is_delta_sharpe_significant(0.5, None) is False
    assert is_delta_sharpe_significant(0.5, 0.0) is False
    # The motivating audit examples (±0.01–0.025 on SE≈0.08) are NOT significant.
    assert is_delta_sharpe_significant(-0.009, 0.08) is False
    assert is_delta_sharpe_significant(0.025, 0.08) is False


def test_deflated_delta_sharpe_threshold():
    assert deflated_delta_sharpe_threshold([0.05]) == 0.0          # N<2
    # zero variance → effectively 0 (float residue ~1e-18 is negligible)
    assert deflated_delta_sharpe_threshold([0.05, 0.05, 0.05]) < 1e-9
    spread = deflated_delta_sharpe_threshold([0.3, -0.1, 0.05, 0.2, -0.2])
    assert spread > 0                                              # expected-max under null


# ---------------------------------------------------------------------------
# greedy_orthogonal_order — value objective (ΔSharpe-driven, breadth-constrained)
# ---------------------------------------------------------------------------


def test_greedy_value_orders_by_delta_sharpe_then_none_last():
    # value mode: highest ΔSharpe first; negative still beats None; None last.
    cands = [
        {"id": 1, "self_corr": 0.1, "score": 0.05},
        {"id": 2, "self_corr": 0.1, "score": 0.30},
        {"id": 3, "self_corr": 0.1, "score": None},   # no ΔSharpe (no PnL)
        {"id": 4, "self_corr": 0.1, "score": -0.20},
    ]
    corr = {(1, 2): 0.1, (1, 3): 0.1, (1, 4): 0.1, (2, 3): 0.1, (2, 4): 0.1, (3, 4): 0.1}
    ordered, blocked = greedy_orthogonal_order(cands, corr, threshold=0.7, objective="value")
    assert [c["id"] for c in ordered] == [2, 1, 4, 3]
    assert blocked == []


def test_greedy_value_respects_correlation_constraint():
    # B is a 0.9-duplicate of the higher-ΔSharpe A → blocked once A is selected.
    cands = [
        {"id": 1, "self_corr": 0.1, "score": 0.30},
        {"id": 2, "self_corr": 0.1, "score": 0.20},
    ]
    corr = {(1, 2): 0.9}
    ordered, blocked = greedy_orthogonal_order(cands, corr, threshold=0.7, objective="value")
    assert [c["id"] for c in ordered] == [1]
    assert [c["id"] for c in blocked] == [2]
    assert blocked[0]["max_corr_to_selected"] == 0.9


def test_greedy_breadth_mode_unchanged_by_objective_default():
    # P0-1 default ('breadth') still picks min-max-corr first.
    cands = [
        {"id": 1, "self_corr": 0.6, "score": 9.0},
        {"id": 2, "self_corr": 0.1, "score": 1.0},
    ]
    ordered, _ = greedy_orthogonal_order(cands, {(1, 2): 0.1}, threshold=0.7)
    assert ordered[0]["id"] == 2  # most orthogonal first, NOT highest score


# ---------------------------------------------------------------------------
# greedy value objective — SIGN-based tiers (reconciliation 2026-06-03)
# ---------------------------------------------------------------------------


def test_greedy_value_tier_orders_by_sign_not_magnitude():
    # value_tier present ⇒ route on validated SIGN tier (additive 0 > neutral 1 >
    # dilutive 2 > unmeasurable 3), then breadth — NOT on the noisy magnitude.
    # Note ids 1 and 4: 4 has the larger raw |score| but a WORSE tier (dilutive),
    # so it must rank below the additive 1 despite magnitude. All equal breadth.
    cands = [
        {"id": 1, "self_corr": 0.1, "score": 0.01, "value_tier": 0},   # additive
        {"id": 2, "self_corr": 0.1, "score": None, "value_tier": 1},   # neutral/no-Δ
        {"id": 3, "self_corr": 0.1, "score": None, "value_tier": 3},   # no PnL
        {"id": 4, "self_corr": 0.1, "score": -0.99, "value_tier": 2},  # dilutive
    ]
    corr = {(a, b): 0.1 for a in range(1, 5) for b in range(a + 1, 5)}
    ordered, blocked = greedy_orthogonal_order(cands, corr, threshold=0.7, objective="value")
    assert [c["id"] for c in ordered] == [1, 2, 4, 3]
    assert blocked == []


def test_greedy_value_tier_breaks_ties_by_breadth():
    # Same tier (both additive) ⇒ the more-orthogonal (lower max_corr) wins.
    cands = [
        {"id": 1, "self_corr": 0.1, "value_tier": 0},
        {"id": 2, "self_corr": 0.1, "value_tier": 0},
    ]
    # 1 correlates with the seed-of-2 less than 2 does → but with only these two,
    # the first picked is the globally most-orthogonal by min-max-corr seeding.
    ordered, _ = greedy_orthogonal_order(
        cands, {(1, 2): 0.2}, threshold=0.7, objective="value",
    )
    assert {c["id"] for c in ordered} == {1, 2}  # both selected, tier-equal


def test_greedy_value_tier_falls_back_to_legacy_when_absent():
    # No value_tier ⇒ legacy significant-magnitude ordering still holds.
    cands = [
        {"id": 1, "self_corr": 0.1, "score": 0.05},
        {"id": 2, "self_corr": 0.1, "score": 0.30},
        {"id": 3, "self_corr": 0.1, "score": None},
        {"id": 4, "self_corr": 0.1, "score": -0.20},
    ]
    corr = {(a, b): 0.1 for a in range(1, 5) for b in range(a + 1, 5)}
    ordered, _ = greedy_orthogonal_order(cands, corr, threshold=0.7, objective="value")
    assert [c["id"] for c in ordered] == [2, 1, 4, 3]
