"""WinnerSelector — delay-aware band picking.

Stage A picks "winner" = sim that clears every BRAIN gate on the right
delay band. delay-0 is STRICTER (sharpe≥2.0 vs delay-1's 1.5, b8a9560).

These tests pin the contract that drives the 14d conversion rate — if
the selector accidentally treats a delay-0 result against the delay-1 band,
Stage A would over-count winners and the GO/STOP gate would fire wrong.
"""
from __future__ import annotations

import pytest

from backend.services.optimization.protocols import Variant, VariantSimResult
from backend.services.optimization.winner_selector import WinnerSelector


def _variant(tag: str = "t") -> Variant:
    return Variant(
        expression="dummy", settings={}, tag=tag,
        generator_name="settings_sweep",
    )


def _result(
    *,
    sharpe: float = 2.5,
    fitness: float = 1.5,
    turnover: float = 0.25,
    margin: float = 0.001,
    subuniv: float = 0.9,
    checks_passed: bool = True,
    error: str = None,
    brain_alpha_id: str = "abc",
) -> VariantSimResult:
    return VariantSimResult(
        variant=_variant(),
        sim_response={},
        sharpe=sharpe, fitness=fitness, turnover=turnover, margin=margin,
        subuniv=subuniv,
        brain_alpha_id=brain_alpha_id,
        checks_passed=checks_passed,
        error=error,
    )


# ---------------------------------------------------------------------------
# Delay band selection
# ---------------------------------------------------------------------------


def test_delay1_band_lets_sharpe_1_5_through():
    """delay-1 sharpe_min = 1.5 → sharpe=1.5 exactly passes."""
    sel = WinnerSelector()
    r = _result(sharpe=1.5, fitness=1.21, turnover=0.25)
    winners = sel.pick([r], delay=1)
    assert winners == [r]


def test_delay0_band_rejects_sharpe_1_8():
    """delay-0 sharpe_min = 2.0 → sharpe=1.8 (which would pass delay-1) is rejected."""
    sel = WinnerSelector()
    r = _result(sharpe=1.8, fitness=1.5, turnover=0.25, subuniv=0.9)
    winners = sel.pick([r], delay=0)
    assert winners == []


def test_delay0_band_accepts_sharpe_2_2():
    """delay-0 sharpe=2.2 clears every threshold."""
    sel = WinnerSelector()
    r = _result(sharpe=2.2, fitness=1.4, turnover=0.25, subuniv=0.85)
    winners = sel.pick([r], delay=0)
    assert winners == [r]


# ---------------------------------------------------------------------------
# Filter individual gates
# ---------------------------------------------------------------------------


def test_error_result_is_never_a_winner():
    sel = WinnerSelector()
    r = _result(error="sim_timeout(600s)")
    assert sel.pick([r], delay=1) == []


def test_checks_failed_result_is_never_a_winner():
    """Even if all numeric metrics clear the band, checks_passed=False
    (e.g. sub-univ FAIL) blocks the winner verdict."""
    sel = WinnerSelector()
    r = _result(sharpe=3.0, fitness=2.0, turnover=0.2, checks_passed=False)
    assert sel.pick([r], delay=1) == []


def test_none_metric_rejected():
    sel = WinnerSelector()
    for kwargs in (
        {"sharpe": None},
        {"fitness": None},
        {"turnover": None},
    ):
        r = _result(**kwargs)
        assert sel.pick([r], delay=1) == [], (
            f"{kwargs} should have been rejected"
        )


def test_turnover_outside_band_rejected():
    sel = WinnerSelector()
    # delay-1 turnover_max = 0.4 — 0.5 should be out
    assert sel.pick([_result(turnover=0.5)], delay=1) == []
    # delay-1 turnover_min = 0.01 — 0.0 should be out
    assert sel.pick([_result(turnover=0.0)], delay=1) == []


# ---------------------------------------------------------------------------
# Mixed batch — only winners returned, in input order
# ---------------------------------------------------------------------------


def test_mixed_batch_returns_only_winners_in_order():
    sel = WinnerSelector()
    r_ok1 = _result(sharpe=2.5, fitness=1.5, turnover=0.25, subuniv=0.9)
    r_fail = _result(sharpe=0.5)
    r_ok2 = _result(sharpe=3.0, fitness=2.0, turnover=0.3, subuniv=0.95)
    winners = sel.pick([r_ok1, r_fail, r_ok2], delay=1)
    assert winners == [r_ok1, r_ok2]


def test_empty_batch_returns_empty_list():
    sel = WinnerSelector()
    assert sel.pick([], delay=1) == []
