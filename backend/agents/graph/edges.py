"""
LangGraph Edge Functions
Conditional routing logic for the mining workflow
"""

from typing import Literal
from loguru import logger

from backend.agents.graph.state import MiningState
from backend.config import settings


# =============================================================================
# EDGE: After Validate
# =============================================================================

def route_after_validate(state: MiningState) -> Literal["simulate", "self_correct"]:
    """
    Route after validation step (Batch).
    
    - If ALL valid: proceed to simulate
    - If SOME invalid and retries available: go to self-correct
    - If max retries reached: proceed to simulate (only valid ones will run)
    """
    # Check if any alpha is invalid
    any_invalid = any(not a.is_valid for a in state.pending_alphas)
    
    if not any_invalid:
        logger.debug("[Edge] route_after_validate -> simulate (All Valid)")
        return "simulate"
    
    if state.retry_count < state.max_retries:
        logger.debug(f"[Edge] route_after_validate -> self_correct (retry {state.retry_count + 1}/{state.max_retries})")
        return "self_correct"
    
    logger.debug("[Edge] route_after_validate -> simulate (Max retries, processing valid only)")
    return "simulate"


# =============================================================================
# Phase 3 R1b CoSTEER loop routers (2026-05-18)
# =============================================================================
# Plan: ~/.claude/plans/phase3-r1b-costeer-loop-2026-05-18.md v1.3 §2.3-§2.4.
# Two routers added to the graph after `evaluate`:
#   1. _route_after_evaluate — gate entering the retry sub-graph
#   2. _route_after_r1b_retry — 3-way fork (mutate / retry / give-up)
# Both routers are flag-gated; with all R1b flags OFF they preserve
# byte-equivalent legacy behavior (always 'save_results').


def _route_after_evaluate(
    state: MiningState,
) -> Literal["save_results", "r1b_retry_router"]:
    """R1b.1c — decide whether the evaluate node hands off to the retry
    sub-graph or proceeds to save_results.

    Returns 'r1b_retry_router' when at least one FAIL alpha has an
    actionable attribution (implementation OR hypothesis OR both) AND the
    corresponding R1b flag is ON. Otherwise 'save_results' (legacy).

    UNKNOWN attribution → never triggers retry (per plan §2.3 graceful
    handling: R5 OFF + heuristic UNKNOWN preserves drop-fail behavior).
    """
    retry_on = bool(getattr(settings, "ENABLE_R1B_RETRY_LOOP", False))
    mutate_on = bool(getattr(settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", False))
    if not retry_on and not mutate_on:
        return "save_results"
    for a in getattr(state, "pending_alphas", []) or []:
        if getattr(a, "quality_status", None) != "FAIL":
            continue
        attr = (getattr(a, "metrics", None) or {}).get("_r1a_attribution")
        if attr in ("implementation", "both") and retry_on:
            return "r1b_retry_router"
        if attr in ("hypothesis", "both") and mutate_on:
            return "r1b_retry_router"
    return "save_results"


def _route_after_r1b_retry(
    state: MiningState,
) -> Literal["save_results", "code_gen_retry", "hypothesis_mutate"]:
    """R1b.1c — 3-way fork after the retry router.

    Budget guards (plan §2.4):
      - per-alpha retry counter < R1B_MAX_RETRIES_PER_ALPHA
      - per-cycle mutation counter < R1B_MAX_MUTATIONS_PER_DATASET_CYCLE
      - token cost < R1B_TOKEN_COST_CEILING_USD_PER_ALPHA
    Any guard fails → 'save_results'.

    Per [V1.0-A2-3]: when an alpha has BOTH attribution, HYPOTHESIS_MUTATE
    dominates retry (mutate makes implementation retry stale). In R1b.1c
    the hypothesis_mutate path returns the string 'hypothesis_mutate' so
    workflow.py can wire it once R1b.2 ships; until then operators must
    keep ENABLE_R1B_HYPOTHESIS_MUTATE OFF or the path will route to a
    node that doesn't exist yet (workflow build asserts on that).
    """
    retry_on = bool(getattr(settings, "ENABLE_R1B_RETRY_LOOP", False))
    mutate_on = bool(getattr(settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", False))
    max_retries = int(getattr(settings, "R1B_MAX_RETRIES_PER_ALPHA", 3))
    max_mutations = int(getattr(settings, "R1B_MAX_MUTATIONS_PER_DATASET_CYCLE", 2))
    token_ceiling = float(
        getattr(settings, "R1B_TOKEN_COST_CEILING_USD_PER_ALPHA", 0.05)
    )

    if getattr(state, "r1b_token_cost_this_alpha", 0.0) >= token_ceiling:
        logger.warning(
            f"[Edge] r1b_retry_router -> save_results "
            f"(token ceiling {state.r1b_token_cost_this_alpha:.4f} >= {token_ceiling})"
        )
        return "save_results"

    retries_left = (
        getattr(state, "r1b_retries_attempted_this_alpha", 0) < max_retries
    )
    mutations_left = (
        getattr(state, "r1b_mutations_attempted_this_cycle", 0) < max_mutations
    )

    for a in getattr(state, "pending_alphas", []) or []:
        if getattr(a, "quality_status", None) != "FAIL":
            continue
        attr = (getattr(a, "metrics", None) or {}).get("_r1a_attribution")
        # BOTH → mutate dominates retry per [V1.0-A2-3]
        if attr in ("hypothesis", "both") and mutate_on and mutations_left:
            return "hypothesis_mutate"
        if attr in ("implementation", "both") and retry_on and retries_left:
            return "code_gen_retry"
    return "save_results"


