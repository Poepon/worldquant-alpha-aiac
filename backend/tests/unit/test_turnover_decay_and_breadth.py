"""Gap 2 (turnover→decay direct-pick) + Gap 1 (breadth guard) — 2026-05-20.

Gap 2: reference machine_lib computes ONE decay from observed turnover (a
graduated schedule) instead of sweeping all DECAY_OPTIONS. On a 3-slot USER
account that single targeted sim saves slots, so it's tried FIRST.

Gap 1: the breadth floor used `if long_count and short_count` (0 is falsy), so
a one-sided long-only / short-only alpha bypassed it. Fixed to
(long or 0)+(short or 0).
"""
from __future__ import annotations

import pytest

from backend.optimization_chain import (
    recommend_decay_for_turnover,
    generate_settings_variants,
    OptimizationContext,
    OptimizationType,
)


# ---------------------------------------------------------------------------
# Gap 2: recommend_decay_for_turnover schedule
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("turnover,expected", [
    (0.75, 16),   # base*4
    (0.65, 15),   # base*3 + 3
    (0.55, 12),   # base*3
    (0.45, 8),    # base*2
    (0.37, 8),    # d + 4
    (0.32, 6),    # d + 2
    (0.30, None), # boundary — not > 0.3
    (0.20, None),
    (0.0, None),
])
def test_recommend_decay_base4(turnover, expected):
    assert recommend_decay_for_turnover(4, turnover) == expected


@pytest.mark.parametrize("turnover,expected", [
    (0.75, 4),    # base=max(0,1)=1 → 1*4 (no zero-trap)
    (0.55, 3),    # 1*3
    (0.45, 2),    # 1*2
    (0.37, 4),    # d(0) + 4
    (0.32, 2),    # d(0) + 2
    (0.20, None),
])
def test_recommend_decay_base0_no_zero_trap(turnover, expected):
    # A decay-0 alpha must still get real smoothing (0*4 would stay 0).
    assert recommend_decay_for_turnover(0, turnover) == expected


def test_recommend_decay_monotonic_in_turnover():
    vals = [recommend_decay_for_turnover(4, t) or 0
            for t in (0.31, 0.36, 0.41, 0.51, 0.61, 0.71)]
    assert vals == sorted(vals)  # higher turnover ⇒ ≥ decay


# ---------------------------------------------------------------------------
# Gap 2: wired into generate_settings_variants
# ---------------------------------------------------------------------------

def _base():
    return {"neutralization": "INDUSTRY", "decay": 4, "truncation": 0.02}


def test_settings_variants_high_turnover_targeted_first():
    ctx = OptimizationContext(expression="x", turnover=0.75)
    out = generate_settings_variants(_base(), context=ctx)
    first = out[0]
    assert first["change_type"] == OptimizationType.SETTINGS_DECAY.value
    assert first["decay"] == 16  # base 4, turnover 0.75 → 4*4
    assert "turnover-targeted" in first["description"]
    # No duplicate swept decay=16 variant (saved a sim).
    dups = [v for v in out[1:]
            if v["change_type"] == OptimizationType.SETTINGS_DECAY.value
            and v["decay"] == 16]
    assert dups == []


def test_settings_variants_low_turnover_no_targeted():
    ctx = OptimizationContext(expression="x", turnover=0.20)
    out = generate_settings_variants(_base(), context=ctx)
    assert not any("turnover-targeted" in v["description"] for v in out)


def test_settings_variants_no_context_unchanged():
    out = generate_settings_variants(_base())  # no context
    assert not any("turnover-targeted" in v["description"] for v in out)


# ---------------------------------------------------------------------------
# Gap 1: breadth guard no longer bypassed by one-sided alphas
# ---------------------------------------------------------------------------

from backend.alpha_scoring import evaluate_alpha_comprehensive


def _sim(long_c, short_c, turnover=0.2, sharpe=2.0, fitness=1.5):
    return {"is": {
        "sharpe": sharpe, "fitness": fitness, "turnover": turnover,
        "longCount": long_c, "shortCount": short_c,
        "margin": 0.01, "drawdown": 0.05, "returns": 0.1,
    }}


def _has_concentrated(ev):
    return any("CONCENTRATED_WEIGHT" in t for t in ev.failed_tests)


def test_breadth_one_sided_low_now_flagged():
    # long-only with 5 positions — old `and` guard skipped this, now flagged.
    ev = evaluate_alpha_comprehensive(_sim(5, 0), use_brain_checks=False)
    assert _has_concentrated(ev)


def test_breadth_one_sided_high_not_flagged():
    ev = evaluate_alpha_comprehensive(_sim(5000, 0), use_brain_checks=False)
    assert not _has_concentrated(ev)


def test_breadth_both_zero_skipped_as_missing():
    # 0 positions = missing/unsimulated data → must not flag.
    ev = evaluate_alpha_comprehensive(_sim(0, 0), use_brain_checks=False)
    assert not _has_concentrated(ev)


def test_breadth_two_sided_low_still_flagged():
    ev = evaluate_alpha_comprehensive(_sim(3, 4), use_brain_checks=False)
    assert _has_concentrated(ev)
