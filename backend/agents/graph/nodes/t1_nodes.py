"""T1 LLM-guided generation nodes (PR2).

Each T1 round runs:
  node_t1_strategy_select → 1 LLM call → T1Strategy stored on state.current_strategy
  node_t1_expand          → 0 LLM calls → fields × ops × windows enumerated into
                            state.pending_alphas

Replaces the legacy HYPOTHESIS + CODE_GEN pair when settings.T1_USE_LLM_GUIDED_STRATEGY
is True. Toggle to False to fall back.
"""
from __future__ import annotations

import time
from typing import Dict

from langchain_core.runnables import RunnableConfig
from loguru import logger

from backend.agents.graph.nodes.base import record_trace
from backend.agents.graph.state import AlphaCandidate, MiningState
from backend.agents.services.llm_service import get_llm_service
from backend.factor_generation import (
    DEFAULT_T1_STRATEGY,
    T1Strategy,
    expand_t1_strategy,
    select_t1_strategy_via_llm,
)


async def node_t1_strategy_select(
    state: MiningState, config: RunnableConfig = None
) -> Dict:
    """LLM picks T1 strategy for this round (fields + ops + window scale).

    Reads:
        state.dataset_id, state.region, state.fields, state.patterns
        state.round_history[-1] (when round > 1, used as feedback)

    Writes:
        state.current_strategy (T1Strategy.model_dump())
        state.trace_steps += STRATEGY_SELECT entry
    """
    node_name = "STRATEGY_SELECT"
    trace_service = config.get("configurable", {}).get("trace_service") if config else None

    start = time.time()
    last_round = state.round_history[-1] if state.round_history else None

    llm_service = get_llm_service()
    strategy = await select_t1_strategy_via_llm(
        dataset_id=state.dataset_id,
        region=state.region,
        available_fields=state.fields,
        success_patterns=state.patterns,
        llm_service=llm_service,
        last_round_feedback=last_round,
    )

    duration_ms = int((time.time() - start) * 1000)
    logger.info(
        f"[{node_name}] T1 round={state.current_round} | "
        f"velocity={strategy.signal_velocity} window={strategy.window_scale} "
        f"fields={len(strategy.promising_fields)} ops={len(strategy.preferred_ts_ops)} "
        f"duration={duration_ms}ms"
    )

    if trace_service:
        await record_trace(
            state, trace_service, node_name,
            input_data={
                "round": state.current_round,
                "fields_pool_size": len(state.fields),
                "patterns_in_pool": len(state.patterns),
            },
            output_data={
                "economic_hypothesis": strategy.economic_hypothesis,
                "signal_velocity": strategy.signal_velocity,
                "window_scale": strategy.window_scale,
                "n_promising_fields": len(strategy.promising_fields),
                "preferred_ts_ops": strategy.preferred_ts_ops,
                "rationale": strategy.rationale,
            },
            duration_ms=duration_ms,
            status="SUCCESS",
        )

    return {"current_strategy": strategy.model_dump()}


async def node_t1_expand(
    state: MiningState, config: RunnableConfig = None
) -> Dict:
    """Enumerate fields × ops × windows from current_strategy. Pure code, no LLM.

    Reads:
        state.current_strategy, state.num_alphas_target, state.region

    Writes:
        state.pending_alphas — daily_goal × 1.5 AlphaCandidate rows
        state.current_alpha_index = 0
        state.trace_steps += TIER_WRAP entry
    """
    node_name = "TIER_WRAP"
    trace_service = config.get("configurable", {}).get("trace_service") if config else None

    start = time.time()

    if not state.current_strategy:
        logger.warning(f"[{node_name}] no current_strategy — falling back to DEFAULT")
        strategy = DEFAULT_T1_STRATEGY
    else:
        try:
            strategy = T1Strategy(**state.current_strategy)
        except Exception as e:
            logger.warning(f"[{node_name}] failed to deserialize strategy: {e}")
            strategy = DEFAULT_T1_STRATEGY

    candidates = expand_t1_strategy(
        strategy=strategy,
        daily_goal=state.num_alphas_target,
        region=state.region,
    )

    pending = [
        AlphaCandidate(
            expression=c["expression"],
            hypothesis=strategy.economic_hypothesis,
            explanation=strategy.rationale,
            metadata={
                "field": c.get("field"),
                "op": c.get("op"),
                "window": c.get("window"),
                "round": state.current_round,
            },
        )
        for c in candidates
    ]

    duration_ms = int((time.time() - start) * 1000)
    logger.info(
        f"[{node_name}] T1 expand | candidates={len(pending)} (target={state.num_alphas_target * 1.5:.0f})"
    )

    if trace_service:
        await record_trace(
            state, trace_service, node_name,
            input_data={
                "round": state.current_round,
                "daily_goal": state.num_alphas_target,
            },
            output_data={
                "candidates_total": len(pending),
                "first_n_expressions": [p.expression for p in pending[:5]],
            },
            duration_ms=duration_ms,
            status="SUCCESS",
        )

    return {
        "pending_alphas": pending,
        "current_alpha_index": 0,
    }
