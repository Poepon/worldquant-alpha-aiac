"""Unit tests for backend.agents.graph.nodes.evaluation._eval_thresholds.

The flat threshold helper replaces the retired get_tier_thresholds (Ship
Phase 2.3, 2026-05-18). Critical invariants verified here:

  - Returns a single flat dict with keys: sharpe_min / fitness_min /
    turnover_min / turnover_max / subuniv_min / self_corr_max /
    check_self_corr / check_concentrated / score_pass / score_optimize /
    provisional (nested dict).
  - sharpe_min == max(settings.EVAL_SHARPE_MIN, settings.effective_sharpe_submit_min)
    when no override (so a global Consultant flag flip raises the bar).
  - sharpe_submit_min_override wins over both EVAL_SHARPE_MIN + effective
    (so a running task's brain_role_snapshot keeps its startup value across
    Consultant flag toggles — plan §4.A).
  - All other threshold values come straight from settings.EVAL_* constants.
"""
from __future__ import annotations

import pytest

from backend.agents.graph.nodes.evaluation import _eval_thresholds
from backend.config import _flag_override_cache, settings


@pytest.fixture(autouse=True)
def _clear_flag_cache():
    _flag_override_cache.clear()
    yield
    _flag_override_cache.clear()


def test_returns_flat_dict_with_required_keys():
    t = _eval_thresholds()
    required = {
        "sharpe_min", "fitness_min", "turnover_min", "turnover_max",
        "subuniv_min", "self_corr_max", "check_self_corr", "check_concentrated",
        "score_pass", "score_optimize", "provisional",
    }
    assert required.issubset(t.keys()), f"missing keys: {required - set(t)}"
    assert isinstance(t["provisional"], dict)


def test_provisional_sub_dict_carries_eval_provisional_settings():
    t = _eval_thresholds()
    p = t["provisional"]
    assert p["sharpe_min"] == settings.EVAL_PROVISIONAL_SHARPE_MIN
    assert p["fitness_min"] == settings.EVAL_PROVISIONAL_FITNESS_MIN
    assert p["turnover_max"] == settings.EVAL_PROVISIONAL_TURNOVER_MAX
    assert p["subuniv_min"] == settings.EVAL_PROVISIONAL_SUBUNIV_MIN


def test_threshold_values_from_settings_constants():
    t = _eval_thresholds()
    assert t["fitness_min"] == settings.EVAL_FITNESS_MIN
    assert t["turnover_min"] == settings.EVAL_TURNOVER_MIN
    assert t["turnover_max"] == settings.EVAL_TURNOVER_MAX
    assert t["subuniv_min"] == settings.EVAL_SUBUNIV_MIN
    assert t["self_corr_max"] == settings.EVAL_SELF_CORR_MAX
    assert t["check_self_corr"] is True
    assert t["check_concentrated"] is True
    assert t["score_pass"] == settings.EVAL_SCORE_PASS
    assert t["score_optimize"] == settings.EVAL_SCORE_OPTIMIZE


def test_user_mode_sharpe_min_uses_eval_constant():
    """No Consultant flag, no override → sharpe_min = max(EVAL, effective).
    In User mode EVAL_SHARPE_MIN (1.5) == effective_sharpe_submit_min (1.5),
    so max collapses to EVAL_SHARPE_MIN."""
    t = _eval_thresholds()
    assert t["sharpe_min"] == settings.EVAL_SHARPE_MIN
    assert t["sharpe_min"] == 1.5


def test_consultant_mode_raises_sharpe_min_via_max():
    """Global Consultant flag ON → effective_sharpe_submit_min = 1.58 →
    max(EVAL_SHARPE_MIN=1.5, 1.58) = 1.58."""
    _flag_override_cache["ENABLE_BRAIN_CONSULTANT_MODE"] = True
    t = _eval_thresholds()
    assert t["sharpe_min"] == 1.58


def test_explicit_override_wins_over_settings():
    """sharpe_submit_min_override is the task-startup snapshot — it wins
    over both EVAL_SHARPE_MIN and settings.effective_sharpe_submit_min
    so a running task survives Consultant flag toggles."""
    # User mode (1.5) globally
    t = _eval_thresholds(sharpe_submit_min_override=1.58)
    assert t["sharpe_min"] == 1.58

    # Even with Consultant ON globally, snapshot of an earlier-started
    # task at 1.5 still wins.
    _flag_override_cache["ENABLE_BRAIN_CONSULTANT_MODE"] = True
    t = _eval_thresholds(sharpe_submit_min_override=1.5)
    assert t["sharpe_min"] == 1.5


def test_override_zero_is_treated_as_explicit_value_not_none():
    """0.0 is a valid override (drops the bar). Must NOT be confused with
    None (falsy) — explicit zero wins."""
    t = _eval_thresholds(sharpe_submit_min_override=0.0)
    assert t["sharpe_min"] == 0.0


def test_no_tier_kwarg_accepted():
    """Post tier-system removal, _eval_thresholds takes ONLY the optional
    override kwarg — no tier parameter. Sanity check: passing tier= would
    TypeError immediately."""
    with pytest.raises(TypeError):
        _eval_thresholds(tier=2)  # type: ignore[call-arg]
