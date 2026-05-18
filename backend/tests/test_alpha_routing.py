"""Tests for backend/alpha_routing.py (P0 #3, 2026-05-15).

Covers route_alpha_action() for all four bands and every sub-case within
Band A, plus boundary conditions and RoutingDecision field shape.

Behaviour contract: default score thresholds 0.8/0.3 — identical to the
pre-refactor globals, so no numeric changes are introduced.
"""
from __future__ import annotations

import pytest

from backend.alpha_routing import RoutingDecision, route_alpha_action


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _call(**overrides):
    """Call route_alpha_action with sensible defaults; override as needed."""
    defaults = dict(
        hard_gate_pass=False,
        meets_thresholds=False,
        score=0.0,
        score_pass_threshold=0.8,
        has_v16_hard_flags=False,
        brain_checks_present=True,
        brain_actionable_fails=False,
        brain_can_submit=True,
        near_pass=False,
        should_optimize=False,
        score_optimize_threshold=0.3,
    )
    defaults.update(overrides)
    return route_alpha_action(**defaults)


# ---------------------------------------------------------------------------
# RoutingDecision shape
# ---------------------------------------------------------------------------

class TestRoutingDecisionShape:
    def test_fields_present(self):
        d = _call()
        assert hasattr(d, "status")
        assert hasattr(d, "reason")
        assert hasattr(d, "band")

    def test_status_is_string(self):
        d = _call()
        assert isinstance(d.status, str)

    def test_valid_statuses(self):
        valid = {"PASS", "PASS_PROVISIONAL", "OPTIMIZE", "FAIL"}
        # Exercise all four bands
        for d in [
            _call(hard_gate_pass=True, meets_thresholds=True),     # A-pass → PASS
            _call(near_pass=True),                                  # B → PROV
            _call(should_optimize=True, score=0.5),                 # C → OPTIMIZE
            _call(),                                                 # D → FAIL
        ]:
            assert d.status in valid


# ---------------------------------------------------------------------------
# Band D — FAIL baseline (all gates off)
# ---------------------------------------------------------------------------

class TestBandD:
    def test_all_false_is_fail(self):
        d = _call()
        assert d.status == "FAIL"
        assert d.reason == "below_all_bands"
        assert d.band == "D"

    def test_score_below_optimize_threshold_is_fail(self):
        d = _call(should_optimize=True, score=0.1, score_optimize_threshold=0.3)
        assert d.status == "FAIL"

    def test_hard_gate_pass_but_score_below_threshold_not_near_pass(self):
        # hard_gate_pass=True but score < 0.8 and not near_pass → FAIL
        d = _call(hard_gate_pass=True, meets_thresholds=False, score=0.5)
        assert d.status == "FAIL"


# ---------------------------------------------------------------------------
# Band C — OPTIMIZE
# ---------------------------------------------------------------------------

class TestBandC:
    def test_optimize_when_should_optimize_and_score_meets_threshold(self):
        d = _call(should_optimize=True, score=0.5)
        assert d.status == "OPTIMIZE"
        assert d.reason == "should_optimize"
        assert d.band == "C"

    def test_optimize_at_exact_threshold(self):
        d = _call(should_optimize=True, score=0.3, score_optimize_threshold=0.3)
        assert d.status == "OPTIMIZE"

    def test_near_pass_takes_priority_over_optimize(self):
        # Band B check happens before Band C
        d = _call(near_pass=True, should_optimize=True, score=0.5)
        assert d.status == "PASS_PROVISIONAL"
        assert d.reason == "near_pass"


# ---------------------------------------------------------------------------
# Band B — PASS_PROVISIONAL via near_pass
# ---------------------------------------------------------------------------

class TestBandB:
    def test_near_pass_gives_provisional(self):
        d = _call(near_pass=True)
        assert d.status == "PASS_PROVISIONAL"
        assert d.reason == "near_pass"
        assert d.band == "B"

    def test_hard_gate_pass_takes_priority_over_near_pass(self):
        # Band A check happens before Band B
        d = _call(hard_gate_pass=True, meets_thresholds=True, near_pass=True)
        assert d.band.startswith("A")


# ---------------------------------------------------------------------------
# Band A — clean PASS
# ---------------------------------------------------------------------------

class TestBandAPass:
    def test_hard_gate_meets_thresholds_gives_pass(self):
        d = _call(hard_gate_pass=True, meets_thresholds=True)
        assert d.status == "PASS"
        assert d.reason == "hard_gate_pass"
        assert d.band == "A-pass"

    def test_hard_gate_score_above_threshold_gives_pass(self):
        d = _call(hard_gate_pass=True, meets_thresholds=False, score=0.9)
        assert d.status == "PASS"

    def test_hard_gate_score_exactly_at_threshold_gives_pass(self):
        d = _call(hard_gate_pass=True, meets_thresholds=False, score=0.8, score_pass_threshold=0.8)
        assert d.status == "PASS"

    def test_hard_gate_false_score_above_threshold_is_not_band_a(self):
        # Band A requires BOTH hard_gate_pass=True AND (meets_thresholds OR score>=thr)
        d = _call(hard_gate_pass=False, meets_thresholds=False, score=0.95)
        assert d.status != "PASS"


# ---------------------------------------------------------------------------
# Band A sub-cases — PASS_PROVISIONAL downgrades
# ---------------------------------------------------------------------------

class TestBandAV16HardFlags:
    def test_v16_hard_flags_downgrade_to_provisional(self):
        d = _call(hard_gate_pass=True, meets_thresholds=True, has_v16_hard_flags=True)
        assert d.status == "PASS_PROVISIONAL"
        assert d.reason == "v16_hard_flags"
        assert d.band == "A-v16"

    def test_v16_hard_flags_checked_before_brain_unverified(self):
        # v16 sub-case takes priority
        d = _call(
            hard_gate_pass=True, meets_thresholds=True,
            has_v16_hard_flags=True,
            brain_checks_present=False,
        )
        assert d.reason == "v16_hard_flags"


class TestBandABrainUnverified:
    def test_brain_checks_absent_downgrades_to_provisional(self):
        d = _call(hard_gate_pass=True, meets_thresholds=True, brain_checks_present=False)
        assert d.status == "PASS_PROVISIONAL"
        assert d.reason == "brain_checks_unverified"
        assert d.band == "A-unverified"

    def test_brain_checks_present_does_not_trigger_unverified(self):
        d = _call(hard_gate_pass=True, meets_thresholds=True, brain_checks_present=True)
        assert d.reason != "brain_checks_unverified"


class TestBandABrainActionable:
    def test_actionable_fails_not_submittable_downgrades(self):
        d = _call(
            hard_gate_pass=True, meets_thresholds=True,
            brain_actionable_fails=True, brain_can_submit=False,
        )
        assert d.status == "PASS_PROVISIONAL"
        assert d.reason == "brain_actionable_fails"
        assert d.band == "A-brain"

    def test_actionable_fails_but_can_submit_gives_pass(self):
        # If BRAIN can_submit, the actionable fails are non-blocking
        d = _call(
            hard_gate_pass=True, meets_thresholds=True,
            brain_actionable_fails=True, brain_can_submit=True,
        )
        assert d.status == "PASS"

    def test_no_actionable_fails_gives_pass(self):
        d = _call(
            hard_gate_pass=True, meets_thresholds=True,
            brain_actionable_fails=False, brain_can_submit=False,
        )
        assert d.status == "PASS"


# ---------------------------------------------------------------------------
# Band A sub-case priority order
# ---------------------------------------------------------------------------

class TestBandAPriority:
    """v16 > brain_unverified > brain_actionable > pass."""

    def test_v16_beats_brain_actionable(self):
        d = _call(
            hard_gate_pass=True, meets_thresholds=True,
            has_v16_hard_flags=True,
            brain_actionable_fails=True, brain_can_submit=False,
        )
        assert d.reason == "v16_hard_flags"

    def test_brain_unverified_beats_brain_actionable(self):
        d = _call(
            hard_gate_pass=True, meets_thresholds=True,
            brain_checks_present=False,
            brain_actionable_fails=True, brain_can_submit=False,
        )
        assert d.reason == "brain_checks_unverified"


# ---------------------------------------------------------------------------
# Tier-aware threshold wiring (behaviour contract)
# ---------------------------------------------------------------------------

class TestTierAwareThresholds:
    """Verify that score_pass_threshold and score_optimize_threshold are
    respected — the actual values come from config.EVAL_SCORE_PASS /
    EVAL_SCORE_OPTIMIZE, but the routing function only cares about the
    passed floats."""

    def test_custom_pass_threshold(self):
        # With thr=0.6, a score=0.65 should PASS
        d = _call(hard_gate_pass=True, meets_thresholds=False, score=0.65, score_pass_threshold=0.6)
        assert d.status == "PASS"

    def test_custom_pass_threshold_just_below(self):
        d = _call(hard_gate_pass=True, meets_thresholds=False, score=0.59, score_pass_threshold=0.6)
        assert d.status != "PASS"

    def test_custom_optimize_threshold(self):
        d = _call(should_optimize=True, score=0.2, score_optimize_threshold=0.15)
        assert d.status == "OPTIMIZE"

    def test_default_thresholds_match_globals(self):
        # Legacy defaults: score_pass=0.8, score_optimize=0.3
        # score=0.8 → PASS (at exact threshold)
        d = _call(hard_gate_pass=True, meets_thresholds=False, score=0.8, score_pass_threshold=0.8)
        assert d.status == "PASS"
        # score=0.3 → OPTIMIZE (at exact threshold)
        d2 = _call(should_optimize=True, score=0.3, score_optimize_threshold=0.3)
        assert d2.status == "OPTIMIZE"
