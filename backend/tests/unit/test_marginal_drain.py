"""Unit tests for the set-level orthogonal backlog drain (P0-1, 2026-06-03).

Pure algorithm — no DB/BRAIN. Covers the greedy farthest-point ordering and the
local-PnL pairwise correlation builder.
"""
from __future__ import annotations

import pytest

from backend.marginal_drain import (
    greedy_orthogonal_order,
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
