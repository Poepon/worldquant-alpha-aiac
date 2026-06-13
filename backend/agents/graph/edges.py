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
