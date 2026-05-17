"""T1 LLM-guided generation nodes (PR2).

Each T1 round runs:
  node_t1_strategy_select → 1 LLM call → T1Strategy stored on state.current_strategy
  node_t1_expand          → 0 LLM calls → fields × ops × windows enumerated into
                            state.pending_alphas

Replaces the legacy HYPOTHESIS + CODE_GEN pair when settings.T1_USE_LLM_GUIDED_STRATEGY
is True. Toggle to False to fall back.
"""
from __future__ import annotations

import random
import time
from typing import Dict

from langchain_core.runnables import RunnableConfig
from loguru import logger

from backend.agents.graph.nodes.base import record_trace, resolve_db
from backend.agents.graph.state import AlphaCandidate, MiningState
from backend.agents.services.llm_service import get_llm_service
from backend.config import settings
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

    # Plan v5+ §Phase 1 C-architecture v2 (2026-05-04): instead of relying
    # on hypothesis node having pre-cached current_hypothesis_fields into
    # state (LangGraph state merge for List[Dict] proved unreliable in
    # spike v3 — strategy_select kept seeing anchor-only 30 fields even
    # after hypothesis chose ["option8", "pv13"]), fetch union here from
    # current_hypothesis_datasets directly.
    chosen_dsets = list(getattr(state, "current_hypothesis_datasets", []) or [])
    hypothesis_fields = list(getattr(state, "current_hypothesis_fields", []) or [])

    if (not hypothesis_fields) and chosen_dsets and (
        len(chosen_dsets) > 1 or chosen_dsets[0] != state.dataset_id
    ):
        # Pre-cache miss → fetch union ourselves
        try:
            from backend.tasks.mining_tasks import _get_dataset_fields
            seen_ids: set = set()
            unioned: list = []
            # V-27.D: pure read — reuse the workflow-injected db_session.
            async with resolve_db(config) as _db:
                for ds in chosen_dsets:
                    try:
                        ds_fields = await _get_dataset_fields(_db, ds, state.region, state.universe)
                    except Exception as _e:
                        logger.warning(f"[STRATEGY_SELECT] union fetch {ds} failed: {_e}")
                        continue
                    for f in ds_fields or []:
                        fid = f.get("field_id") or f.get("id")
                        if fid and fid not in seen_ids:
                            seen_ids.add(fid)
                            unioned.append(f)
            hypothesis_fields = unioned[:80]
            logger.info(
                f"[STRATEGY_SELECT] Phase 1 union (fetched here) | "
                f"datasets={chosen_dsets} unique_fields={len(hypothesis_fields)}"
            )
        except Exception as _ex:
            logger.warning(f"[STRATEGY_SELECT] union fetch failed (non-fatal): {_ex}")

    effective_fields = hypothesis_fields if hypothesis_fields else state.fields
    if hypothesis_fields:
        logger.info(
            f"[STRATEGY_SELECT] Phase 1 effective fields | "
            f"datasets={chosen_dsets} effective={len(effective_fields)} "
            f"(vs anchor {len(state.fields)})"
        )

    # L1 ε-greedy explore (2026-05-11): per-round coin flip — with
    # probability EXPLORE_BUDGET_PCT, run this round in EXPLORE mode
    # (RAG examples hidden, prompt prepended with novelty directive).
    explore_mode = (
        random.random() < float(getattr(settings, "EXPLORE_BUDGET_PCT", 0.3) or 0.3)
    )
    if explore_mode:
        logger.info(
            f"[STRATEGY_SELECT] L1 EXPLORE round (ε-greedy fired) — "
            f"hiding {len(state.patterns or [])} success_patterns"
        )

    llm_service = get_llm_service()
    strategy = await select_t1_strategy_via_llm(
        dataset_id=state.dataset_id,
        region=state.region,
        available_fields=effective_fields,
        success_patterns=state.patterns,
        llm_service=llm_service,
        last_round_feedback=last_round,
        # D2: pass chosen_datasets so the prompt's "MUST sample from EACH"
        # rule has named targets. Empty when not Phase 1 → legacy behavior.
        selected_datasets=chosen_dsets if (chosen_dsets and len(chosen_dsets) > 1) else None,
        # L1 Anti-collapse: forward dedup blacklist accumulated this run.
        dedup_skeletons=list(state.recent_dedup_skeletons or []),
        explore_mode=explore_mode,
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
                # L1 ε-greedy observability: surface fire rate via SQL.
                "explore_mode": explore_mode,
                "dedup_blacklist_size": len(state.recent_dedup_skeletons or []),
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

    # R3/Q8 (Phase 1, 2026-05-17 hotfix): T1 tier-1 tasks bypass node_code_gen
    # (pure-code rule-based expansion, no LLM) so the ast_distance hook wired
    # there never fires. Wire it here too so T1 tasks (90% of production
    # workload per pre-discovery snapshot) contribute to ast_distance_log
    # accumulation. Soft-fail, never blocks expansion.
    try:
        from backend.ast_distance_logger import log_round_ast_distances
        task_id = getattr(state, "task_id", None)
        round_idx = getattr(state, "current_iteration", None) or getattr(state, "current_round", None)
        new_exprs = [a.expression for a in pending if getattr(a, "expression", None)]
        await log_round_ast_distances(task_id, round_idx, new_exprs)
    except Exception as e:
        logger.debug(f"[{node_name}] R3/Q8 ast_distance log skip (non-fatal): {e}")

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
