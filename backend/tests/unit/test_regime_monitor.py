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
# compute_regime_signal
# --------------------------------------------------------------------------- #
def test_regime_down_when_old_winners_still_negative():
    # The current trough: submitted winners re-sim deeply negative.
    probes = [
        _p("a", "submitted", 2.0, -0.74),
        _p("b", "submitted", 1.7, -0.20),
        _p("c", "submitted", 1.5, 0.10),
    ]
    sig = compute_regime_signal(probes, recovery_gate=1.25, turn_mean_threshold=0.5)
    assert sig["turn_detected"] is False
    assert sig["verdict"] == "REGIME_DOWN"
    assert sig["n_recovered_total"] == 0
    assert sig["submitted"]["mean_resim"] < 0.5


def test_regime_turning_via_mean_recovery():
    # Old winners' mean current-IS Sharpe back above the turn threshold.
    probes = [
        _p("a", "submitted", 2.0, 0.8),
        _p("b", "submitted", 1.7, 0.6),
        _p("c", "submitted", 1.5, 0.7),
    ]
    sig = compute_regime_signal(probes, recovery_gate=1.25, turn_mean_threshold=0.5)
    assert sig["turn_detected"] is True
    assert sig["verdict"] == "REGIME_TURNING"


def test_regime_turning_via_one_recovered_to_gate():
    # Mean still below turn threshold, but ONE alpha re-sims submittable (≥gate).
    probes = [
        _p("a", "submitted", 2.0, -0.5),
        _p("b", "submitted", 1.7, 0.1),
        _p("c", "backlog", 1.3, 1.40),     # recovered to submittable
    ]
    sig = compute_regime_signal(probes, recovery_gate=1.25,
                                turn_mean_threshold=0.5, turn_min_recovered=1)
    assert sig["turn_detected"] is True
    assert sig["verdict"] == "REGIME_TURNING"
    assert sig["n_recovered_total"] == 1
    assert "c" in sig["recovered_ids"]


def test_turn_min_recovered_threshold_respected():
    # turn_min_recovered=2 → a single recovered alpha is NOT enough.
    probes = [
        _p("a", "submitted", 2.0, -0.5),
        _p("b", "backlog", 1.3, 1.40),
    ]
    sig = compute_regime_signal(probes, recovery_gate=1.25,
                                turn_mean_threshold=0.5, turn_min_recovered=2)
    assert sig["turn_detected"] is False
    assert sig["verdict"] == "REGIME_DOWN"


def test_insufficient_when_all_resims_errored():
    probes = [
        _p("a", "submitted", 2.0, None),
        _p("b", "submitted", 1.7, None),
    ]
    sig = compute_regime_signal(probes, recovery_gate=1.25, turn_mean_threshold=0.5)
    assert sig["verdict"] == "INSUFFICIENT"
    assert sig["n_resimmed"] == 0
    assert sig["turn_detected"] is False


def test_cohorts_aggregated_separately():
    probes = [
        _p("a", "submitted", 2.0, 0.3),
        _p("b", "backlog", 1.3, 0.9),
    ]
    sig = compute_regime_signal(probes, recovery_gate=1.25, turn_mean_threshold=0.5)
    assert sig["submitted"]["n"] == 1 and sig["backlog"]["n"] == 1
    assert sig["submitted"]["mean_resim"] == 0.3
    assert sig["backlog"]["mean_resim"] == 0.9
    assert sig["n_probed"] == 2
