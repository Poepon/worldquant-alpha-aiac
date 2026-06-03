"""Unit tests for the RobustnessFilter 止血 (2026-06-03 methodology fix).

Covers the two gates that deflate settings-sweep winners before they reach the
submit-backlog: the SR0 expected-max-Sharpe deflation and the plateau (lone-spike)
gate. Pure functions — no DB / BRAIN.
"""
from __future__ import annotations

import math

from backend.config import settings
from backend.services.optimization.protocols import Variant, VariantSimResult
from backend.services.optimization.robustness import (
    RobustnessFilter,
    expected_max_sharpe,
    plateau_ok,
)


_SHARPE_MIN = float(settings.eval_thresholds(1)["sharpe_min"])
_PLATEAU_BAND = 0.15
_FLOOR = _SHARPE_MIN - _PLATEAU_BAND


def _vsr(neut: str, sharpe: float, *, decay: int = 4, error: str = None) -> VariantSimResult:
    return VariantSimResult(
        variant=Variant(
            expression="x",
            settings={"neutralization": neut, "decay": decay},
            tag=f"decay={decay}|neut={neut}",
            generator_name="settings_sweep",
        ),
        sim_response={},
        sharpe=sharpe, fitness=1.5, turnover=0.25, margin=0.001, subuniv=0.9,
        brain_alpha_id="m", checks_passed=True, error=error,
    )


# ---------------------------------------------------------------------------
# expected_max_sharpe (SR0)
# ---------------------------------------------------------------------------


def test_sr0_degenerate_cases():
    assert expected_max_sharpe([]) == 0.0
    assert expected_max_sharpe([1.5]) == 0.0          # N<2 → no multiple-testing
    assert expected_max_sharpe([1.0, 1.0, 1.0]) == 0.0  # zero variance
    # None / NaN filtered out
    assert expected_max_sharpe([1.0, None, float("nan")]) == 0.0


def test_sr0_known_value():
    # N=2, sharpes [0,1]: mean=0.5, var(ddof=1)=0.5, std=0.7071.
    # q1=Φ⁻¹(0.5)=0, q2=Φ⁻¹(1-1/(2e)=0.8161)=0.8995.
    # SR0 = 0.7071*((1-γ)*0 + γ*0.8995) = 0.7071*0.5772*0.8995 ≈ 0.367
    sr0 = expected_max_sharpe([0.0, 1.0])
    assert math.isclose(sr0, 0.367, abs_tol=0.01)


def test_sr0_monotone_in_spread_and_n():
    # Wider spread → higher SR0.
    assert expected_max_sharpe([0.0, 2.0]) > expected_max_sharpe([0.0, 1.0])
    # More trials at same spread → higher expected max (more chances for luck).
    assert expected_max_sharpe([0.0, 1.0, 0.0, 1.0, 0.0, 1.0]) > expected_max_sharpe([0.0, 1.0])


# ---------------------------------------------------------------------------
# plateau_ok
# ---------------------------------------------------------------------------


def test_plateau_lone_spike_rejected():
    winner = _vsr("INDUSTRY", _SHARPE_MIN + 0.2)
    siblings = [_vsr("INDUSTRY", _FLOOR - 0.3), _vsr("INDUSTRY", _FLOOR - 0.5)]
    ok, reason = plateau_ok(
        winner, [winner, *siblings], sharpe_min=_SHARPE_MIN, plateau_band=_PLATEAU_BAND
    )
    assert ok is False
    assert "lone_spike" in reason


def test_plateau_with_strong_sibling_kept():
    winner = _vsr("INDUSTRY", _SHARPE_MIN + 0.2)
    siblings = [_vsr("INDUSTRY", _FLOOR + 0.05), _vsr("INDUSTRY", 0.1)]
    ok, reason = plateau_ok(
        winner, [winner, *siblings], sharpe_min=_SHARPE_MIN, plateau_band=_PLATEAU_BAND
    )
    assert ok is True
    assert "plateau_ok" in reason


def test_plateau_unassessable_passes():
    # No same-neut sibling → can't assess → pass (don't drop a real winner).
    winner = _vsr("INDUSTRY", _SHARPE_MIN + 0.2)
    others = [_vsr("SECTOR", 2.0), _vsr("SECTOR", 1.9)]
    ok, reason = plateau_ok(
        winner, [winner, *others], sharpe_min=_SHARPE_MIN, plateau_band=_PLATEAU_BAND
    )
    assert ok is True
    assert reason == "plateau_unassessed"


# ---------------------------------------------------------------------------
# RobustnessFilter.apply
# ---------------------------------------------------------------------------


def test_filter_rejects_lone_spike_keeps_plateau():
    # Two winners, both clear the band; one is a lone spike (no strong INDUSTRY
    # sibling), the other (SECTOR) has a strong sibling → plateau.
    spike = _vsr("INDUSTRY", _SHARPE_MIN + 0.3)
    plateau_winner = _vsr("SECTOR", _SHARPE_MIN + 0.3)
    plateau_sib = _vsr("SECTOR", _FLOOR + 0.05)
    industry_low = _vsr("INDUSTRY", 0.2)  # spike's only sibling, far below floor
    all_results = [spike, plateau_winner, plateau_sib, industry_low]

    # deflation off → isolate plateau behaviour
    rf = RobustnessFilter(require_deflation=False, plateau_band=_PLATEAU_BAND)
    survivors, rejected = rf.apply([spike, plateau_winner], all_results, 1)

    survived_tags = {s.variant.tag for s in survivors}
    assert plateau_winner.variant.tag in survived_tags
    assert spike.variant.tag not in survived_tags
    assert any("lone_spike" in r["reason"] for r in rejected)


def test_filter_rejects_marginal_winner_in_noisy_field():
    # A wide, noisy field → high SR0. A winner barely above SR0 is rejected;
    # a clearly-dominant winner is kept. (plateau off → isolate deflation.)
    field = [_vsr("INDUSTRY", s) for s in (1.6, 1.2, 0.6, 0.0, -0.6, -1.0, 0.3, -0.3)]
    sr0 = expected_max_sharpe([r.sharpe for r in field])
    assert sr0 > 0.5  # the noisy field really does have a non-trivial luck bar

    marginal = _vsr("INDUSTRY", sr0 - 0.1)
    dominant = _vsr("INDUSTRY", sr0 + 1.0)
    all_results = field + [marginal, dominant]

    rf = RobustnessFilter(require_plateau=False)
    survivors, rejected = rf.apply([marginal, dominant], all_results, 1)

    survived = {round(s.sharpe, 4) for s in survivors}
    assert round(dominant.sharpe, 4) in survived
    assert round(marginal.sharpe, 4) not in survived
    assert any(r["reason"] == "failed_deflation" for r in rejected)


def test_filter_noop_when_clean_dominant_winner():
    # A single clear winner on a tight, all-high field passes both gates.
    field = [_vsr("INDUSTRY", s) for s in (2.5, 2.3, 2.2, 2.1)]
    rf = RobustnessFilter()
    survivors, rejected = rf.apply([field[0]], field, 1)
    assert len(survivors) == 1
    assert rejected == []
