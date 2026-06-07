"""Regime-turn monitor — periodic re-sim of the submitted pool (+ backlog sample)
on CURRENT BRAIN data to detect when the market regime turns back favorable.

Context (`docs/dev_plan_greenfield_2026-06-07.md` branch B; memory
`project_dev_plan_branch_b_regime_trough`): production is PAUSED in a regime
trough — old winning structures decayed/inverted (mLxlen69 submitted IS 2.01 →
re-sim IS −0.74), and #40 confirmed the backlog is BRAIN-authoritatively dilutive
+ arbitraged-away. Re-simulating the already-known winners on CURRENT data is the
cheapest, most direct regime sensor: ``simulate_alpha`` uses a ROLLING
``test_period`` (P2Y0M), NOT the frozen 2019-2023 window, so a re-sim reflects how
the structure does on recent-visible data. When the old winners' current-IS Sharpe
recovers, the regime may be turning → re-engage mining.

⚠️ 口径 = current **IS** (rolling window), NOT OS (BRAIN hides realized OS;
`reference_brain_os_hidden_is_only`). This is a regime-DECAY sensor, not a
submission signal — it tells you WHEN to re-engage, not WHAT to submit.

This module is the PURE logic (probe-set Variant building + signal computation),
separable from the BRAIN call so it is unit-testable. The beat task
(`tasks/regime_monitor_tasks.py`) loads rows, runs ``BrainSimulator``, persists.
"""
from __future__ import annotations

from statistics import mean
from typing import Any, Dict, List, Optional

# Settings keys passed to simulate_alpha for a re-sim. We deliberately pass ONLY
# the structural params — NOT startDate/endDate — so BRAIN uses its default rolling
# test_period (current window). simulate_alpha doesn't even accept dates; this list
# mirrors its signature (brain_adapter.py:659).
_SIM_SETTING_KEYS = (
    "region", "universe", "delay", "decay", "neutralization", "truncation",
)


def make_variant(spec: Dict[str, Any]):
    """Build an optimization ``Variant`` for a re-sim from a probe spec dict.

    ``spec`` must carry ``alpha_id`` + ``expression`` + the structural settings
    (region/universe/delay/decay/neutralization/truncation). Lazy import of
    Variant keeps this module import-light for unit tests that only exercise the
    signal math.
    """
    from backend.services.optimization.protocols import Variant
    settings = {k: spec[k] for k in _SIM_SETTING_KEYS if spec.get(k) is not None}
    return Variant(
        expression=spec["expression"],
        settings=settings,
        tag=f"regime:{spec.get('kind', '?')}:{spec['alpha_id']}",
        generator_name="regime_monitor",
        generation=0,
    )


def _agg(probes: List[Dict[str, Any]], *, recovery_gate: float) -> Dict[str, Any]:
    """Aggregate one cohort (submitted or backlog). Each probe dict:
    ``{alpha_id, baseline_sharpe, resim_sharpe (None on error)}``."""
    resimmed = [p for p in probes if p.get("resim_sharpe") is not None]
    baselines = [p["baseline_sharpe"] for p in probes if p.get("baseline_sharpe") is not None]
    resims = [p["resim_sharpe"] for p in resimmed]
    n_recovered = sum(1 for r in resims if r >= recovery_gate)
    return {
        "n": len(probes),
        "n_resimmed": len(resimmed),
        "mean_baseline": round(mean(baselines), 4) if baselines else None,
        "mean_resim": round(mean(resims), 4) if resims else None,
        "mean_delta": (
            round(mean(resims) - mean([p["baseline_sharpe"] for p in resimmed
                                       if p.get("baseline_sharpe") is not None]), 4)
            if resims and any(p.get("baseline_sharpe") is not None for p in resimmed)
            else None
        ),
        "n_recovered": n_recovered,
        "recovered_ids": [p["alpha_id"] for p in resimmed if p["resim_sharpe"] >= recovery_gate],
    }


def compute_regime_signal(
    probes: List[Dict[str, Any]],
    *,
    recovery_gate: float,
    turn_mean_threshold: float,
    turn_min_recovered: int = 1,
) -> Dict[str, Any]:
    """Turn a list of probe results into a regime-turn signal.

    Each ``probes`` item: ``{alpha_id, kind('submitted'|'backlog'),
    baseline_sharpe, resim_sharpe (None on sim error)}``.

    ``turn_detected`` fires when the SUBMITTED cohort's mean current-IS Sharpe
    recovers to ``turn_mean_threshold`` (old winners meaningfully positive again)
    OR ``turn_min_recovered`` alphas re-sim at/above ``recovery_gate`` (=
    submittable on current data). Both are deliberately conservative — a single
    noisy re-sim shouldn't declare the regime turned.
    """
    submitted = [p for p in probes if p.get("kind") == "submitted"]
    backlog = [p for p in probes if p.get("kind") == "backlog"]
    sub_agg = _agg(submitted, recovery_gate=recovery_gate)
    bk_agg = _agg(backlog, recovery_gate=recovery_gate)

    all_resimmed = [p for p in probes if p.get("resim_sharpe") is not None]
    n_recovered_total = sum(1 for p in all_resimmed if p["resim_sharpe"] >= recovery_gate)

    sub_mean = sub_agg["mean_resim"]
    turn_detected = bool(
        (sub_mean is not None and sub_mean >= turn_mean_threshold)
        or (n_recovered_total >= int(turn_min_recovered))
    )

    if not all_resimmed:
        verdict = "INSUFFICIENT"          # every re-sim errored (auth/slot/data)
    elif turn_detected:
        verdict = "REGIME_TURNING"        # re-engage candidate — review + resume mining
    else:
        verdict = "REGIME_DOWN"           # still a trough — stay paused

    return {
        "verdict": verdict,
        "turn_detected": turn_detected,
        "n_probed": len(probes),
        "n_resimmed": len(all_resimmed),
        "n_recovered_total": n_recovered_total,
        "recovery_gate": recovery_gate,
        "turn_mean_threshold": turn_mean_threshold,
        "submitted": sub_agg,
        "backlog": bk_agg,
        "recovered_ids": sorted(
            {*sub_agg["recovered_ids"], *bk_agg["recovered_ids"]}
        ),
    }
