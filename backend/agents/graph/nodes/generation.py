"""
Generation nodes for LangGraph workflow.

Redesigned based on RD-Agent's hypothesis-driven approach:
- Each experiment tests a specific hypothesis
- Knowledge transfer from previous experiments
- Balanced exploration and exploitation
- No preconceived biases

Contains:
- node_rag_query: Retrieve patterns from knowledge base
- node_distill_context: Distill concepts from fields
- node_hypothesis: Generate investment hypotheses
- node_code_gen: Generate alpha expressions
"""

import json
import time
import random
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
from loguru import logger
from langchain_core.runnables import RunnableConfig

from sqlalchemy import select as _sa_select, func as _sa_func
from backend.config import settings as _gen_settings
from backend.models import Alpha, Hypothesis


# V-26.49 (2026-05-13): proper dataclass for LLM-call failures. Pre-fix used
# `type('obj', (object,), {...})()` inline which is hard to grep for, hard
# to extend (new attrs need both call sites updated), and confuses static
# checkers. Mirrors the live response shape (success / parsed / error) so
# downstream consumers don't need to special-case the failure object.
@dataclass
class _FailedLLMResponse:
    success: bool = False
    parsed: Any = None
    error: str = ""


def _failed_llm_response(error: str) -> _FailedLLMResponse:
    return _FailedLLMResponse(success=False, parsed=None, error=error)

from backend.agents.graph.state import MiningState, AlphaCandidate
from backend.agents.graph.nodes.base import record_trace, _debug_log, resolve_db
from backend.agents.graph.nodes.prompt_enrichers import HypothesisEnricherOrchestrator
from backend.agents.services import LLMService, RAGService
from backend.agents.prompts import (
    ALPHA_GENERATION_SYSTEM,
    HYPOTHESIS_SYSTEM,
    DISTILL_SYSTEM,
    HYPOTHESIS_USER,
    DISTILL_USER,
    build_alpha_generation_prompt,
    build_hypothesis_prompt,
    build_distill_prompt,
    PromptContext,
)


# =============================================================================
# NODE: RAG Query
# =============================================================================

async def node_rag_query(
    state: MiningState,
    rag_service: RAGService,
    config: RunnableConfig = None
) -> Dict:
    """
    Retrieve success patterns and failure pitfalls from knowledge base.
    
    Input State:
        - dataset_id, region
    
    Output Updates:
        - patterns, pitfalls
        - trace_steps
    """
    start_time = time.time()
    node_name = "RAG_QUERY"
    
    logger.info(f"[{node_name}] Starting | task={state.task_id} dataset={state.dataset_id}")
    
    trace_service = config.get("configurable", {}).get("trace_service") if config else None
    
    try:
        # V-26.12: pass current_hypothesis_id (with V-25.C list[0] fallback for
        # LangGraph scalar drops) so the retrieve path soft-prefers same-family
        # SUCCESS_PATTERN / FAILURE_PITFALL rows. None when RAG_QUERY runs
        # before HYPOTHESIS_PROPOSE — the retrieve scoring then ignores the
        # field and falls back to dataset/category matching.
        _hid_for_rag = state.current_hypothesis_id
        if _hid_for_rag is None:
            _hids_for_rag = state.current_hypothesis_ids or []
            if _hids_for_rag:
                _hid_for_rag = _hids_for_rag[0]
        # RAG category-overlap A/B (2026-05-21): per-round arm assignment.
        # TRUE per-round randomization (50/50). The earlier deterministic
        # (task_id+round)%2 was per-task-fixed because current_round is 0 at
        # node_rag_query time in the FLAT loop (each round re-enters the graph
        # fresh) → a whole task stuck on one arm. Random per node_rag_query call
        # guarantees both arms accrue within a single FLAT task. OFF → "" →
        # category always on (current P0 behavior).
        _rag_arm = ""
        try:
            from backend.config import settings as _ab_stg
            if getattr(_ab_stg, "ENABLE_RAG_CATEGORY_AB", False):
                import random as _ab_random
                _rag_arm = "category" if _ab_random.random() < 0.5 else "control"
        except Exception as _ab_e:
            logger.debug(f"[{node_name}] rag_ab_arm assignment skipped: {_ab_e}")
            _rag_arm = ""
        result = await rag_service.query(
            dataset_id=state.dataset_id,
            region=state.region,
            max_patterns=5,
            max_pitfalls=10,
            hypothesis_id=_hid_for_rag,
            # R8 follow-up (2026-05-18): plumb task_id through so the
            # r8_query_log row (when ENABLE_R8_QUERY_LOG ON) is attributable
            # to the originating task instead of always NULL.
            task_id=getattr(state, "task_id", None),
            rag_ab_arm=_rag_arm,
        )
        
        duration_ms = int((time.time() - start_time) * 1000)
        
        _debug_log("C", "nodes.py:rag_query:result", "RAG query complete", {
            "patterns_count": len(result.patterns),
            "pitfalls_count": len(result.pitfalls),
            "duration_ms": duration_ms,
            "dataset_id": state.dataset_id,
        })
        
        logger.info(
            f"[{node_name}] Complete | patterns={len(result.patterns)} pitfalls={len(result.pitfalls)}"
        )
        
        trace_update = await record_trace(
            state, trace_service, step_type=node_name,
            input_data={"dataset_id": state.dataset_id, "region": state.region},
            output_data={
                "patterns_count": len(result.patterns),
                "pitfalls_count": len(result.pitfalls),
                "top_patterns": [p['pattern'] for p in result.patterns[:3]],
                "top_pitfalls": [p['pattern'] for p in result.pitfalls[:3]]
            },
            duration_ms=duration_ms,
            status="SUCCESS"
        )
        
        ds_info = result.dataset_info or {}
        description = ds_info.get("description", "")
        category = ds_info.get("category", "Unknown")
        subcategory = ds_info.get("subcategory", "")
        full_category = f"{category} > {subcategory}" if subcategory else category
        
        return {
            "patterns": result.patterns,
            "pitfalls": result.pitfalls,
            "dataset_description": description,
            "dataset_category": full_category,
            # A/B arm for THIS round → state-merge so evaluation/persistence
            # can stamp it onto alpha.metrics + alpha_failures.
            "rag_ab_arm": _rag_arm,
            **trace_update
        }
        
    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)
        logger.error(f"[{node_name}] Failed | error={e}")
        
        trace_update = await record_trace(
            state, trace_service, node_name, {}, {},
            duration_ms, "FAILED", str(e)
        )
        
        return {
            "patterns": [],
            "pitfalls": [],
            "error": str(e),
            **trace_update
        }


# =============================================================================
# NODE: Distill Context
# =============================================================================

async def node_distill_context(
    state: MiningState,
    llm_service: LLMService,
    config: RunnableConfig = None
) -> Dict:
    """
    Distill relevant concepts/categories from large field sets.
    
    Input State:
        - fields, dataset_description
        
    Output Updates:
        - distilled_concepts
        - focused_fields
        - trace_steps
    """
    start_time = time.time()
    node_name = "DISTILL_CONTEXT"
    
    trace_service = config.get("configurable", {}).get("trace_service") if config else None
    logger.info(f"[{node_name}] Starting | task={state.task_id} fields={len(state.fields)}")
    
    # Group fields by category
    categories = {}
    for f in state.fields:
        cat = f.get("category") or "General"
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(f.get("id", f.get("name")))
    
    # Format for prompt
    categories_text = []
    for cat, f_list in categories.items():
        sample = ", ".join(f_list[:5])
        suffix = f"... ({len(f_list)-5} more)" if len(f_list) > 5 else ""
        categories_text.append(f"- **{cat}**: {sample}{suffix}")
    
    field_categories_str = "\n".join(categories_text)
    
    success_patterns_text = "\n".join([
        f"- {p.get('pattern', '')}" for p in state.patterns[:3]
    ]) or "N/A"
    
    prompt = DISTILL_USER.format(
        dataset_id=state.dataset_id,
        description=state.dataset_description or "N/A",
        category=state.dataset_category or "Unknown",
        success_patterns=success_patterns_text,
        field_categories=field_categories_str
    )
    
    try:
        response = await llm_service.call(
            system_prompt=DISTILL_SYSTEM,
            user_prompt=prompt,
            temperature=0.5,
            json_mode=True,
            node_key="distill_context",
        )
    except Exception as llm_err:
        logger.error(f"[{node_name}] LLM call failed: {llm_err}")
        response = _failed_llm_response(str(llm_err))

    duration_ms = int((time.time() - start_time) * 1000)

    selected_concepts = []
    reasoning = ""
    focused_fields = []
    
    if response.success and response.parsed:
        try:
            parsed = response.parsed
            if isinstance(parsed, dict):
                selected_concepts = parsed.get("selected_concepts", []) or []
                reasoning = parsed.get("reasoning", "") or ""
        except (TypeError, AttributeError) as parse_err:
            logger.error(f"[{node_name}] Parse error: {parse_err}")
    
    if not isinstance(selected_concepts, list):
        selected_concepts = [selected_concepts] if selected_concepts else []
    
    if selected_concepts:
        full_field_list = state.fields
        
        for f in full_field_list:
            f_cat = f.get("category") or "General"
            f_id = str(f.get("id", "")).lower()
            f_name = str(f.get("name", "")).lower()
            
            for c in selected_concepts:
                c_lower = c.lower()
                if c_lower in f_cat.lower() or f_cat.lower() in c_lower:
                    focused_fields.append(f)
                    break
                if c_lower in f_id or c_lower in f_name:
                    focused_fields.append(f)
                    break
    
    if not focused_fields:
        logger.warning(f"[{node_name}] Distillation yielded 0 fields. Falling back to top 30.")
        focused_fields = state.fields[:30]
    
    logger.info(f"[{node_name}] Complete | concepts={selected_concepts} focused={len(focused_fields)}")
    
    trace_update = await record_trace(
        state, trace_service, node_name,
        {"field_count": len(state.fields), "categories": list(categories.keys())},
        {
            "selected_concepts": selected_concepts,
            "focused_count": len(focused_fields),
            "reasoning": reasoning
        },
        duration_ms,
        "SUCCESS" if response.success else "FAILED",
        response.error if hasattr(response, 'error') else None
    )
    
    return {
        "distilled_concepts": selected_concepts,
        "focused_fields": focused_fields,
        **trace_update
    }


# =============================================================================
# NODE: Hypothesis Generation
# =============================================================================

async def _node_hypothesis_inject_consumed(
    *,
    state: MiningState,
    consumed: Dict,
    config: RunnableConfig,
    trace_service: Any,
    start_time: float,
    node_name: str,
) -> Dict:
    """R1b.2-v2 (2026-05-18): construct a 1-element hypotheses list from the
    mutated hypothesis dict consumed at round entry, skipping the LLM call.

    Mirrors node_hypothesis's return shape so the workflow continues
    unchanged. Falls back to LLM via caller's try/except if anything raises.

    The consumed dict may carry partial fields (statement + rationale
    minimally); we fill the rest with conservative defaults that keep
    downstream code_gen / persistence sane.
    """
    statement = str(consumed.get("statement", "")).strip()
    if not statement:
        raise ValueError("consumed hypothesis missing statement")

    legacy_anchor = state.dataset_id
    selected = consumed.get("selected_datasets") or [legacy_anchor]
    if not isinstance(selected, list) or not selected:
        selected = [legacy_anchor]

    hypothesis_dict = {
        "statement": statement,
        "rationale": str(consumed.get("rationale", "")),
        "selected_datasets": selected,
        "key_fields": consumed.get("key_fields") or [],
        "suggested_operators": consumed.get("suggested_operators") or [],
        "pillar": consumed.get("pillar") or "unknown",
        # Marker so attribution / KB writes can tell this round was
        # CoSTEER-mutate-driven rather than fresh exploration.
        "_r1b_origin": "mutate_v2",
    }
    hypotheses = [hypothesis_dict]

    logger.info(
        f"[{node_name}] R1b.2-v2 inject ACTIVE | task={state.task_id} "
        f"skipping LLM | statement={statement[:80]}"
    )

    duration_ms = int((time.time() - start_time) * 1000)
    trace_update = await record_trace(
        state, trace_service, node_name,
        {
            "dataset_id": state.dataset_id,
            "mode": "r1b_mutate_inject_v2",
            "consumed_origin": consumed.get("_r1b_origin", "hypothesis_mutate"),
        },
        {
            "hypotheses_count": 1,
            "hypotheses": hypotheses,
            "knowledge_transfer": {},
            "analysis": {"inject_path": True},
        },
        duration_ms,
        "SUCCESS",
        None,
    )

    # CoSTEER loop-closure fix (2026-05-22): the consumed dict carries the
    # mutated Hypothesis row id (r1b_loop._insert_mutated_hypothesis stamped it
    # into pending["hypothesis_id"] on a successful INSERT). Propagate it as
    # this round's current_hypothesis_id so (1) the alphas generated from the
    # mutated hypothesis link to it (alpha.hypothesis_id = mutated id → the
    # mutation actually drives next-round output, previously 0/10108 referenced
    # IMPROVEMENT_RULE) and (2) when one of these alphas fails and triggers a
    # further mutation, node_hypothesis_mutate finds a real parent → the chain
    # deepens past depth 1 (previously parent_hypothesis_id was always None).
    # Stays None when the parent INSERT had failed (no id) — downstream handles
    # None gracefully exactly as before.
    _consumed_hid = consumed.get("hypothesis_id")
    return {
        "hypotheses": hypotheses,
        "knowledge_transfer": {},
        "current_hypothesis_datasets": selected,
        "current_hypothesis_fields": [],
        "current_hypothesis_id": _consumed_hid,
        "current_hypothesis_ids": [_consumed_hid] if _consumed_hid else [],
        # F4/F5 review fix: explicitly return cleared cognitive_layer_id_used
        # so LangGraph state-merge guarantees the reset takes effect on this
        # inject-path round (the in-place mutation at node_hypothesis entry
        # is defense-in-depth).
        "cognitive_layer_id_used": "",
        **trace_update,
    }


async def node_hypothesis(
    state: MiningState,
    llm_service: LLMService,
    config: RunnableConfig = None
) -> Dict:
    """
    Generate investment hypotheses based on dataset using hypothesis-driven approach.
    
    Redesigned based on RD-Agent principles:
    - Each hypothesis is precise, testable, and focused on a single direction
    - Learns from previous experiment results (feedback loop)
    - Balances exploration and exploitation based on evidence
    - No preconceived biases about what works
    
    Input State:
        - dataset_id, fields, patterns, dataset_description
        - experiment_trace (optional): Previous experiment results for learning
    
    Output Updates:
        - hypotheses
        - knowledge_transfer
        - trace_steps
    """
    start_time = time.time()
    node_name = "HYPOTHESIS"

    trace_service = config.get("configurable", {}).get("trace_service") if config else None
    strategy_dict = config.get("configurable", {}).get("strategy", {}) if config else {}

    # Get experiment trace for learning (if available)
    experiment_trace = strategy_dict.get("experiment_trace", [])
    exploration_weight = strategy_dict.get("exploration_weight", 0.5)

    logger.info(f"[{node_name}] Starting | task={state.task_id} trace_len={len(experiment_trace)}")

    # F4/F5 review fix (Sprint 3 R3): cross-round transient cleanup. Both
    # paths (R1b inject + LLM exploration) must reset state.cognitive_
    # layer_id_used so a stale layer ID from the previous round doesn't
    # get stamped on the new alphas. Mirrors G8's g8_forest_referenced_ids
    # reset at line 788. Done HERE (before the inject early-return) so
    # both branches share the reset.
    state.cognitive_layer_id_used = ""

    # ------------------------------------------------------------------
    # R1b.2-v2 (2026-05-18): inject path — when the prior round's
    # hypothesis_mutate emitted a pending hypothesis (consumed by
    # pipeline round + plumbed via workflow.run), use it directly
    # as the round's primary hypothesis and SKIP the exploration LLM call.
    # Flag-gated by ENABLE_R1B_HYPOTHESIS_MUTATE so consumed-but-flag-OFF is
    # a no-op (legacy LLM-driven path runs).
    # ------------------------------------------------------------------
    _consumed = getattr(state, "r1b_consumed_pending_hypothesis", None)
    if _consumed and isinstance(_consumed, dict) and _consumed.get("statement"):
        try:
            if bool(getattr(_gen_settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", False)):
                return await _node_hypothesis_inject_consumed(
                    state=state,
                    consumed=_consumed,
                    config=config,
                    trace_service=trace_service,
                    start_time=start_time,
                    node_name=node_name,
                )
        except Exception as _inject_ex:
            logger.warning(
                f"[{node_name}] R1b.2-v2 inject path failed, falling back to "
                f"LLM exploration: {_inject_ex}"
            )

    target_fields = state.focused_fields if state.focused_fields else state.fields[:20]

    # ------------------------------------------------------------------
    # Prompt-context enrichment (Phase 1a-E): the 8 former inline nudge blocks
    # (P2-B pillar / P2-D neg-kb / P2-A macro / P2-C style / G8 forest /
    # R8-v3 cognitive / G10 distilled / orthogonality) now live as
    # PromptContextEnricher strategies. The orchestrator runs the enabled ones
    # in source order over ONE shared session (P2-B first -> its pillar_hint
    # feeds G8/R8-v3/G10). Flag-OFF enrichers leave acc fields at their legacy
    # defaults -> byte-for-byte legacy prompt. See nodes/prompt_enrichers.py.
    # ------------------------------------------------------------------
    enrichment = await HypothesisEnricherOrchestrator().run(state, config)
    # State mutations (were set inside the G8 / R8-v3 / G10 blocks). The
    # function-entry reset of state.cognitive_layer_id_used (above, for the R1b
    # inject early-return path) is followed here by an unconditional (re)assign
    # of all three from enrichment -- the defaults ([] / "" / 0) reproduce the
    # former per-round resets; a fired enricher supplies the real value.
    state.g8_forest_referenced_ids = enrichment.g8_referenced_ids
    state.cognitive_layer_id_used = enrichment.cognitive_layer_id_used
    state.g10_injected_entries_n = enrichment.g10_injected_entries_n

    # Pool Phase 2 (R1a-v1): SOFT skeleton-frequency de-prioritization nudge.
    # Flag-gated so the OFF path opens NO DB session → byte-for-byte legacy +
    # zero overhead. Soft-fail: any error leaves the nudge empty (legacy prompt).
    crowded_skeletons_block = ""
    if bool(getattr(_gen_settings, "ENABLE_R1A_KB_SKELETON_FREQUENCY", False)):
        try:
            from backend.agents.prompts.skeleton_frequency import (
                skeleton_frequency_nudge_block,
            )
            async with resolve_db(config) as _sk_db:
                crowded_skeletons_block = await skeleton_frequency_nudge_block(
                    _sk_db, region=state.region, dataset_id=state.dataset_id,
                )
        except Exception as _sk_ex:  # noqa: BLE001 — prompt prior, never fatal
            logger.warning(
                f"[{node_name}] R1a skeleton-freq nudge failed (non-fatal): {_sk_ex}"
            )
            crowded_skeletons_block = ""

    # Build prompt context. Plan v5+ Phase 1: cross-dataset pool is wired
    # through MiningState.available_dataset_pool (populated by mining_tasks
    # when HYPOTHESIS_CENTRIC_LEVEL >= 1; empty otherwise → legacy behavior).
    prompt_context = PromptContext(
        dataset_id=state.dataset_id,
        dataset_description=state.dataset_description or "",
        dataset_category=state.dataset_category or "",
        region=state.region,
        universe=state.universe,
        fields=target_fields,
        # Pass the full operator catalog for parity with node_code_gen. NOTE:
        # build_hypothesis_prompt does not currently render ctx.operators, so
        # this is a no-op for the hypothesis LLM today — kept so a future
        # hypothesis prompt that surfaces operators sees the full set, not [:30].
        # The operator-visibility fix that actually matters is in node_code_gen.
        operators=state.operators,
        success_patterns=state.patterns[:5],
        # P2-D: when flag is on AND we fetched ≥1 pitfall, prepend them to
        # state.pitfalls. When flag is off OR no pitfalls fetched, fall
        # back to state.pitfalls[:5] — byte-for-byte legacy (verified by
        # test_node_hypothesis_negative_knowledge.test_flag_off_byte_for_byte
        # _legacy).
        failure_pitfalls=(
            (enrichment.neg_kb_pitfalls + (state.pitfalls or []))[:5]
            if enrichment.neg_kb_pitfalls
            else state.pitfalls[:5]
        ),
        exploration_weight=exploration_weight,
        available_dataset_pool=getattr(state, "available_dataset_pool", []) or [],
        pillar_hint=enrichment.pillar_hint,
        # Orthogonality Phase A (2026-06-05): None when flag OFF / empty pool →
        # byte-for-byte legacy. render_profile_block ensures "" → None here.
        submitted_pool_profile=(enrichment.orth_steer_block or None),
        # Pool Phase 2 (R1a-v1): crowded-skeleton soft nudge. "" when flag OFF /
        # too few samples → build_hypothesis_prompt splices "" → byte-for-byte legacy.
        crowded_skeletons_block=(crowded_skeletons_block or None),
        # P2-A (2026-05-16): only attach when the flag is ON AND we fetched
        # ≥1 row. Off / fetch-failed paths set macro_narratives=[] so the
        # template splice produces the empty-string byte-for-byte legacy
        # render (field assertion in
        # test_node_hypothesis_macro.test_flag_off_byte_for_byte_legacy, M8).
        macro_narratives=enrichment.macro_narratives,
        # P2-C (2026-05-16): only attach when the flag is ON AND we have
        # a regime injection. Off path: style_preset = None → builder
        # returns "" → byte-for-byte legacy.
        style_preset=enrichment.style_preset,
        # G8 Phase A (2026-05-19): only attach when the flag is ON AND we
        # fetched ≥1 row. Off / fetch-failed paths set cross_task_hypotheses=[]
        # so build_cross_task_hypotheses_block returns "" → template splice
        # produces the empty-string byte-for-byte legacy render.
        cross_task_hypotheses=enrichment.cross_task_hyps,
        # B5 R8-v3 (Sprint 3, 2026-05-20): pre-rendered cognitive-layer
        # block (str). "" = OFF / no layer fired → template splice yields
        # empty (byte-for-byte legacy).
        cognitive_layer_block=enrichment.cognitive_layer_block,
        cognitive_layer_id=enrichment.cognitive_layer_id,
        # A5.2 G10 PR2 (Sprint 4, 2026-05-20): pre-rendered distilled-
        # logic block (str). "" = OFF / no rows → splice yields empty.
        distilled_logic_block=enrichment.distilled_logic_block,
    )
    
    # Use new hypothesis builder with experiment trace
    prompt = build_hypothesis_prompt(prompt_context, experiment_trace)
    
    # Adjust temperature based on exploration weight
    # Higher exploration -> higher temperature for more diverse hypotheses
    temperature = 0.7 + (exploration_weight * 0.3)  # Range: 0.7 - 1.0
    
    # V-27.31: node_distill_context / node_code_gen already wrap their
    # llm_service.call in try/except → _failed_llm_response (V-26.49).
    # node_hypothesis — the most critical of the three generation nodes —
    # was a bare call: an LLM timeout / network blip / JSON-mode error threw
    # straight out of the node, crashing the whole workflow.run(). Degrade
    # gracefully like the sibling nodes.
    try:
        response = await llm_service.call(
            system_prompt=HYPOTHESIS_SYSTEM,
            user_prompt=prompt,
            temperature=temperature,
            json_mode=True,
            node_key="hypothesis",
            # 2026-05-31: explicit budget (was the call() default 4096) so verbose
            # routed models (deepseek-v4-pro) aren't truncated mid-JSON → 0 hyps.
            max_tokens=_gen_settings.HYPOTHESIS_MAX_TOKENS,
        )
    except Exception as llm_err:
        logger.error(f"[{node_name}] LLM call exception: {llm_err}")
        response = _failed_llm_response(str(llm_err))

    duration_ms = int((time.time() - start_time) * 1000)
    
    hypotheses = []
    knowledge_transfer = {}
    analysis = {}
    
    # Plan v5+ Phase 1: aggregate selected_datasets across all hypotheses
    # for code_gen field-pool union. Each hypothesis may pick its own subset;
    # the round's effective dataset set is the union (capped at the available pool).
    chosen_datasets: List[str] = []
    pool_set = set(prompt_context.available_dataset_pool)
    legacy_anchor = state.dataset_id

    if response.success and response.parsed:
        parsed = response.parsed
        hypotheses = parsed.get("hypotheses", [])
        knowledge_transfer = parsed.get("knowledge_transfer", {})
        analysis = parsed.get("analysis", {})

        # Phase 1 selected_datasets parsing:
        #   - Each hypothesis may include "selected_datasets": [...] (1-3 ids)
        #   - Validate against available_dataset_pool (drop any rogue ids)
        #   - Fall back to [anchor] when missing/empty (preserves legacy)
        union_set = set()
        for h in hypotheses:
            sel = h.get("selected_datasets") or []
            if not isinstance(sel, list):
                sel = []
            if pool_set:
                # Pool offered → keep only valid ids; require at least one
                sel = [d for d in sel if d in pool_set]
            if not sel:
                sel = [legacy_anchor]
            h["selected_datasets"] = sel  # write-back normalized
            union_set.update(sel)
        chosen_datasets = sorted(union_set) if union_set else [legacy_anchor]

        # Log extracted knowledge for future reference
        if knowledge_transfer:
            rules = knowledge_transfer.get("if_then_rules", [])
            if rules:
                logger.info(f"[{node_name}] Extracted {len(rules)} knowledge rules")
                for rule in rules[:3]:
                    logger.debug(f"[{node_name}] Rule: {rule}")

    logger.info(
        f"[{node_name}] Complete | hypotheses={len(hypotheses)} "
        f"selected_datasets={chosen_datasets} (pool_size={len(pool_set)})"
    )
    
    trace_update = await record_trace(
        state, trace_service, node_name,
        {
            "dataset_id": state.dataset_id,
            "mode": "hypothesis_driven",
            "exploration_weight": exploration_weight,
            "experiment_trace_length": len(experiment_trace)
        },
        {
            "hypotheses_count": len(hypotheses),
            "hypotheses": hypotheses[:3],
            "knowledge_transfer": knowledge_transfer,
            "analysis": analysis
        },
        duration_ms,
        "SUCCESS" if response.success else "FAILED",
        response.error if hasattr(response, 'error') else None
    )
    
    # Phase 1 (C-architecture): when Phase 1 active and chosen_datasets exceed
    # the legacy anchor, fetch the union field pool now and persist it on
    # state. Downstream t1_strategy_select / node_code_gen read this as
    # effective_fields, which threads cross-dataset awareness through the
    # entire T1 LLM-guided pipeline.
    union_fields: List[Dict] = []
    if chosen_datasets and (len(chosen_datasets) > 1 or chosen_datasets[0] != legacy_anchor):
        try:
            from backend.tasks.fetch_helpers import _get_dataset_fields
            seen_ids: set = set()
            # V-27.D: pure read — reuse the workflow-injected db_session
            # when present instead of always self-opening a connection.
            async with resolve_db(config) as _db:
                for ds in chosen_datasets:
                    try:
                        ds_fields = await _get_dataset_fields(_db, ds, state.region, state.universe)
                    except Exception as _e:
                        logger.warning(f"[{node_name}] union fetch {ds} failed: {_e}")
                        continue
                    for f in ds_fields or []:
                        fid = f.get("field_id") or f.get("id")
                        if fid and fid not in seen_ids:
                            seen_ids.add(fid)
                            union_fields.append(f)
            # Cap at 80 (slightly larger than code_gen's 60 — t1_strategy_select
            # picks 8-12 promising_fields from this pool, so giving it a bit
            # more breadth helps cross-dataset combinations surface).
            union_fields = union_fields[:80]
            logger.info(
                f"[{node_name}] Phase 1 union fields cached | "
                f"datasets={chosen_datasets} unique_fields={len(union_fields)}"
            )
        except Exception as _ex:
            logger.warning(f"[{node_name}] union-field cache failed (non-fatal): {_ex}")
            union_fields = []

    # ------------------------------------------------------------------
    # Phase 2 (B3): typed Hypothesis persistence
    # ------------------------------------------------------------------
    # When hypothesis_centric_level >= 2, persist each LLM-emitted hypothesis
    # as a Hypothesis ORM row BEFORE downstream code_gen runs. This satisfies
    # the time-ordering hard constraint (Plan §A 4 道 post-hoc 防御):
    # hypothesis.created_at < alpha.created_at, audited by
    # scripts/audit_temporal_consistency.py.
    #
    # state.current_hypothesis_id = primary (first) hypothesis row id; alphas
    # downstream link to this. state.current_hypothesis_ids = full list so
    # B5 feedback can update lifecycle on every proposed hypothesis when
    # multiple were emitted in one round.
    current_hypothesis_id: Optional[int] = None
    current_hypothesis_ids: List[int] = []
    cfg = (config.get("configurable", {}) if config else {}) or {}

    # Pool Phase 2 (1a, 2026-06-07): typed Hypothesis persistence is now
    # UNCONDITIONAL — one row per generated hypothesis, regardless of the legacy
    # ``hypothesis_centric_level`` config. The decoupled HG/S/E pool's
    # hg_run_config() drops that key, so it was always 0 → this INSERT was gated
    # OFF → the cognitive spine was DEAD: candidate_queue.current_hypothesis_id
    # stayed NULL (0/2442), the PROPOSED→ACTIVE→PROMOTED lifecycle never advanced.
    # The async cognitive reconcile beat (Track C 1c) now drives that lifecycle
    # from the rows created here.
    #
    # The retired FLAT V-22.13 cross-round reuse block was DELETED: it only ran
    # at hge_level>=2 (never in the pool) and was itself structurally dead even in
    # FLAT (0 hypotheses ever ABANDONED — should_abandon needed ≥3 consecutive
    # round-history entries but each round made a fresh row). Lease-recycle
    # idempotency is now handled by the per-intent dedup (find_open_by_intent)
    # inside the INSERT below, not by cross-round reuse.

    if hypotheses and current_hypothesis_id is None:
        # V-19.7 (2026-05-06) zombie-ACTIVE prevention: persist ONLY the
        # FIRST viable hypothesis (the "primary") instead of N siblings.
        # B4 links every alpha in the round to current_hypothesis_id (the
        # primary), so non-primary siblings would never receive alphas and
        # would stuck in ACTIVE forever (V-19.6 stopped them being falsely
        # PROMOTED but they remained zombie rows). Plan v5 Final §三轮精简
        # cut multi-hypothesis-per-round Layer pool design, so 1-per-round
        # is the cleanest semantic match. Sibling candidates remain in the
        # `hypotheses` list (returned to state for trace step output) so the
        # LLM exploration is not lost — they're just not DB-persisted.
        try:
            from backend.database import AsyncSessionLocal
            from backend.services.hypothesis_service import (
                HypothesisService, HypothesisCreateData,
            )
            from backend.models import HypothesisKind
            experiment_variant = cfg.get("experiment_variant")
            async with AsyncSessionLocal() as _hdb:
                svc = HypothesisService(_hdb)
                primary_h = None
                for h in hypotheses:
                    statement = (h.get("idea") or h.get("statement") or "").strip()
                    if statement:
                        primary_h = h
                        break
                if primary_h is not None:
                    statement = (primary_h.get("idea") or primary_h.get("statement") or "").strip()
                    # P2-B (2026-05-15, M8 fix): resolve pillar BEFORE the
                    # HypothesisCreateData ctor so persistence happens
                    # unconditionally. Decision-injection (the nudge above)
                    # is gated by ENABLE_PILLAR_AWARE_SELECTION, but data
                    # collection (stamp) is always on — otherwise the
                    # pillar_balance_check report has nothing to read.
                    from backend.pillar_classifier import (
                        normalize_pillar as _p2b_norm,
                        infer_pillar as _p2b_infer,
                    )
                    _llm_pillar_raw = primary_h.get("pillar")
                    _llm_pillar = _p2b_norm(_llm_pillar_raw)
                    resolved_pillar = _llm_pillar or _p2b_infer(
                        hypothesis_pillar=_llm_pillar_raw,
                        key_fields=primary_h.get("key_fields") or [],
                        suggested_operators=primary_h.get(
                            "suggested_operators",
                        ) or [],
                        expected_signal=primary_h.get("expected_signal"),
                    )
                    # N2: stamp ``_pillar_nudged`` when the LLM actually
                    # honoured the nudge. Cheap, non-persisted audit hook —
                    # mirrors the existing ``_v22_13_reused`` pattern.
                    if enrichment.pillar_hint and resolved_pillar == enrichment.pillar_hint:
                        primary_h["_pillar_nudged"] = enrichment.pillar_hint
                    # P2-D N4: stamp which signature_keys were shown to
                    # the LLM. Independent of whether LLM acted on them —
                    # used by ops to track nudge surface area / pickup
                    # rate. Non-persisted; lives only on the in-memory
                    # ``hypotheses`` list returned to state.
                    if enrichment.neg_kb_keys_seen:
                        primary_h["_negative_knowledge_pitfalls_seen"] = (
                            list(enrichment.neg_kb_keys_seen)
                        )
                    # P2-A N4: stamp the field_id / dataset_category keys
                    # of macro narratives that were shown to the LLM.
                    # Non-persisted audit hook (same pattern as P2-D N4).
                    if enrichment.macro_keys_seen:
                        primary_h["_macro_narratives_seen"] = (
                            list(enrichment.macro_keys_seen)
                        )
                    # P2-C (2026-05-16) N4 stamp: record the regime label
                    # the LLM actually saw via the style preset block.
                    # Only stamps when both (a) the STYLE flag is on AND
                    # (b) we attached a real preset above. MF4 invariant:
                    # this key MUST NOT appear when flag=False, even if
                    # strategy.regime was set by an effect-flag-only run.
                    if enrichment.style_preset and enrichment.p2c_regime:
                        primary_h["_regime_style_seen"] = enrichment.p2c_regime

                    data = HypothesisCreateData(
                        statement=statement,
                        rationale=primary_h.get("rationale") or primary_h.get("reason") or "",
                        region=state.region,
                        universe=state.universe,
                        kind=HypothesisKind.INVESTMENT_THESIS.value,
                        expected_signal=primary_h.get("expected_signal", "unknown"),
                        confidence=primary_h.get("confidence", "medium"),
                        novelty=primary_h.get("novelty", "established"),
                        key_fields=primary_h.get("key_fields") or [],
                        suggested_operators=primary_h.get("suggested_operators") or [],
                        dataset_pool=primary_h.get("selected_datasets") or [],
                        experiment_variant=str(experiment_variant)
                            if experiment_variant is not None else None,
                        # P2-B (2026-05-15, M8): always stamp the pillar.
                        pillar=resolved_pillar,
                        # Pool 1a: lease-recycle dedup key — the parent intent id
                        # (None for FLAT / non-pool callers → no dedup).
                        hyp_intent_id=getattr(state, "hyp_intent_id", None),
                    )
                    # Pool 1a dedup: a lease-recycled HG re-run on the same intent
                    # reuses the already-open row instead of inserting an orphan
                    # PROPOSED duplicate. Only when an intent id is present (pool);
                    # FLAT/non-pool falls straight through to create.
                    _intent_id = getattr(state, "hyp_intent_id", None)
                    row = None
                    if _intent_id is not None:
                        row = await svc.find_open_by_intent(_intent_id)
                        if row is not None:
                            logger.info(
                                f"[{node_name}] reusing open hypothesis={row.id} "
                                f"for intent={_intent_id} (lease-recycle dedup)"
                            )
                    if row is None:
                        row = await svc.create_hypothesis(data)
                    current_hypothesis_ids.append(row.id)
                    primary_h["hypothesis_id"] = row.id
                    await _hdb.commit()
            if current_hypothesis_ids:
                current_hypothesis_id = current_hypothesis_ids[0]
                logger.info(
                    f"[{node_name}] Phase 2 persisted primary hypothesis="
                    f"{current_hypothesis_id} (sibling candidates retained "
                    f"in trace, not DB-persisted; V-19.7)"
                )
        except Exception as _ex:
            logger.warning(
                f"[{node_name}] Phase 2 hypothesis persist failed (non-fatal): {_ex}"
            )

    # P2-D S8 audit: flag was on AND we showed N pitfalls to the LLM, but
    # the parser came back with no valid hypothesis (LLM-side failure).
    # Surfaces "nudge shown / no LLM signal" cases for ops without blocking
    # the node.
    if enrichment.neg_kb_keys_seen and current_hypothesis_id is None:
        logger.warning(
            f"[{node_name}] P2-D nudge shown ({len(enrichment.neg_kb_keys_seen)} "
            f"pitfalls) but LLM returned no valid hypothesis "
            f"(region={state.region})"
        )

    return {
        "hypotheses": hypotheses,
        "knowledge_transfer": knowledge_transfer,
        # Phase 1: union of selected_datasets across hypotheses; downstream
        # nodes (t1_strategy_select / code_gen) read state.current_hypothesis_fields
        # as effective_fields when non-empty.
        "current_hypothesis_datasets": chosen_datasets,
        "current_hypothesis_fields": union_fields,
        # Phase 2: typed Hypothesis link IDs. None when level<2 / no hypotheses.
        "current_hypothesis_id": current_hypothesis_id,
        "current_hypothesis_ids": current_hypothesis_ids,
        # G8 Phase A follow-up: surface forest reference IDs to state so
        # persistence can stamp them onto alpha.metrics for reverse
        # attribution (empty when flag OFF / no rows).
        "g8_forest_referenced_ids": [
            int(h["hypothesis_id"]) for h in enrichment.cross_task_hyps
            if h.get("hypothesis_id") is not None
        ],
        # F6 review fix (Sprint 3 R3): explicitly return cognitive_layer_
        # id_used so LangGraph state-merge propagates it to downstream
        # nodes (node_evaluate reads it via getattr). In-place mutation
        # at line ~900 is defense-in-depth but the dict-return is the
        # documented LangGraph contract.
        "cognitive_layer_id_used": enrichment.cognitive_layer_id_used,
        **trace_update
    }


# =============================================================================
# NODE: Code Generation
# =============================================================================

async def node_code_gen(
    state: MiningState,
    llm_service: LLMService,
    config: RunnableConfig = None
) -> Dict:
    """
    Generate Alpha expressions using hypothesis-driven approach.
    
    Redesigned based on RD-Agent principles:
    - Each expression tests a specific hypothesis
    - Learns from previous experiment feedback
    - No preconceived biases about operators or patterns
    - Balanced approach between convention and exploration
    
    Input State:
        - dataset_id, fields, operators, patterns, pitfalls
        - hypotheses: Generated hypotheses to implement
        
    Config:
        - strategy: Evolution strategy dict with exploration parameters
        - experiment_feedback: Previous experiment results for learning
        - target_hypothesis: Optional specific hypothesis to implement
    
    Output Updates:
        - pending_alphas
        - trace_steps
    """
    start_time = time.time()
    node_name = "CODE_GEN"
    
    trace_service = config.get("configurable", {}).get("trace_service") if config else None
    strategy_dict = config.get("configurable", {}).get("strategy", {}) if config else {}
    
    # Extract strategy parameters
    temperature = strategy_dict.get("temperature", 0.7)
    exploration_weight = strategy_dict.get("exploration_weight", 0.5)
    preferred_fields = strategy_dict.get("preferred_fields", [])
    avoid_fields = strategy_dict.get("avoid_fields", [])
    focus_hypotheses = strategy_dict.get("focus_hypotheses", [])
    avoid_patterns = strategy_dict.get("avoid_patterns", [])
    
    # New: Get experiment feedback for learning
    experiment_feedback = strategy_dict.get("experiment_feedback", [])
    target_hypothesis = strategy_dict.get("target_hypothesis")
    
    logger.info(
        f"[{node_name}] Starting | task={state.task_id} "
        f"temp={temperature:.2f} explore={exploration_weight:.2f} "
        f"feedback_len={len(experiment_feedback)}"
    )
    
    # W6: rolling few-shot pool — pull last 7d PASS/PROVISIONAL with HITL bias
    # NOTE: no longer injecting submitted-alpha expressions into the prompt
    # (rejected — IP leakage to LLM vendor + does not scale to 1000s of OS
    # alphas). SELF_CORRELATION dedup is enforced post-sim by
    # correlation_service in evaluation.node_evaluate.
    recent_pass_examples = []
    try:
        from backend.agents.services.rag_service import RAGService
        # V-27.D: pure read — reuse the workflow-injected db_session if present.
        async with resolve_db(config) as _db:
            _rag = RAGService(_db)
            recent_pass_examples = await _rag.get_recent_pass_examples(
                region=state.region,
                dataset_id=state.dataset_id,
                limit=5,
            )
    except Exception as _ex:
        logger.warning(f"[{node_name}] few-shot pool fetch failed (non-fatal): {_ex}")

    # Build structured prompt context — merge recent_pass_examples into the
    # success_patterns slot so the existing prompt builder picks them up.
    merged_patterns = list(recent_pass_examples)
    # Keep regular RAG patterns afterwards as backup so total stays moderate
    for p in (state.patterns or [])[:5]:
        if not any(p.get("pattern") == ex.get("pattern") for ex in merged_patterns):
            merged_patterns.append(p)

    # Plan v5+ §Phase 1 (A4 → C-architecture): cross-dataset field union for
    # code generation. node_hypothesis caches the union into
    # state.current_hypothesis_fields when Phase 1 active; here we just
    # consume it. Fall back to state.fields for legacy single-anchor.
    hypothesis_fields = list(getattr(state, "current_hypothesis_fields", []) or [])
    if hypothesis_fields:
        # V-26.46 (2026-05-13): de-dup by field id before the 60-slot trim
        # so the prompt doesn't waste capacity on the same field appearing
        # twice (happened when current_hypothesis_datasets shared fields).
        # Preserves first-seen order so the most-relevant field stays at
        # the head of the trimmed list.
        seen_ids: set = set()
        deduped_fields: list = []
        for f in hypothesis_fields:
            fid = (
                f.get("id") if isinstance(f, dict) else None
            ) or (
                f.get("name") if isinstance(f, dict) else str(f)
            )
            if not fid or fid in seen_ids:
                continue
            seen_ids.add(fid)
            deduped_fields.append(f)
        code_gen_fields = deduped_fields[:60]
        logger.info(
            f"[{node_name}] Phase 1 effective fields | "
            f"datasets={getattr(state, 'current_hypothesis_datasets', [])} "
            f"effective_fields={len(code_gen_fields)} (vs anchor {len(state.fields)})"
        )
    else:
        code_gen_fields = state.focused_fields if state.focused_fields else state.fields[:30]

    prompt_context = PromptContext(
        dataset_id=state.dataset_id,
        dataset_description=state.dataset_description or "",
        dataset_category=state.dataset_category or "",
        region=state.region,
        universe=state.universe,
        fields=code_gen_fields,
        # Full operator catalog (see node_hypothesis note) — code_gen must see
        # the Cross Sectional / Group operators to compose neutralized alphas.
        operators=state.operators,
        success_patterns=merged_patterns[:8],
        failure_pitfalls=state.pitfalls[:5],
        preferred_fields=preferred_fields,
        avoid_fields=avoid_fields,
        focus_hypotheses=focus_hypotheses + [
            h.get("statement", h.get("idea", str(h))) if isinstance(h, dict) else str(h)
            for h in state.hypotheses[:3]
        ],
        avoid_patterns=avoid_patterns,
        num_alphas=state.num_alphas_target,
        exploration_weight=exploration_weight,
        available_dataset_pool=getattr(state, "available_dataset_pool", []) or [],
    )

    # Use enhanced prompt builder with hypothesis and feedback context
    prompt = build_alpha_generation_prompt(
        prompt_context,
        target_hypothesis=target_hypothesis,
        experiment_feedback=experiment_feedback
    )

    # delay-0 native mining (2026-05-26): the offered field list IS the delay-0
    # roster, but RAG/KB patterns are delay-1 so the LLM reaches for delay-1
    # field names (e.g. anl4_af_eps_value) that don't exist at delay-0 → BRAIN
    # "unknown variable" → wasted sim. Hard-constrain generation to the listed
    # fields. (Pairs with the validation-node delay-0 strict_field_check that
    # rejects any that still slip through, pre-sim.)
    if getattr(state, "delay", 1) != 1:
        prompt += (
            "\n\n## CRITICAL — delay-0 simulation\n"
            "This is a DELAY-0 run. You MUST use ONLY field IDs that appear "
            "verbatim in the Available Fields list above. Field names from "
            "examples, patterns, or prior knowledge that are NOT in that list "
            "(many delay-1-only fields look plausible but DO NOT EXIST at "
            "delay-0) will fail simulation with 'unknown variable' and waste "
            "the run. When in doubt, choose a field that is explicitly listed."
        )

    # Size the output budget to the batch. One JSON response holds
    # num_alphas_target alpha objects (~400-500 tokens each); the call default
    # of 4096 truncates a batch of 10 → JSONDecodeError → 0 candidates. Keep
    # max(4096, …) so the old batch-of-4 budget never shrinks; cap at the
    # configured ceiling (must be ≤ model max output). See config.py.
    _n_alphas = int(getattr(state, "num_alphas_target", 0) or 4)
    _code_gen_max_tokens = min(
        _gen_settings.CODE_GEN_MAX_TOKENS_CEILING,
        max(4096, 1024 + _gen_settings.CODE_GEN_MAX_TOKENS_PER_ALPHA * _n_alphas),
    )
    try:
        response = await llm_service.call(
            system_prompt=ALPHA_GENERATION_SYSTEM,
            user_prompt=prompt,
            temperature=temperature,
            json_mode=True,
            max_tokens=_code_gen_max_tokens,
            node_key="code_gen",
        )
    except Exception as llm_err:
        logger.error(f"[{node_name}] LLM call exception: {llm_err}")
        response = _failed_llm_response(str(llm_err))

    duration_ms = int((time.time() - start_time) * 1000)

    # Parse alphas into candidates
    pending_alphas = []
    implementation_notes = ""
    alternatives_considered = []
    # F2/F3 review fix (Sprint 4 R2+R3): G3-v2 parse-fail telemetry +
    # min-pass-rate degrade-open floor. Dropped candidates are buffered
    # (not silently `continue`d) so (a) we can count the drop rate and
    # degrade-open when a too-narrow grammar would zero out the round,
    # and (b) the parse-fail count survives to a state counter for
    # telemetry (the dropped candidate's own metrics never persist —
    # mirror of Sprint 2 F2 / Sprint 3 F1 trap).
    _g3v2_parse_fail_buffer = []  # candidates that failed grammar parse
    _g3v2_total_seen = 0

    if response.success and response.parsed and isinstance(response.parsed, dict):
        parsed = response.parsed
        # V-26.48 (2026-05-13): validate `alphas` is actually a list of dicts
        # before iterating. LLM output is best-effort JSON; a malformed
        # response can drop `alphas` as a string, dict, or null which would
        # throw `AttributeError: 'str' object has no attribute 'get'` deep
        # inside the loop. Drop malformed entries; skip the whole list if
        # the wrapper itself isn't iterable.
        raw_alphas_raw = parsed.get("alphas", [])
        if not isinstance(raw_alphas_raw, list):
            logger.warning(
                f"[{node_name}] V-26.48 LLM response 'alphas' is "
                f"{type(raw_alphas_raw).__name__}, expected list — discarding"
            )
            raw_alphas = []
        else:
            raw_alphas = [a for a in raw_alphas_raw if isinstance(a, dict)]
            if len(raw_alphas) != len(raw_alphas_raw):
                logger.warning(
                    f"[{node_name}] V-26.48 dropped "
                    f"{len(raw_alphas_raw) - len(raw_alphas)} non-dict entries "
                    f"from LLM 'alphas' list"
                )
        implementation_notes = parsed.get("implementation_notes", "")
        if not isinstance(implementation_notes, str):
            implementation_notes = ""
        alternatives_considered = parsed.get("alternatives_considered", [])
        if not isinstance(alternatives_considered, list):
            alternatives_considered = []

        # Phase 4 Sprint 1 A1.3 (2026-05-20): assistant-mode template synth.
        # When state.llm_mode_used == "assistant", every alpha_data has its
        # LLM-emitted ``expression`` overwritten by the template composer's
        # output. The hypothesis text remains the authoritative artifact;
        # the DSL is *re-derived* from a curated 10-entry library
        # (backend/data/assistant_mode_templates.yaml) keyed on pillar +
        # keyword overlap. Soft-fail: per-alpha — if no template matches,
        # the LLM's own expression survives (we fall through to author
        # behavior for that single candidate; never break the round).
        # A1.4 will measure via /ops/llm-mode/comparison whether this
        # actually moves PASS rate. Authorial path is byte-identical when
        # state.llm_mode_used == "author".
        _assistant_mode_active = (
            getattr(state, "llm_mode_used", "author") == "assistant"
        )
        if _assistant_mode_active:
            try:
                from backend.services.assistant_template import (
                    compose_for_hypothesis as _assistant_compose,
                )
            except Exception as _imp_ex:  # noqa: BLE001
                logger.warning(
                    "[%s] A1.3 assistant_template import failed (falling through "
                    "to author per-alpha): %s", node_name, _imp_ex,
                )
                _assistant_compose = None  # type: ignore
        else:
            _assistant_compose = None

        for alpha_data in raw_alphas:
            # Slim schema (2026-06): economic_hypothesis is the single canonical
            # reasoning slot. Older payloads may still carry hypothesis_tested /
            # explanation, so fall back through them before economic_hypothesis.
            economic_hypothesis = alpha_data.get("economic_hypothesis", "")
            hypothesis_text = (
                alpha_data.get("hypothesis_tested")
                or alpha_data.get("hypothesis")
                or economic_hypothesis
            )

            # Handle both old format (string) and new format (dict) for explanation
            explanation_raw = alpha_data.get("explanation") or economic_hypothesis
            if isinstance(explanation_raw, dict):
                explanation = f"{explanation_raw.get('approach', '')} - {explanation_raw.get('market_logic', '')}"
            else:
                explanation = explanation_raw

            # V-26.50 (2026-05-13): LLM output sometimes has expected_sharpe
            # as a string, NaN, or absurd magnitude (the field is user-facing
            # context in downstream prompts so a junk value pollutes the loop).
            # Clip to a sane range, drop non-numerics.
            raw_es = alpha_data.get("expected_sharpe")
            sanitized_es: Optional[float]
            try:
                v = float(raw_es) if raw_es is not None else None
                if v is None or v != v:  # NaN check
                    sanitized_es = None
                else:
                    sanitized_es = max(-5.0, min(10.0, v))
            except (TypeError, ValueError):
                sanitized_es = None

            # A1.3 assistant override: re-compose DSL from template library.
            _composed_expression: Optional[str] = None
            _composed_template_id: Optional[str] = None
            _composed_score: Optional[float] = None
            if _assistant_compose is not None and hypothesis_text:
                try:
                    pillar_hint = alpha_data.get("pillar") or alpha_data.get(
                        "pillar_choice"
                    )
                    composed = _assistant_compose(
                        hypothesis_text,
                        pillar=pillar_hint if isinstance(pillar_hint, str) else None,
                    )
                    if composed is not None and composed.get("expression"):
                        _composed_expression = composed["expression"]
                        _composed_template_id = composed.get("template_id")
                        _composed_score = composed.get("score")
                except Exception as _comp_ex:  # noqa: BLE001
                    logger.warning(
                        "[%s] A1.3 assistant compose failed for hypothesis %r "
                        "(falling through to author expression): %s",
                        node_name, hypothesis_text[:40], _comp_ex,
                    )

            candidate = AlphaCandidate(
                expression=(
                    _composed_expression
                    if _composed_expression is not None
                    else alpha_data.get("expression", "")
                ),
                hypothesis=hypothesis_text,
                explanation=explanation,
                expected_sharpe=sanitized_es,
            )

            # Attach additional metadata for tracking (in-round routing
            # decisions only — these fields NEVER persist to alpha.metrics
            # because persistence.py:382 reads alpha.metrics, not metadata).
            candidate.metadata = {
                "fields_used": alpha_data.get("fields_used", []),
                "complexity": alpha_data.get("complexity", "unknown"),
                "novelty_level": alpha_data.get("novelty_level", "unknown"),
            }
            # P1 (2026-05-24): persist the 5-slot reasoning chain into
            # candidate.metrics (the PERSISTED path — the metadata above never
            # reaches alpha.metrics). These slots are generated under the
            # code_gen=xhigh budget then were discarded (0/200 reached
            # alpha.metrics), so we could neither measure predicted_turnover
            # vs actual (is the CoT real or theater?) nor feed signal_velocity
            # back into the P2 velocity-table calibration. Persisting them
            # (under _reasoning_*) builds the measurement both need before any
            # decision to drop the chain. predicted_turnover is coerced to
            # float when numeric so the downstream predicted-vs-actual analysis
            # is a plain numeric compare.
            for _slot in (
                "economic_hypothesis", "signal_velocity",
                "predicted_turnover", "math_sanity_check",
            ):
                _sv = alpha_data.get(_slot)
                if _sv is None:
                    continue
                if _slot == "predicted_turnover":
                    try:
                        _sv = float(_sv)
                    except (TypeError, ValueError):
                        pass  # keep raw string if LLM emitted a non-numeric
                candidate.metrics[f"_reasoning_{_slot}"] = _sv
            # A1.3 trace assistant-mode synth — must land in candidate.metrics
            # (NOT metadata) so the keys survive the validate → simulate →
            # evaluate pipeline via evaluation.py:1278 setdefault merge into
            # alpha.metrics. F1 fix per Sprint 1 S1-A Seam 3 review: prior
            # writes to .metadata caused R12 GO gate to see 100% author / 0%
            # assistant because A1.4 query_mode_pool reads row.metrics
            # (persisted) not row.metadata (which doesn't even persist).
            if _assistant_mode_active:
                candidate.metrics["llm_mode_used"] = "assistant"
                if _composed_expression is not None:
                    candidate.metrics["assistant_template_id"] = _composed_template_id
                    candidate.metrics["assistant_template_score"] = _composed_score
                    candidate.metrics["assistant_template_fallthrough"] = False
                else:
                    candidate.metrics["assistant_template_fallthrough"] = True

            # F14 review fix (Sprint 4 R3): stamp G10 inject coverage onto
            # each candidate's metrics (reachable persist path). 0 = OFF /
            # no rows. These flow through validate→simulate→evaluate into
            # alpha.metrics via the setdefault merge.
            _g10_n = int(getattr(state, "g10_injected_entries_n", 0) or 0)
            if _g10_n > 0:
                candidate.metrics["_g10_injected"] = True
                candidate.metrics["_g10_entries_n"] = _g10_n

            if candidate.expression and candidate.expression.strip():
                # B4.1 G3-v2 grammar-aware validation (Sprint 4, 2026-05-20):
                # parse the expression BEFORE persistence / simulation.
                # Parse-fail candidates are BUFFERED (not silently dropped)
                # so a min-pass-rate floor can degrade-open if the grammar
                # is too narrow (F2/F3 review fix). Soft-fail: validator
                # exception falls through to legacy append.
                if bool(getattr(_gen_settings, "ENABLE_GRAMMAR_VALIDATOR", False)):
                    try:
                        from backend.services import grammar_validator as _g3v2
                        _g3v2_total_seen += 1
                        _g3v2_res = _g3v2.validate(candidate.expression)
                        if not _g3v2_res.ok:
                            candidate.metrics["_g3v2_parse_failed"] = True
                            candidate.metrics["_g3v2_parse_error"] = _g3v2_res.error_msg
                            candidate.metrics["_g3v2_parse_position"] = _g3v2_res.error_position
                            logger.info(
                                f"[{node_name}] G3-v2 parse fail "
                                f"({_g3v2_res.error_msg[:80]}); "
                                f"buffering expression={candidate.expression[:60]}"
                            )
                            _g3v2_parse_fail_buffer.append(candidate)
                            continue  # decision deferred to post-loop floor check
                        if _g3v2_res.unknown_ops:
                            candidate.metrics["_g3v2_unknown_ops"] = list(_g3v2_res.unknown_ops)
                            logger.debug(
                                f"[{node_name}] G3-v2 unknown ops "
                                f"{_g3v2_res.unknown_ops} in {candidate.expression[:60]}"
                            )
                    except Exception as _g3v2_ex:  # noqa: BLE001
                        logger.warning(
                            f"[{node_name}] G3-v2 validator failed (non-fatal): {_g3v2_ex}"
                        )
                pending_alphas.append(candidate)

    # F2/F3 review fix: G3-v2 min-pass-rate degrade-open floor + telemetry.
    # If grammar would drop > 50% of this round's candidates, a too-narrow
    # grammar (or a lark version regression) is the likely cause — better
    # to degrade-open (re-include the buffered candidates) than to zero out
    # production. Always record the parse-fail count to a state counter so
    # an operator can observe the drop rate even though the dropped
    # candidate's own metrics never persist (it's not in pending_alphas).
    if _g3v2_total_seen > 0 and _g3v2_parse_fail_buffer:
        _g3v2_drop_rate = len(_g3v2_parse_fail_buffer) / _g3v2_total_seen
        try:
            state.g3v2_parse_fail_count = (
                int(getattr(state, "g3v2_parse_fail_count", 0) or 0)
                + len(_g3v2_parse_fail_buffer)
            )
            state.g3v2_total_validated = (
                int(getattr(state, "g3v2_total_validated", 0) or 0)
                + _g3v2_total_seen
            )
        except Exception:  # noqa: BLE001 — state attr-set must never break round
            pass
        if _g3v2_drop_rate > 0.5:
            logger.warning(
                f"[{node_name}] G3-v2 drop rate {_g3v2_drop_rate:.0%} > 50% "
                f"({len(_g3v2_parse_fail_buffer)}/{_g3v2_total_seen}) — "
                f"degrade-open: re-including buffered candidates (grammar "
                f"likely too narrow; check /ops or widen _GRAMMAR)"
            )
            for _buffered in _g3v2_parse_fail_buffer:
                _buffered.metrics["_g3v2_degrade_open_readmit"] = True
                pending_alphas.append(_buffered)
        else:
            logger.info(
                f"[{node_name}] G3-v2 dropped {len(_g3v2_parse_fail_buffer)}/"
                f"{_g3v2_total_seen} ({_g3v2_drop_rate:.0%}, within floor)"
            )

    _debug_log("A", "nodes.py:code_gen:result", "Alpha code generation complete", {
        "alphas_generated": len(pending_alphas),
        "target": state.num_alphas_target,
        "duration_ms": duration_ms,
        "llm_success": response.success,
        "temperature": temperature,
        "implementation_notes": implementation_notes[:100] if implementation_notes else ""
    })
    
    logger.info(f"[{node_name}] Complete | alphas={len(pending_alphas)}")

    # R3/Q8 (Phase 1, 2026-05-17): light wiring — write per-alpha ast_distance
    # to ast_distance_log dedicated table when flag ON. Soft-fail, never
    # blocks generation. Per R1a v1.6 lesson, must NOT route via
    # AlphaCandidate.metrics (95% drop rate).
    try:
        from backend.ast_distance_logger import log_round_ast_distances
        task_id = getattr(state, "task_id", None)
        round_idx = getattr(state, "current_iteration", None) or getattr(state, "current_round", None)
        new_exprs = [a.expression for a in pending_alphas if getattr(a, "expression", None)]
        await log_round_ast_distances(task_id, round_idx, new_exprs)
    except Exception as e:
        logger.debug(f"[{node_name}] R3/Q8 ast_distance log skip (non-fatal): {e}")

    trace_update = await record_trace(
        state, trace_service, node_name,
        {
            "num_alphas_target": state.num_alphas_target,
            "strategy": {
                "temperature": temperature,
                "exploration_weight": exploration_weight,
                "preferred_fields_count": len(preferred_fields),
                "avoid_fields_count": len(avoid_fields),
                "has_target_hypothesis": target_hypothesis is not None,
                "feedback_length": len(experiment_feedback),
            }
        },
        {
            "alphas_generated": len(pending_alphas),
            "expressions": [a.expression[:200] for a in pending_alphas],
            "implementation_notes": implementation_notes,
            "alternatives_count": len(alternatives_considered)
        },
        duration_ms,
        "SUCCESS" if response.success else "FAILED",
        response.error if hasattr(response, 'error') else None
    )
    
    # G5 Phase A (2026-05-19): prepend crossover offspring (carried from prior
    # round via state.g5_offspring_candidates). Each becomes a fresh
    # AlphaCandidate with parent ids stamped on metrics so the persistence
    # layer can write _g5_crossover_parent_ids without needing more plumbing.
    # Soft-fail: malformed entries are dropped silently.
    g5_offspring = getattr(state, "g5_offspring_candidates", None) or []
    if g5_offspring:
        from backend.agents.graph.state import AlphaCandidate as _G5AC
        _g5_prepended: List = []
        for off in g5_offspring:
            if not isinstance(off, dict):
                continue
            expr = (off.get("expression") or "").strip()
            if not expr:
                continue
            try:
                parent_ids = [
                    int(x) for x in (
                        off.get("parent_a_alpha_id"),
                        off.get("parent_b_alpha_id"),
                    ) if x is not None
                ]
                cand = _G5AC(
                    expression=expr,
                    hypothesis=(
                        "G5 crossover: combine alpha "
                        f"{off.get('parent_a_alpha_id', '?')} + "
                        f"{off.get('parent_b_alpha_id', '?')} via "
                        f"{off.get('combination_strategy', '?')}"
                    ),
                    explanation=(off.get("rationale") or "")[:200],
                    parent_alpha_id=parent_ids[0] if parent_ids else None,
                    metrics={
                        "_g5_crossover_parent_ids": parent_ids,
                        "_g5_combination_strategy": off.get(
                            "combination_strategy", "unspecified"
                        ),
                    },
                )
                _g5_prepended.append(cand)
            except Exception as _g5_ex:
                logger.warning(
                    f"[{node_name}] G5 offspring AlphaCandidate build failed "
                    f"(non-fatal, dropping): {_g5_ex}"
                )
        if _g5_prepended:
            logger.info(
                f"[{node_name}] G5 prepended {len(_g5_prepended)} offspring "
                f"to pending_alphas (parent ids carried in metrics)"
            )
            pending_alphas = _g5_prepended + pending_alphas

    return {
        "pending_alphas": pending_alphas,
        "current_alpha_index": 0,
        **trace_update
    }
