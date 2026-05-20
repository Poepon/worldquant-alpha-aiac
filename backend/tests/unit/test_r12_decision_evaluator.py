"""Sprint 5 PR1 — R12 decision evaluator unit tests (plan v5 §7).

Covers the pure-function decision logic on synthetic (status, metrics)
rows. The DB / query_mode_pool path is integration-tested elsewhere
(operator runs the script once at 7/4).
"""
from __future__ import annotations

import pytest

from scripts.r12_decision_evaluator import (
    compute_sentinel_counterfactuals,
    map_sprint5_route,
    _SENTINELS,
)


# ---------------------------------------------------------------------------
# Sentinel inventory
# ---------------------------------------------------------------------------

def test_six_sentinels_with_stamp_keys():
    assert len(_SENTINELS) == 6
    flags = {f for f, _k in _SENTINELS}
    assert flags == {
        "ENABLE_R1B_HYPOTHESIS_MUTATE",
        "ENABLE_G5_CROSSOVER",
        "ENABLE_HYPOTHESIS_FOREST_REUSE",
        "ENABLE_R8_L0",
        "ENABLE_AST_ORIGINALITY_GATE",
        "ENABLE_SIMULATION_CACHE",
    }


# ---------------------------------------------------------------------------
# compute_sentinel_counterfactuals
# ---------------------------------------------------------------------------

def _rows(*specs):
    """Build (status, metrics) rows from (status, {stamp_keys...}) specs."""
    out = []
    for status, keys in specs:
        m = {k: True for k in keys}
        out.append((status, m))
    return out


def test_counterfactual_restore_when_margin_positive():
    """A sentinel whose stamped alphas PASS more than baseline → RESTORE."""
    # 10 alphas: 5 with r1b stamp (4 PASS = 80%), 5 without (1 PASS = 20%).
    # baseline = 5/10 = 50%. r1b stamped rate 80% → margin +30pp → RESTORE.
    rows = (
        _rows(*[("PASS", ["_r1b_mutation_triggered"])] * 4)
        + _rows(("FAIL", ["_r1b_mutation_triggered"]))
        + _rows(("PASS", []))
        + _rows(*[("FAIL", [])] * 4)
    )
    out = compute_sentinel_counterfactuals(rows, min_stamped=5)
    r1b = next(s for s in out if s.flag == "ENABLE_R1B_HYPOTHESIS_MUTATE")
    assert r1b.stamped_n == 5
    assert r1b.stamped_pass == 4
    assert r1b.margin_pct_pts == pytest.approx(30.0, abs=0.1)
    assert r1b.recommendation == "RESTORE"


def test_counterfactual_deprecate_when_margin_negative():
    """Stamped alphas PASS LESS than baseline → DEPRECATE."""
    # 10 alphas: 5 with g5 stamp (1 PASS = 20%), 5 without (4 PASS = 80%).
    # baseline = 5/10 = 50%. g5 stamped 20% → margin -30pp → DEPRECATE.
    rows = (
        _rows(("PASS", ["_g5_crossover"]))
        + _rows(*[("FAIL", ["_g5_crossover"])] * 4)
        + _rows(*[("PASS", [])] * 4)
        + _rows(("FAIL", []))
    )
    out = compute_sentinel_counterfactuals(rows, min_stamped=5)
    g5 = next(s for s in out if s.flag == "ENABLE_G5_CROSSOVER")
    assert g5.margin_pct_pts == pytest.approx(-30.0, abs=0.1)
    assert g5.recommendation == "DEPRECATE"


def test_counterfactual_insufficient_when_few_stamped():
    """< min_stamped rows → INSUFFICIENT regardless of margin."""
    rows = (
        _rows(("PASS", ["_simulation_cache_hit"]))
        + _rows(*[("FAIL", [])] * 9)
    )
    out = compute_sentinel_counterfactuals(rows, min_stamped=5)
    cache = next(s for s in out if s.flag == "ENABLE_SIMULATION_CACHE")
    assert cache.stamped_n == 1
    assert cache.recommendation == "INSUFFICIENT"


def test_counterfactual_zero_margin_deprecates():
    """Margin exactly at floor (0.0) → DEPRECATE (not pulling weight)."""
    # 10 alphas: 5 with forest stamp (3 PASS=60%), 5 without (3 PASS=60%).
    # baseline = 6/10 = 60%. stamped 60% → margin 0pp → DEPRECATE.
    rows = (
        _rows(*[("PASS", ["_hypothesis_forest_reference"])] * 3)
        + _rows(*[("FAIL", ["_hypothesis_forest_reference"])] * 2)
        + _rows(*[("PASS", [])] * 3)
        + _rows(*[("FAIL", [])] * 2)
    )
    out = compute_sentinel_counterfactuals(rows, min_stamped=5)
    forest = next(s for s in out if s.flag == "ENABLE_HYPOTHESIS_FOREST_REUSE")
    assert forest.margin_pct_pts == pytest.approx(0.0, abs=0.1)
    assert forest.recommendation == "DEPRECATE"


def test_counterfactual_empty_rows_all_insufficient():
    out = compute_sentinel_counterfactuals([], min_stamped=5)
    assert len(out) == 6
    assert all(s.recommendation == "INSUFFICIENT" for s in out)
    assert all(s.stamped_n == 0 for s in out)


def test_counterfactual_pass_provisional_counts_as_pass():
    """PASS_PROVISIONAL should count toward the PASS rate."""
    rows = (
        _rows(*[("PASS_PROVISIONAL", ["_r8_l0_on"])] * 5)
        + _rows(*[("FAIL", [])] * 5)
    )
    out = compute_sentinel_counterfactuals(rows, min_stamped=5)
    r8 = next(s for s in out if s.flag == "ENABLE_R8_L0")
    assert r8.stamped_pass == 5
    assert r8.stamped_pass_rate == 1.0


def test_counterfactual_all_six_returned():
    rows = _rows(*[("PASS", [])] * 10)
    out = compute_sentinel_counterfactuals(rows)
    assert len(out) == 6
    assert {s.flag for s in out} == {f for f, _k in _SENTINELS}


# ---------------------------------------------------------------------------
# map_sprint5_route
# ---------------------------------------------------------------------------

def test_map_route_go():
    assert map_sprint5_route("GO") == "GO"


def test_map_route_nogo():
    assert map_sprint5_route("NO-GO") == "NO-GO"


def test_map_route_partial_and_insufficient():
    assert map_sprint5_route("PARTIAL") == "PARTIAL"
    assert map_sprint5_route("INSUFFICIENT") == "PARTIAL"
    assert map_sprint5_route("ERROR") == "PARTIAL"


# ---------------------------------------------------------------------------
# margin_floor tunable
# ---------------------------------------------------------------------------

def test_margin_floor_raises_restore_bar():
    """A +2pp margin with floor=5pp → DEPRECATE (didn't clear the bar)."""
    # 5 stamped, 3 PASS (60%); baseline includes them — build so margin ~+2pp.
    # 5 stamped 3 PASS = 60%; 5 unstamped 3 PASS = 58%-ish. Simpler: tune via floor.
    rows = (
        _rows(*[("PASS", ["_g3_ast_originality_blocked"])] * 6)
        + _rows(*[("FAIL", ["_g3_ast_originality_blocked"])] * 4)  # 60% stamped
        + _rows(*[("PASS", [])] * 5)
        + _rows(*[("FAIL", [])] * 5)  # 50% unstamped
    )
    # baseline = 11/20 = 55%, stamped = 60% → margin +5pp
    out_low = compute_sentinel_counterfactuals(rows, margin_floor_pct_pts=0.0, min_stamped=5)
    g3_low = next(s for s in out_low if s.flag == "ENABLE_AST_ORIGINALITY_GATE")
    assert g3_low.recommendation == "RESTORE"  # +5pp > 0 floor

    out_high = compute_sentinel_counterfactuals(rows, margin_floor_pct_pts=10.0, min_stamped=5)
    g3_high = next(s for s in out_high if s.flag == "ENABLE_AST_ORIGINALITY_GATE")
    assert g3_high.recommendation == "DEPRECATE"  # +5pp < 10pp floor
