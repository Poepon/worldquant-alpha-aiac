"""Unit tests for the shared verdict logic extracted in Feature 1 (2026-05-24).

`compute_verdict_from_signals` is a verbatim extraction of node_evaluate's verdict
block (evaluation.py:559-639) so /alphas/sync derives quality_status through the
SAME bands. These band-parity tests are the PRIMARY equivalence proof (the
regression suite does NOT execute node_evaluate); they pin every band + reason and
the str/dict hardening of the brain_actionable_fails extraction.

`_unpack_eval_thresholds` golden tests pin the 11-key contract (T2) on both a bare
`_eval_thresholds()` and a regime-adjusted dict.
"""
import pytest

from backend.agents.graph.nodes.evaluation import (
    compute_verdict_from_signals,
    _unpack_eval_thresholds,
    _eval_thresholds,
)
from backend.services.correlation_service import CorrSource


# A representative threshold bundle (matches _unpack_eval_thresholds(_eval_thresholds())
# defaults; tests don't depend on the exact config values, only their relations).
TH = {
    "sharpe_min": 1.5,
    "fitness_min": 1.2,
    "turnover_min": 0.01,
    "turnover_max": 0.4,
    "max_correlation": 0.7,
    "prov_sharpe_min": 1.25,
    "prov_fitness_min": 1.0,
    "prov_turnover_min": 0.01,
    "prov_turnover_max": 0.55,
    "score_pass_threshold": 0.8,
    "score_optimize_threshold": 0.3,
}


def _verdict(
    *,
    sharpe,
    fitness,
    turnover,
    checks=None,
    self_corr=0.1,
    self_corr_source=CorrSource.BRAIN,
    meets_thresholds=True,
    brain_check_details_present=True,
    brain_failed_checks=None,
    brain_can_submit=True,
    score=0.0,
    should_opt=False,
    extra_metrics=None,
):
    metrics = {"sharpe": sharpe, "fitness": fitness, "turnover": turnover,
               "checks": checks or []}
    if extra_metrics:
        metrics.update(extra_metrics)
    return compute_verdict_from_signals(
        metrics=metrics,
        sharpe=sharpe,
        fitness=fitness,
        turnover=turnover,
        self_corr=self_corr,
        self_corr_source=self_corr_source,
        meets_thresholds=meets_thresholds,
        brain_check_details_present=brain_check_details_present,
        brain_failed_checks=brain_failed_checks or [],
        brain_can_submit=brain_can_submit,
        score=score,
        should_opt=should_opt,
        expression="ts_rank(close, 20)",
        th=TH,
        check_self_corr=True,
        check_concentrated=True,
    )


# ── Band A ──────────────────────────────────────────────────────────────────

def test_band_a_pass():
    """hard_gate_pass + meets_thresholds + checks present + no v16/actionable → PASS."""
    vr = _verdict(sharpe=1.8, fitness=1.5, turnover=0.2)
    assert vr.decision.status == "PASS"
    assert vr.decision.reason == "hard_gate_pass"
    assert vr.hard_gate_pass is True


def test_band_a_pass_via_score_with_meets_thresholds_false():
    """score=0 (sync) cannot enter Band A on its own — needs meets_thresholds.
    Conversely a high score (mining) enters Band A even if meets_thresholds=False."""
    # score=0 + meets_thresholds=False + hard_gate_pass=True → NOT band A → near_pass? no
    # (prov band passes here too), so it lands in B. Pin the meets_thresholds-driven entry:
    vr = _verdict(sharpe=1.8, fitness=1.5, turnover=0.2, meets_thresholds=True, score=0.0)
    assert vr.decision.status == "PASS"
    # high-score entry (mining) with meets_thresholds=False still reaches Band A:
    vr2 = _verdict(sharpe=1.8, fitness=1.5, turnover=0.2,
                   meets_thresholds=False, score=0.9)
    assert vr2.decision.status == "PASS"


def test_band_a_v16_hard_flag():
    """sharpe>3 + drawdown=0 outlier → hard v16 flag → PASS_PROVISIONAL."""
    vr = _verdict(
        sharpe=4.0, fitness=2.0, turnover=0.2,
        extra_metrics={"drawdown": 0, "returns": 0.1, "os_sharpe": 2.0},
    )
    assert vr.decision.status == "PASS_PROVISIONAL"
    assert vr.decision.reason == "v16_hard_flags"
    assert any(f.get("severity") == "hard" for f in vr.v16_flags)


def test_band_a_unverified():
    """hard_gate_pass + BRAIN returned no check_details → PASS_PROVISIONAL/unverified."""
    vr = _verdict(sharpe=1.8, fitness=1.5, turnover=0.2,
                  brain_check_details_present=False)
    assert vr.decision.status == "PASS_PROVISIONAL"
    assert vr.decision.reason == "brain_checks_unverified"


def test_band_a_brain_actionable_fails():
    """hard_gate_pass + brain actionable fail + not submittable → PASS_PROVISIONAL/brain."""
    vr = _verdict(
        sharpe=1.8, fitness=1.5, turnover=0.2,
        score=0.9,  # force Band A entry independent of meets_thresholds
        brain_failed_checks=["LOW_FITNESS"],
        brain_can_submit=False,
    )
    assert vr.decision.status == "PASS_PROVISIONAL"
    assert vr.decision.reason == "brain_actionable_fails"
    assert vr.brain_actionable_fails == ["LOW_FITNESS"]


# ── Band B / C / D ──────────────────────────────────────────────────────────

def test_band_b_near_pass():
    """Below hard band but within provisional band → PASS_PROVISIONAL/near_pass."""
    vr = _verdict(sharpe=1.3, fitness=1.1, turnover=0.2)
    assert vr.decision.status == "PASS_PROVISIONAL"
    assert vr.decision.reason == "near_pass"
    assert vr.hard_gate_pass is False


def test_band_c_optimize():
    vr = _verdict(sharpe=0.5, fitness=0.5, turnover=0.2, should_opt=True, score=0.5)
    assert vr.decision.status == "OPTIMIZE"
    assert vr.decision.reason == "should_optimize"


def test_band_d_fail():
    vr = _verdict(sharpe=0.5, fitness=0.5, turnover=0.2, should_opt=False, score=0.0)
    assert vr.decision.status == "FAIL"
    assert vr.decision.reason == "below_all_bands"


# ── M7: CONCENTRATED_WEIGHT FAIL blocks hard gate ────────────────────────────

def test_concentrated_weight_fail_blocks_hard_gate():
    """Otherwise-passing alpha with CONCENTRATED_WEIGHT=FAIL → hard_gate_pass=False."""
    vr = _verdict(
        sharpe=1.8, fitness=1.5, turnover=0.2,
        checks=[{"name": "CONCENTRATED_WEIGHT", "result": "FAIL"}],
    )
    assert vr.hard_gate_pass is False
    # near_pass also requires concentrated_ok → FAIL
    assert vr.decision.status == "FAIL"


# ── ERROR-as-non-FAIL (documented mining behavior; sync S1 net catches it) ───

def test_sub_universe_error_does_not_block_hard_gate():
    """LOW_SUB_UNIVERSE_SHARPE=ERROR is NOT a FAIL → sub_universe_ok stays True.
    This is the verdict's (unchanged) ERROR semantics; sync's S1 guardrail, not
    this function, demotes such a PASS when compute_can_submit says unsubmittable."""
    vr = _verdict(
        sharpe=1.8, fitness=1.5, turnover=0.2,
        checks=[{"name": "LOW_SUB_UNIVERSE_SHARPE", "result": "ERROR"}],
    )
    assert vr.hard_gate_pass is True
    assert vr.decision.status == "PASS"


# ── SELF_CORRELATION unverified (synced reality) ─────────────────────────────

def test_self_corr_unknown_blocks_hard_gate_but_allows_near_pass():
    """source=UNKNOWN → self_corr_verified=False → hard_gate_pass=False, but
    self_corr_acceptable=True keeps near_pass reachable (mirrors mining + synced)."""
    vr = _verdict(
        sharpe=1.8, fitness=1.5, turnover=0.2,
        self_corr=0.0, self_corr_source=CorrSource.UNKNOWN,
    )
    assert vr.hard_gate_pass is False
    assert vr.decision.status == "PASS_PROVISIONAL"
    assert vr.decision.reason == "near_pass"


# ── M4: str/dict hardening of brain_actionable_fails extraction ──────────────

def test_brain_failed_checks_str_and_dict_parity():
    """The extraction must yield identical names whether brain_failed_checks is a
    list[str] (real callers: evaluate_with_brain_checks) or list[dict] (defensive)."""
    common = dict(sharpe=1.8, fitness=1.5, turnover=0.2, score=0.9,
                  brain_can_submit=False)
    vr_str = _verdict(brain_failed_checks=["LOW_FITNESS", "LOW_SHARPE"], **common)
    vr_dict = _verdict(
        brain_failed_checks=[{"name": "LOW_FITNESS"}, {"name": "LOW_SHARPE"}],
        **common,
    )
    assert vr_str.brain_actionable_fails == ["LOW_FITNESS", "LOW_SHARPE"]
    assert vr_dict.brain_actionable_fails == ["LOW_FITNESS", "LOW_SHARPE"]
    assert vr_str.decision.status == vr_dict.decision.status == "PASS_PROVISIONAL"


def test_brain_failed_checks_nonempty_str_list_does_not_crash():
    """Regression guard for the dormant 614 crash: a non-empty str-list must not
    raise (the pre-extraction `c.get('name')` over str-list raised AttributeError)."""
    vr = _verdict(
        sharpe=0.5, fitness=0.5, turnover=0.2,
        brain_failed_checks=["LOW_SHARPE", "LOW_FITNESS", "CONCENTRATED_WEIGHT"],
    )
    assert vr.decision.status == "FAIL"  # routed, not crashed


# ── _unpack_eval_thresholds golden (T2) ──────────────────────────────────────

_EXPECTED_TH_KEYS = {
    "sharpe_min", "fitness_min", "turnover_min", "turnover_max", "max_correlation",
    "prov_sharpe_min", "prov_fitness_min", "prov_turnover_min", "prov_turnover_max",
    "score_pass_threshold", "score_optimize_threshold",
}


def test_unpack_key_set_contract_bare():
    """T2 contract: exactly the 11 numeric keys — never check_self_corr/
    check_concentrated/corr_check_threshold (those stay in node_evaluate)."""
    th = _unpack_eval_thresholds(_eval_thresholds())
    assert set(th.keys()) == _EXPECTED_TH_KEYS


def test_unpack_maps_self_corr_max_to_max_correlation():
    cfg = _eval_thresholds()
    th = _unpack_eval_thresholds(cfg)
    assert th["max_correlation"] == cfg["self_corr_max"]


def test_unpack_regime_adjusted_input_and_prov_fallback_order():
    """Golden on a regime-adjusted-shaped dict: prov_*_min fall back to the
    already-resolved (scaled) main-band scalar; explicit prov overrides win."""
    regime_cfg = {
        "sharpe_min": 1.875,        # e.g. base 1.5 * 1.25 regime multiplier
        "fitness_min": 1.5,
        "turnover_min": 0.02,
        "turnover_max": 0.45,
        "self_corr_max": 0.65,
        "check_self_corr": True,
        "check_concentrated": True,
        "score_pass": 0.85,
        "score_optimize": 0.35,
        "provisional": {
            "sharpe_min": 1.5,      # explicit override → used as-is
            "fitness_min": 1.1,
            "turnover_max": 0.6,
            # NO turnover_min → falls back to main turnover_min (0.02)
        },
    }
    th = _unpack_eval_thresholds(regime_cfg)
    assert set(th.keys()) == _EXPECTED_TH_KEYS
    assert th["sharpe_min"] == 1.875
    assert th["max_correlation"] == 0.65
    assert th["prov_sharpe_min"] == 1.5
    assert th["prov_turnover_max"] == 0.6
    assert th["prov_turnover_min"] == 0.02        # fell back to main turnover_min
    assert th["score_pass_threshold"] == 0.85
    assert th["score_optimize_threshold"] == 0.35


def test_unpack_prov_defaults_when_no_provisional_block():
    """No provisional sub-dict → hardcoded defaults (0.6 / 0.85) + main-band fallbacks."""
    cfg = {
        "sharpe_min": 1.5, "fitness_min": 1.2, "turnover_min": 0.01,
        "turnover_max": 0.4, "self_corr_max": 0.7,
        "score_pass": 0.8, "score_optimize": 0.3,
    }
    th = _unpack_eval_thresholds(cfg)
    assert th["prov_sharpe_min"] == 1.5     # → main sharpe_min
    assert th["prov_fitness_min"] == 0.6    # hardcoded default
    assert th["prov_turnover_min"] == 0.01  # → main turnover_min
    assert th["prov_turnover_max"] == 0.85  # hardcoded default
