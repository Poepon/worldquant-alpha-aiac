"""Integration: Phase 2 R10 Family-cap (Hubble v2 Table 1).

Tests per master plan §4.4 R10:
  1. family_signature canonicalization (same ops → same sig)
  2. family_signature edge cases (empty / no ops / case-insensitive)
  3. apply_family_cap basic: 5 alphas same family → keep top-K=2, drop 3
  4. apply_family_cap multi-pillar isolation: different pillars don't collide
  5. apply_family_cap with composite_score > sharpe priority
  6. apply_family_cap edge cases (empty / top_k=0 / top_k > group)
  7. flag OFF byte-equivalence
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.config import _flag_override_cache
from backend.family_classifier import (
    apply_family_cap,
    family_signature,
)


@pytest.fixture(autouse=True)
def _clear_flag_cache():
    _flag_override_cache.clear()
    yield
    _flag_override_cache.clear()


def _mk_alpha(expr: str, sharpe: float = 0.0, pillar: str = None, composite: float = None):
    """Build a SimpleNamespace mimicking AlphaCandidate for testing."""
    metrics = {"sharpe": sharpe}
    if composite is not None:
        metrics["composite_score"] = composite
    if pillar is not None:
        metrics["pillar"] = pillar
    return SimpleNamespace(
        expression=expr,
        metrics=metrics,
        quality_status="PENDING",
    )


# ---------------------------------------------------------------------------
# Test 1-2: family_signature canonicalization + edge cases
# ---------------------------------------------------------------------------

def test_family_signature_same_ops_same_sig():
    """Same operator pipeline → same signature regardless of field/window."""
    sig_a = family_signature("rank(ts_mean(close, 20))")
    sig_b = family_signature("rank(ts_mean(volume, 60))")
    sig_c = family_signature("rank(ts_mean(returns, 5))")
    assert sig_a == sig_b == sig_c


def test_family_signature_different_ops_different_sig():
    """Different operator pipelines → different sigs."""
    sig_rank = family_signature("rank(close)")
    sig_zscore = family_signature("zscore(close)")
    assert sig_rank != sig_zscore


def test_family_signature_order_matters():
    """Operator order matters — rank(ts_mean(x)) ≠ ts_mean(rank(x))."""
    sig_outer_rank = family_signature("rank(ts_mean(close, 20))")
    sig_outer_mean = family_signature("ts_mean(rank(close), 20)")
    assert sig_outer_rank != sig_outer_mean


def test_family_signature_empty_or_no_ops():
    """Empty / op-less expressions → '<empty>' signature."""
    assert family_signature("") == "<empty>"
    assert family_signature(None) == "<empty>"  # type: ignore
    assert family_signature("close") == "<empty>"  # bare field, no op


def test_family_signature_case_insensitive():
    """OPs are extracted lowercase."""
    sig_lower = family_signature("rank(close)")
    sig_upper = family_signature("RANK(close)")
    assert sig_lower == sig_upper


# ---------------------------------------------------------------------------
# Test 3: basic cap — 5 same-family alphas → keep top-2 drop 3
# ---------------------------------------------------------------------------

def test_apply_family_cap_drops_overflow_in_same_family():
    """5 rank(ts_mean) alphas same pillar — keep top-2 by sharpe, drop 3."""
    alphas = [
        _mk_alpha("rank(ts_mean(close, 5))",  sharpe=0.5, pillar="momentum"),
        _mk_alpha("rank(ts_mean(close, 20))", sharpe=1.5, pillar="momentum"),  # top
        _mk_alpha("rank(ts_mean(close, 60))", sharpe=2.0, pillar="momentum"),  # top
        _mk_alpha("rank(ts_mean(vwap, 10))",  sharpe=0.8, pillar="momentum"),
        _mk_alpha("rank(ts_mean(volume, 30))", sharpe=0.3, pillar="momentum"),
    ]
    drop = apply_family_cap(alphas, top_k=2)
    assert len(drop) == 3
    # Sharpes 0.5 / 0.8 / 0.3 dropped; 1.5 / 2.0 kept
    dropped_sharpes = sorted(alphas[i].metrics["sharpe"] for i in drop)
    assert dropped_sharpes == [0.3, 0.5, 0.8]


# ---------------------------------------------------------------------------
# Test 4: multi-pillar isolation — same family but different pillars survive
# ---------------------------------------------------------------------------

def test_apply_family_cap_multi_pillar_isolation():
    """rank() alphas across 2 pillars — each pillar caps independently."""
    alphas = [
        _mk_alpha("rank(close)", sharpe=0.5, pillar="momentum"),
        _mk_alpha("rank(close)", sharpe=1.0, pillar="momentum"),
        _mk_alpha("rank(close)", sharpe=1.5, pillar="momentum"),  # 3rd in momentum → DROP
        _mk_alpha("rank(close)", sharpe=2.0, pillar="value"),     # separate pillar → KEEP
        _mk_alpha("rank(close)", sharpe=2.5, pillar="value"),
    ]
    drop = apply_family_cap(alphas, top_k=2)
    # momentum group has 3 → drop 1; value group has 2 → drop 0
    assert len(drop) == 1
    # Dropped is the lowest-sharpe momentum
    assert alphas[drop[0]].metrics["sharpe"] == 0.5
    assert alphas[drop[0]].metrics["pillar"] == "momentum"


# ---------------------------------------------------------------------------
# Test 5: composite_score > sharpe priority
# ---------------------------------------------------------------------------

def test_apply_family_cap_uses_composite_when_available():
    """composite_score (R5 + R1a combined) preferred over sharpe alone."""
    alphas = [
        # High sharpe but low composite → DROP
        _mk_alpha("rank(close)", sharpe=2.0, pillar="momentum", composite=0.2),
        # Mid sharpe high composite → KEEP
        _mk_alpha("rank(close)", sharpe=1.0, pillar="momentum", composite=0.9),
        # Low sharpe mid composite → KEEP
        _mk_alpha("rank(close)", sharpe=0.5, pillar="momentum", composite=0.6),
    ]
    drop = apply_family_cap(alphas, top_k=2)
    assert len(drop) == 1
    # The one with composite=0.2 dropped (even though it had highest sharpe)
    assert alphas[drop[0]].metrics["composite_score"] == 0.2


# ---------------------------------------------------------------------------
# Test 6: edge cases
# ---------------------------------------------------------------------------

def test_apply_family_cap_empty_input():
    assert apply_family_cap([], top_k=2) == []


def test_apply_family_cap_group_below_top_k():
    """When group size <= top_k, nothing dropped."""
    alphas = [
        _mk_alpha("rank(close)", sharpe=0.5, pillar="momentum"),
        _mk_alpha("rank(close)", sharpe=1.0, pillar="momentum"),
    ]
    assert apply_family_cap(alphas, top_k=2) == []
    assert apply_family_cap(alphas, top_k=5) == []  # top_k > group


def test_apply_family_cap_invalid_top_k_treats_as_one():
    """top_k=0 / negative → treated as 1 with warning."""
    alphas = [
        _mk_alpha("rank(close)", sharpe=0.5, pillar="momentum"),
        _mk_alpha("rank(close)", sharpe=1.0, pillar="momentum"),
    ]
    drop = apply_family_cap(alphas, top_k=0)
    assert len(drop) == 1  # treated as top_k=1, drops 1


def test_apply_family_cap_top_k_1_only_best_survives():
    """top_k=1 — only the highest-scoring per family survives."""
    alphas = [
        _mk_alpha("rank(close)", sharpe=0.5, pillar="momentum"),
        _mk_alpha("rank(close)", sharpe=2.0, pillar="momentum"),  # KEEP
        _mk_alpha("rank(close)", sharpe=1.0, pillar="momentum"),
    ]
    drop = apply_family_cap(alphas, top_k=1)
    assert len(drop) == 2
    kept_idx = (set(range(3)) - set(drop)).pop()
    assert alphas[kept_idx].metrics["sharpe"] == 2.0


# ---------------------------------------------------------------------------
# Test 7: drop indices are sorted ascending
# ---------------------------------------------------------------------------

def test_apply_family_cap_returns_sorted_indices():
    alphas = [
        _mk_alpha(f"rank(close)", sharpe=float(i), pillar="momentum")
        for i in range(10, 0, -1)  # sharpes 10..1
    ]
    drop = apply_family_cap(alphas, top_k=2)
    assert drop == sorted(drop)
    # Should drop indices 2..9 (alphas with sharpes 8..1)
    assert drop == list(range(2, 10))


# ---------------------------------------------------------------------------
# Test 8 (M4 review fix): FAIL/REJECT alphas excluded from top-K race
# ---------------------------------------------------------------------------

def test_apply_family_cap_skips_already_failed_alphas():
    """M4: alphas with quality_status='FAIL' must NOT occupy a top-K slot
    and must NOT appear in the drop index list (they were already FAIL,
    not dropped by the cap)."""
    a_fail = _mk_alpha("rank(close)", sharpe=5.0, pillar="momentum")
    a_fail.quality_status = "FAIL"
    a_reject = _mk_alpha("rank(close)", sharpe=4.0, pillar="momentum")
    a_reject.quality_status = "REJECT"
    a_keep1 = _mk_alpha("rank(close)", sharpe=2.0, pillar="momentum")  # KEEP
    a_keep2 = _mk_alpha("rank(close)", sharpe=1.5, pillar="momentum")  # KEEP
    a_drop  = _mk_alpha("rank(close)", sharpe=1.0, pillar="momentum")  # DROP (3rd PENDING)
    alphas = [a_fail, a_reject, a_keep1, a_keep2, a_drop]
    drop = apply_family_cap(alphas, top_k=2)
    # Only the 3rd PENDING alpha (idx=4, sharpe=1.0) is cap-dropped.
    # The FAIL (idx=0, sharpe=5.0) and REJECT (idx=1, sharpe=4.0) are
    # excluded from the race despite having the highest sharpes.
    assert drop == [4]
    # Pre-existing FAIL/REJECT status must NOT be re-touched
    assert alphas[0].quality_status == "FAIL"
    assert alphas[1].quality_status == "REJECT"


def test_apply_family_cap_fail_alpha_does_not_crowd_top_k():
    """If a FAIL alpha sits at score=0 inside a 3-member family with top_k=2,
    the two PENDING alphas should both survive — FAIL must not occupy a slot."""
    a_fail = _mk_alpha("rank(close)", sharpe=0.0, pillar="momentum")
    a_fail.quality_status = "FAIL"
    a_p1 = _mk_alpha("rank(close)", sharpe=1.0, pillar="momentum")
    a_p2 = _mk_alpha("rank(close)", sharpe=2.0, pillar="momentum")
    alphas = [a_fail, a_p1, a_p2]
    drop = apply_family_cap(alphas, top_k=2)
    # Without M4 fix: FAIL@0 + p1@1 + p2@2 → 3 members, cap drops the lowest
    # (a_fail at score=0 keeps slot if it sorts higher than p1; either way
    # the FAIL contaminates the count). With M4: FAIL skipped → 2 PENDING ≤ top_k → no drop.
    assert drop == []


def test_apply_family_cap_handles_quality_status_enum():
    """quality_status may be a QualityStatus enum (str-subclass) or raw str —
    both must be recognized as FAIL."""
    from backend.models import QualityStatus
    a_enum_fail = _mk_alpha("rank(close)", sharpe=5.0, pillar="momentum")
    a_enum_fail.quality_status = QualityStatus.FAIL
    a_p1 = _mk_alpha("rank(close)", sharpe=1.0, pillar="momentum")
    a_p2 = _mk_alpha("rank(close)", sharpe=2.0, pillar="momentum")
    drop = apply_family_cap([a_enum_fail, a_p1, a_p2], top_k=2)
    assert drop == []  # enum FAIL excluded → only 2 PENDING remain
