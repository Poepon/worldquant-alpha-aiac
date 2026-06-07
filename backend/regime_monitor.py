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


def _is_stale(p: Dict[str, Any], stale_eps: float) -> bool:
    """A re-sim that returns EXACTLY at baseline carries no regime-change signal:
    BRAIN dedups an identical expression+settings to the stored alpha and returns
    its ORIGINAL metrics instead of a fresh current-window sim. Treat as stale."""
    b = p.get("baseline_sharpe")
    r = p.get("resim_sharpe")
    return r is not None and b is not None and abs(r - b) <= stale_eps


def _agg(probes: List[Dict[str, Any]], *, recovery_gate: float, stale_eps: float) -> Dict[str, Any]:
    """Aggregate one cohort (submitted or backlog). Each probe dict:
    ``{alpha_id, baseline_sharpe, resim_sharpe (None on error)}``.

    Cache/dedup guard (2026-06-08): rows whose re-sim == baseline (|Δ|<=stale_eps)
    are classed ``stale`` and EXCLUDED from mean/delta/recovery so a BRAIN cache
    hit can't masquerade as a recovery (the 2026-06-07 false REGIME_TURNING:
    5/23 re-sims came back exactly at baseline). All headline stats below are over
    the FRESH (cache-excluded) subset."""
    valid = [p for p in probes if p.get("resim_sharpe") is not None]
    stale = [p for p in valid if _is_stale(p, stale_eps)]
    fresh = [p for p in valid if not _is_stale(p, stale_eps)]
    resims = [p["resim_sharpe"] for p in fresh]
    base_fresh = [p["baseline_sharpe"] for p in fresh if p.get("baseline_sharpe") is not None]
    n_recovered = sum(1 for r in resims if r >= recovery_gate)
    return {
        "n": len(probes),
        "n_resimmed": len(valid),
        "n_fresh": len(fresh),
        "n_stale": len(stale),
        "stale_ids": [p["alpha_id"] for p in stale],
        "mean_baseline": round(mean(base_fresh), 4) if base_fresh else None,
        "mean_resim": round(mean(resims), 4) if resims else None,
        "mean_delta": round(mean(resims) - mean(base_fresh), 4) if resims and base_fresh else None,
        "n_recovered": n_recovered,
        "frac_recovered": round(n_recovered / len(fresh), 4) if fresh else None,
        "recovered_ids": [p["alpha_id"] for p in fresh if p["resim_sharpe"] >= recovery_gate],
    }


def compute_regime_signal(
    probes: List[Dict[str, Any]],
    *,
    recovery_gate: float,
    turn_mean_threshold: float,
    turn_min_recovered: int = 1,
    stale_eps: float = 1e-3,
    turn_recovered_frac: float = 0.5,
    turn_max_decay: float = -0.25,
    min_fresh: int = 3,
) -> Dict[str, Any]:
    """Turn a list of probe results into a regime-turn signal.

    Each ``probes`` item: ``{alpha_id, kind('submitted'|'backlog'),
    baseline_sharpe, resim_sharpe (None on sim error)}``.

    Turn sensor = the SUBMITTED cohort (old winners). REGIME_TURNING means the
    old edges genuinely work on CURRENT data again — NOT merely "not all crashed".
    Over the FRESH (cache-excluded, see _agg) submitted re-sims, ALL must hold:
      1. enough fresh re-sims to judge (``n_fresh >= min_fresh``); else INSUFFICIENT
      2. a majority re-sim at/above the gate (``frac_recovered >= turn_recovered_frac``)
      3. the cohort is not in material decay (``mean_delta >= turn_max_decay``)
      4. floors: ``mean_resim >= turn_mean_threshold`` AND ``n_recovered >= turn_min_recovered``
    Conservative by design — re-engaging mining off a noisy/decayed probe is the
    expensive error. (2026-06-08 recal: the old ``mean>=0.5 OR 1-recovered`` rule
    false-fired TURNING on a -0.74 decay inflated by 5 cache-hit "recoveries".)
    """
    submitted = [p for p in probes if p.get("kind") == "submitted"]
    backlog = [p for p in probes if p.get("kind") == "backlog"]
    sub_agg = _agg(submitted, recovery_gate=recovery_gate, stale_eps=stale_eps)
    bk_agg = _agg(backlog, recovery_gate=recovery_gate, stale_eps=stale_eps)

    all_fresh = [
        p for p in probes
        if p.get("resim_sharpe") is not None and not _is_stale(p, stale_eps)
    ]
    n_recovered_total = sum(1 for p in all_fresh if p["resim_sharpe"] >= recovery_gate)

    n_fresh_sub = sub_agg["n_fresh"]
    frac = sub_agg["frac_recovered"]
    delta = sub_agg["mean_delta"]
    smean = sub_agg["mean_resim"]

    if n_fresh_sub < int(min_fresh):
        verdict = "INSUFFICIENT"          # too few fresh submitted re-sims to judge
        turn_detected = False
    else:
        turn_detected = bool(
            frac is not None and frac >= float(turn_recovered_frac)
            and delta is not None and delta >= float(turn_max_decay)
            and smean is not None and smean >= float(turn_mean_threshold)
            and sub_agg["n_recovered"] >= int(turn_min_recovered)
        )
        verdict = "REGIME_TURNING" if turn_detected else "REGIME_DOWN"

    return {
        "verdict": verdict,
        "turn_detected": turn_detected,
        "n_probed": len(probes),
        "n_resimmed": sub_agg["n_resimmed"] + bk_agg["n_resimmed"],
        "n_fresh": len(all_fresh),
        "n_stale": sub_agg["n_stale"] + bk_agg["n_stale"],
        "n_recovered_total": n_recovered_total,
        "recovery_gate": recovery_gate,
        "turn_mean_threshold": turn_mean_threshold,
        "turn_recovered_frac": turn_recovered_frac,
        "turn_max_decay": turn_max_decay,
        "submitted": sub_agg,
        "backlog": bk_agg,
        "recovered_ids": sorted({*sub_agg["recovered_ids"], *bk_agg["recovered_ids"]}),
        "stale_ids": sorted({*sub_agg["stale_ids"], *bk_agg["stale_ids"]}),
    }
