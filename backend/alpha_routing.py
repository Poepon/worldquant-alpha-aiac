"""
Alpha Routing — deterministic band-to-status mapping, no side effects.

Extracted from node_evaluate (evaluation.py L1067–1195) so the routing
policy is readable, auditable, and unit-testable independently of the
evaluation harness.

Band table (checked in order):

  Band  Condition                                          Status            Reason
  ----  -------------------------------------------------  ----------------  -------------------------
  A     hard_gate_pass AND                                 PASS_PROVISIONAL  v16_hard_flags
        (meets_thresholds OR score >= score_pass_thr)      PASS_PROVISIONAL  brain_checks_unverified
          sub-case: has V-16 hard flags                    PASS_PROVISIONAL  brain_actionable_fails
          sub-case: BRAIN returned no check_details        PASS              hard_gate_pass
          sub-case: BRAIN actionable fails, not submittable
          sub-case: none of the above
  B     near_pass                                          PASS_PROVISIONAL  near_pass
  C     should_optimize AND score >= score_optimize_thr   OPTIMIZE          should_optimize
  D     (all else)                                         FAIL              below_all_bands

All inputs are pure values (no DB, no I/O).  Side-effect annotation
(writing alpha.metrics, emitting log lines) remains in node_evaluate.

Semantic contracts (V-16 / V-26.21 / V-27.78 / Fix-C):
  v16_hard_flags        — hard suspicion flag present (sharpe > 3.0 audit)
  brain_checks_unverified — V-27.78: BRAIN returned empty check_details;
                            gate cannot be verified
  brain_actionable_fails  — Fix-C / V-26.21: BRAIN rejected on actionable
                            checks and alpha is not submittable
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RoutingDecision:
    status: str   # PASS | PASS_PROVISIONAL | OPTIMIZE | FAIL
    reason: str   # which rule fired — used for side-effect dispatch + trace
    band: str     # human-readable band label for auditing


def route_alpha_action(
    *,
    hard_gate_pass: bool,
    meets_thresholds: bool,
    score: float,
    score_pass_threshold: float,
    has_v16_hard_flags: bool,
    brain_checks_present: bool,
    brain_actionable_fails: bool,
    brain_can_submit: bool,
    near_pass: bool,
    should_optimize: bool,
    score_optimize_threshold: float,
) -> RoutingDecision:
    """Map evaluation inputs to a routing decision.

    Faithful 1:1 extraction of the decision tree that lived in
    evaluation.py L1067–1195.  V-16 / V-26.21 / V-27.78 / Fix-C
    semantics are preserved verbatim.  Default config values keep
    behaviour identical to the pre-refactor globals (0.8 / 0.3).

    Parameters
    ----------
    hard_gate_pass:
        True when all hard metric gates pass (sharpe/fitness/turnover/
        sub_universe/concentrated_ok/self_corr/IS-OS consistency).
    meets_thresholds:
        True when BRAIN /check says can_submit or no failed_checks.
    score:
        Composite alpha score from calculate_alpha_score().
    score_pass_threshold:
        Minimum composite score for Band A entry (tier-aware default 0.8).
    has_v16_hard_flags:
        True when _run_suspicion_checks() found at least one "hard" flag.
    brain_checks_present:
        True when brain_eval['check_details'] is non-empty.
    brain_actionable_fails:
        True when brain_failed_checks contains at least one actionable name.
    brain_can_submit:
        True when brain_eval['can_submit'] is True.
    near_pass:
        True when alpha meets PROVISIONAL thresholds (looser than PASS).
    should_optimize:
        True when the optimization chain considers this alpha optimizable.
    score_optimize_threshold:
        Minimum composite score for Band C (tier-aware default 0.3).
    """
    # ── Band A ─────────────────────────────────────────────────────────
    if hard_gate_pass and (meets_thresholds or score >= score_pass_threshold):
        if has_v16_hard_flags:
            # V-16: hard suspicion → hold for review rather than entering KB
            return RoutingDecision("PASS_PROVISIONAL", "v16_hard_flags", "A-v16")
        if not brain_checks_present:
            # V-27.78: BRAIN returned no checks (session expiry / 5xx) →
            # gate unverified; mirror the unverified-self_corr path
            return RoutingDecision("PASS_PROVISIONAL", "brain_checks_unverified", "A-unverified")
        if brain_actionable_fails and not brain_can_submit:
            # Fix-C / V-26.21: BRAIN rejected on actionable checks → route
            # into optimization chain so wrapper/window optimizations can
            # push fitness over BRAIN's submission bar
            return RoutingDecision("PASS_PROVISIONAL", "brain_actionable_fails", "A-brain")
        return RoutingDecision("PASS", "hard_gate_pass", "A-pass")

    # ── Band B ─────────────────────────────────────────────────────────
    if near_pass:
        # Near-pass pool: KB learning seeds + island GA; not submission-ready
        return RoutingDecision("PASS_PROVISIONAL", "near_pass", "B")

    # ── Band C ─────────────────────────────────────────────────────────
    if should_optimize and score >= score_optimize_threshold:
        return RoutingDecision("OPTIMIZE", "should_optimize", "C")

    # ── Band D ─────────────────────────────────────────────────────────
    return RoutingDecision("FAIL", "below_all_bands", "D")
