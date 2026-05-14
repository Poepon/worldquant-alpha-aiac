"""
Round-level early-stop policy (Optuna MedianPruner-inspired).

Why not pull Optuna in: Optuna prunes per-trial-step, our unit is a
mining round (N alphas). The principle is the same — "worse than median
peer at the same checkpoint" — implemented in 50 LOC against
state.round_history.

Plan §"Week 1 — Attribution + Early-stop". Edge cases handled:
- warmup=5 (R3 #3): require ≥5 rounds before any pruning
- max_iter/2 floor (R3 #3): also require we're at least halfway through
  max_iterations to avoid early-execution noise
- best_sharpe non-improvement: terminal condition combining recent vs
  initial best; if last 2 rounds didn't beat the first 3, signal stop
"""

from __future__ import annotations

from statistics import median
from typing import Dict, List, Optional, Tuple

from loguru import logger

from backend.config import settings as _settings


# V-26.95 (2026-05-13): WARMUP_ROUNDS / PASS_RATE_DROP_RATIO sourced from
# settings. Module-level constants kept as aliases for tests / scripts that
# import them by name.
WARMUP_ROUNDS = _settings.EARLY_STOP_WARMUP_ROUNDS
PASS_RATE_DROP_RATIO = _settings.EARLY_STOP_PASS_RATE_DROP_RATIO


def should_stop_early(
    round_history: List[Dict],
    max_iterations: int,
) -> Tuple[bool, Optional[str]]:
    """Decide whether the mining loop should be terminated before
    `max_iterations` is reached.

    Args:
        round_history: per-round summary list. Each entry must contain at
            least `pass_rate` (float in [0,1]) and `best_sharpe` (float).
        max_iterations: the loop's configured iteration cap.

    Returns:
        (should_stop, reason). reason is None when not stopping, otherwise
        a short human-readable string suitable for logging / DB storage.
    """
    n = len(round_history)
    if n < WARMUP_ROUNDS:
        return False, None

    # R3 #3 secondary guard: only allow stopping past the halfway mark
    # of the configured budget. Prevents noisy early termination when
    # max_iterations is small.
    if max_iterations and n < max_iterations / 2:
        return False, None

    pass_rates = [r.get("pass_rate", 0.0) or 0.0 for r in round_history]
    best_sharpes = [r.get("best_sharpe", 0.0) or 0.0 for r in round_history]

    # Median pruner (current round vs historical median peer)
    current_pr = pass_rates[-1]
    historical_pr = pass_rates[:-1]
    if historical_pr:
        median_pr = median(historical_pr)
        threshold = median_pr * PASS_RATE_DROP_RATIO
        if current_pr < threshold:
            return True, (
                f"pass_rate {current_pr:.3f} below {PASS_RATE_DROP_RATIO}x "
                f"historical median {median_pr:.3f}"
            )

    # Stagnation: last 2 rounds did not improve over the first 3 rounds'
    # best. This catches "stuck on a plateau" cases.
    if len(best_sharpes) >= 5:
        early_best = max(best_sharpes[:3])
        recent_best = max(best_sharpes[-2:])
        if recent_best <= early_best:
            return True, (
                f"best_sharpe stagnant: recent={recent_best:.3f} ≤ "
                f"early={early_best:.3f}"
            )

    return False, None


def classify_attribution(
    *,
    alpha_count: int,
    pass_count: int,
    syntax_fail_count: int,
    simulate_fail_count: int,
    quality_fail_count: int,
) -> str:
    """Heuristic round-level attribution for one hypothesis.

    Plan v5+ §B5 — distinguish "the hypothesis is wrong" from "the LLM
    rendered bad code that ran fine but didn't pass quality gates" so we
    don't abandon a perfectly good hypothesis just because the early code
    samples were buggy.

    Returns one of: HYPOTHESIS / IMPLEMENTATION / BOTH / UNKNOWN
    (matching backend.agents.core.feedback.AttributionType .value strings).

    Decision tree:
      - alpha_count == 0                              → UNKNOWN (no signal)
      - pass_count >= 1                                → UNKNOWN (success — no need)
      - syntax+simulate fails dominate (≥75% of FAIL)  → IMPLEMENTATION
      - quality fails dominate (≥75% of FAIL)          → HYPOTHESIS
      - mixed                                          → BOTH
    """
    if alpha_count == 0:
        return "unknown"
    if pass_count > 0:
        # B5 promotes via mark_promoted directly; no abandonment-relevant
        # attribution needed.
        return "unknown"
    total_fail = max(1, syntax_fail_count + simulate_fail_count + quality_fail_count)
    impl_share = (syntax_fail_count + simulate_fail_count) / total_fail
    qual_share = quality_fail_count / total_fail
    # V-26.94 (2026-05-13): dominance thresholds sourced from settings.
    if impl_share >= _settings.ATTRIBUTION_IMPL_DOMINANCE_THRESHOLD:
        return "implementation"
    if qual_share >= _settings.ATTRIBUTION_QUALITY_DOMINANCE_THRESHOLD:
        return "hypothesis"
    return "both"


# B6 — Hypothesis-level abandonment. After N consecutive rounds with 0 PASS
# AND attribution=HYPOTHESIS, we abandon the hypothesis. Implementation
# failures don't count — those mean the LLM wrote bad code, not that the
# hypothesis is wrong.
HYPOTHESIS_ABANDON_ROUNDS = 3


def should_abandon_hypothesis(
    history_for_hid: List[Dict],
    *,
    n_rounds: int = HYPOTHESIS_ABANDON_ROUNDS,
    hypothesis_id: Optional[int] = None,
) -> Tuple[bool, Optional[str]]:
    """Decide whether one specific hypothesis should be abandoned.

    Args:
        history_for_hid: list of round summaries for this single hypothesis,
            in chronological order. Each entry must contain `pass_count` and
            `attribution`. Pass `state.hypothesis_round_history.get(hid, [])`.
        n_rounds: number of consecutive HYPOTHESIS-attribution rounds with
            0 PASS required to trigger. Default 3 per Plan §B6.
        hypothesis_id: optional id used purely for diagnostic logging so
            cross-task analytics can join hid → outcome path. Not used in
            the decision itself.

    Returns:
        (should_abandon, reason).

    V-24.A diagnostic logging (2026-05-13): every non-trivial path emits a
    structured log so scripts/abandon_path_audit.py can answer "why is the
    ABANDONED column 0?" without re-running the workflow. Three log levels:

    - history_len < n_rounds  → TRACE (still accumulating, normal)
    - history_len >= n_rounds → DEBUG with attribution distribution
    - decision=True            → INFO so it's visible in default log level
    """
    n_history = len(history_for_hid)
    if n_history < n_rounds:
        # Still accumulating; not worth logging unless we're close.
        if n_history == n_rounds - 1:
            logger.debug(
                f"[B6 abandon-check] hid={hypothesis_id} "
                f"history={n_history}/{n_rounds} (one round from threshold)"
            )
        return False, None

    last_n = history_for_hid[-n_rounds:]
    pass_counts = [(e.get("pass_count", 0) or 0) for e in last_n]
    alpha_counts = [(e.get("alpha_count", 0) or 0) for e in last_n]
    attrs = [e.get("attribution") for e in last_n]
    rounds_str = ",".join(str(e.get("round_index", "?")) for e in last_n)
    has_any_pass = any(p > 0 for p in pass_counts)
    has_non_hypothesis_attr = any(a != "hypothesis" for a in attrs)
    # V-27.68: a round that generated 0 alphas never actually *tested* the
    # hypothesis (LLM/codegen hiccup, all-dedup round, …) — counting it as a
    # "0 PASS failure round" would abandon a hypothesis the workflow never
    # gave a fair shot. Pre-fix this was only avoided as a side effect of
    # classify_attribution returning "unknown" on alpha_count==0; make it an
    # explicit guard reading the alpha_count the round entry already carries.
    has_empty_round = any(c == 0 for c in alpha_counts)

    if has_any_pass or has_non_hypothesis_attr or has_empty_round:
        # Window satisfied N but condition didn't fire — log why so
        # abandon_path_audit can quantify the attribution distribution.
        skip_reason = (
            "has_pass" if has_any_pass
            else "non_hypothesis_attr" if has_non_hypothesis_attr
            else "empty_round"
        )
        logger.debug(
            f"[B6 abandon-skip] hid={hypothesis_id} rounds=[{rounds_str}] "
            f"pass_counts={pass_counts} alpha_counts={alpha_counts} "
            f"attrs={attrs} reason={skip_reason}"
        )
        return False, None

    reason = (
        f"{n_rounds} consecutive rounds (rounds {rounds_str}) with "
        f"0 PASS and attribution=HYPOTHESIS — signal direction does "
        f"not survive validation"
    )
    # Visible at default INFO so persistence.py's terminal branch is
    # traceable. V-27.B: G-refine downstream removed — abandon is now
    # always terminal (persistence.py goes straight to mark_abandoned).
    logger.info(
        f"[B6 abandon-trigger] hid={hypothesis_id} rounds=[{rounds_str}] "
        f"reason={reason!r} — hypothesis will be marked ABANDONED"
    )
    return True, reason


def summarise_round(
    pending_alphas: List, pass_count: int, optimize_count: int, fail_count: int
) -> Dict:
    """Build a round_history entry from per-round counts.

    Compatible with the alpha objects passed through MiningState; reads
    sharpe/score from `metrics` dict and `quality_status`.
    """
    total = max(1, len(pending_alphas))
    sharpes: List[float] = []
    scores: List[float] = []
    for a in pending_alphas:
        m = getattr(a, "metrics", None) or {}
        sh = m.get("sharpe")
        if sh is not None:
            try:
                sharpes.append(float(sh))
            except Exception:
                pass
        sc = m.get("score") or m.get("composite_score")
        if sc is not None:
            try:
                scores.append(float(sc))
            except Exception:
                pass
    best_sharpe = max(sharpes) if sharpes else 0.0
    mean_score = sum(scores) / len(scores) if scores else 0.0
    return {
        "alphas_count": len(pending_alphas),
        "pass_count": pass_count,
        "optimize_count": optimize_count,
        "fail_count": fail_count,
        "pass_rate": pass_count / total,
        "best_sharpe": best_sharpe,
        "mean_score": mean_score,
    }
