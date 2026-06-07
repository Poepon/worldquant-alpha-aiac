"""Unit tests for backend/regime_monitor.py (greenfield branch B regime probe).

The BRAIN re-sim leg is exercised in the worker env (creds there); these tests
cover the PURE logic: probe-set Variant building (current-window, no frozen dates)
+ regime-turn signal computation.
"""
from backend.regime_monitor import make_variant, compute_regime_signal


def _p(aid, kind, baseline, resim):
    return {"alpha_id": aid, "kind": kind, "baseline_sharpe": baseline, "resim_sharpe": resim}


# --------------------------------------------------------------------------- #
# make_variant — current-window re-sim (NO frozen startDate/endDate)
# --------------------------------------------------------------------------- #
def test_make_variant_strips_dates_keeps_structural():
    spec = {
        "alpha_id": "mLxlen69", "kind": "submitted",
        "expression": "ts_decay_linear(-ts_rank(returns,5),10)",
        "region": "USA", "universe": "TOP2000", "delay": 1, "decay": 4,
        "neutralization": "SECTOR", "truncation": 0.08,
        # frozen-window noise that must NOT reach the re-sim:
        "startDate": "2019-01-01", "endDate": "2023-12-31", "baseline_sharpe": 2.01,
    }
    v = make_variant(spec)
    assert v.expression == spec["expression"]
    assert v.settings == {
        "region": "USA", "universe": "TOP2000", "delay": 1, "decay": 4,
        "neutralization": "SECTOR", "truncation": 0.08,
    }
    assert "startDate" not in v.settings and "endDate" not in v.settings
    assert v.generator_name == "regime_monitor"


def test_make_variant_omits_none_settings():
    v = make_variant({"alpha_id": "x", "kind": "backlog",
                      "expression": "close", "region": "USA",
                      "universe": None, "delay": 1})
    assert "universe" not in v.settings        # None → omitted → BRAIN default
    assert v.settings["region"] == "USA"


# --------------------------------------------------------------------------- #
# compute_regime_signal  (2026-06-08 recal: frac+delta turn + cache-hit exclusion)
# --------------------------------------------------------------------------- #
def test_regime_down_when_old_winners_still_negative():
    # The current trough: submitted winners re-sim deeply negative.
    probes = [
        _p("a", "submitted", 2.0, -0.74),
        _p("b", "submitted", 1.7, -0.20),
        _p("c", "submitted", 1.5, 0.10),
    ]
    sig = compute_regime_signal(probes, recovery_gate=1.25, turn_mean_threshold=1.0)
    assert sig["turn_detected"] is False
    assert sig["verdict"] == "REGIME_DOWN"
    assert sig["n_recovered_total"] == 0
    assert sig["submitted"]["mean_resim"] < 0.5


def test_regime_turning_when_winners_recover_to_gate():
    # Genuine turn: >= min_fresh submitted, majority re-sim back AT/above the gate
    # with little decay vs their own baseline.
    probes = [
        _p("a", "submitted", 1.5, 1.6),
        _p("b", "submitted", 1.4, 1.45),
        _p("c", "submitted", 1.6, 1.5),
    ]
    sig = compute_regime_signal(probes, recovery_gate=1.25, turn_mean_threshold=1.0)
    assert sig["turn_detected"] is True
    assert sig["verdict"] == "REGIME_TURNING"
    assert sig["submitted"]["frac_recovered"] == 1.0


def test_single_recovery_not_enough_to_turn():
    # One alpha back at the gate but the cohort is decayed → NOT a turn. Kills the
    # 2026-06-07 false-positive path ("1 recovered → TURNING").
    probes = [
        _p("a", "submitted", 2.0, -0.5),
        _p("b", "submitted", 1.7, 0.1),
        _p("c", "submitted", 1.5, 1.40),   # only this one recovered
    ]
    sig = compute_regime_signal(probes, recovery_gate=1.25, turn_mean_threshold=1.0)
    assert sig["turn_detected"] is False
    assert sig["verdict"] == "REGIME_DOWN"
    assert sig["submitted"]["frac_recovered"] < 0.5


def test_stale_resim_excluded_from_recovery():
    # A re-sim EXACTLY at baseline = BRAIN dedup/cache (no current-data signal):
    # excluded from fresh/recovery so it can't inflate a turn (the 2026-06-07 bug:
    # 5/23 re-sims came back exactly at baseline and were counted as recoveries).
    probes = [
        _p("a", "submitted", 2.18, 2.18),   # stale (cache) — was a false "recovery"
        _p("b", "submitted", 1.75, 1.75),   # stale
        _p("c", "submitted", 1.95, 0.78),   # fresh decay
        _p("d", "submitted", 1.6, 0.8),     # fresh decay
        _p("e", "submitted", 1.5, 0.9),     # fresh decay
    ]
    sig = compute_regime_signal(probes, recovery_gate=1.25, turn_mean_threshold=1.0)
    assert sig["submitted"]["n_stale"] == 2
    assert set(sig["submitted"]["stale_ids"]) == {"a", "b"}
    assert sig["submitted"]["n_fresh"] == 3
    assert sig["n_recovered_total"] == 0       # the 2 cache hits don't count
    assert sig["verdict"] == "REGIME_DOWN"


def test_insufficient_when_too_few_fresh_resims():
    # All errored (or fewer than min_fresh fresh submitted) → can't judge.
    probes = [
        _p("a", "submitted", 2.0, None),
        _p("b", "submitted", 1.7, None),
    ]
    sig = compute_regime_signal(probes, recovery_gate=1.25, turn_mean_threshold=1.0)
    assert sig["verdict"] == "INSUFFICIENT"
    assert sig["n_resimmed"] == 0
    assert sig["turn_detected"] is False


def test_cohorts_aggregated_separately():
    probes = [
        _p("a", "submitted", 2.0, 0.3),
        _p("b", "backlog", 1.3, 0.9),
    ]
    sig = compute_regime_signal(probes, recovery_gate=1.25, turn_mean_threshold=1.0)
    assert sig["submitted"]["n"] == 1 and sig["backlog"]["n"] == 1
    assert sig["submitted"]["mean_resim"] == 0.3
    assert sig["backlog"]["mean_resim"] == 0.9
    assert sig["n_probed"] == 2
