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

import time
import random
from typing import Dict, List, Optional
from loguru import logger
from langchain_core.runnables import RunnableConfig

from backend.agents.graph.state import MiningState, AlphaCandidate
from backend.agents.graph.nodes.base import record_trace, _debug_log
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
        result = await rag_service.query(
            dataset_id=state.dataset_id,
            region=state.region,
            max_patterns=5,
            max_pitfalls=10
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
            json_mode=True
        )
    except Exception as llm_err:
        logger.error(f"[{node_name}] LLM call failed: {llm_err}")
        response = type('obj', (object,), {'success': False, 'parsed': None, 'error': str(llm_err)})()
    
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
    
    target_fields = state.focused_fields if state.focused_fields else state.fields[:20]
    
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
        operators=state.operators[:30],
        success_patterns=state.patterns[:5],
        failure_pitfalls=state.pitfalls[:5],
        exploration_weight=exploration_weight,
        available_dataset_pool=getattr(state, "available_dataset_pool", []) or [],
    )
    
    # Use new hypothesis builder with experiment trace
    prompt = build_hypothesis_prompt(prompt_context, experiment_trace)
    
    # Adjust temperature based on exploration weight
    # Higher exploration -> higher temperature for more diverse hypotheses
    temperature = 0.7 + (exploration_weight * 0.3)  # Range: 0.7 - 1.0
    
    response = await llm_service.call(
        system_prompt=HYPOTHESIS_SYSTEM,
        user_prompt=prompt,
        temperature=temperature,
        json_mode=True
    )
    
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
            from backend.tasks.mining_tasks import _get_dataset_fields
            from backend.database import AsyncSessionLocal
            seen_ids: set = set()
            async with AsyncSessionLocal() as _db:
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
    hge_level = int(cfg.get("hypothesis_centric_level", 0) or 0)
    if hge_level >= 2 and hypotheses:
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
                    data = HypothesisCreateData(
                        statement=statement,
                        rationale=primary_h.get("rationale") or primary_h.get("reason") or "",
                        region=state.region,
                        universe=state.universe,
                        kind=HypothesisKind.INVESTMENT_THESIS.value,
                        target_tier=int(getattr(state, "factor_tier", 1) or 1),
                        expected_signal=primary_h.get("expected_signal", "unknown"),
                        confidence=primary_h.get("confidence", "medium"),
                        novelty=primary_h.get("novelty", "established"),
                        key_fields=primary_h.get("key_fields") or [],
                        suggested_operators=primary_h.get("suggested_operators") or [],
                        dataset_pool=primary_h.get("selected_datasets") or [],
                        experiment_variant=str(experiment_variant)
                            if experiment_variant is not None else None,
                    )
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
        **trace_update
    }


# Backward compatible helper - select exploration fields
def _select_exploration_fields(
    target_fields: List[Dict],
    all_fields: List[Dict],
    count: int = 3
) -> List[Dict]:
    """
    Select fields for exploration that are not in the target set.
    
    This helps ensure diversity and prevents tunnel vision.
    """
    remaining_fields = [f for f in all_fields if f not in target_fields]
    if len(remaining_fields) >= count:
        return random.sample(remaining_fields, count)
    elif len(all_fields) > count:
        return random.sample(all_fields, min(count, len(all_fields)))
    return all_fields


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
        from backend.database import AsyncSessionLocal
        async with AsyncSessionLocal() as _db:
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
        # Cap at 60 to keep prompt manageable; hypothesis node already
        # capped at 80 so this is a soft trim.
        code_gen_fields = hypothesis_fields[:60]
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
        operators=state.operators[:50],
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
    
    try:
        response = await llm_service.call(
            system_prompt=ALPHA_GENERATION_SYSTEM,
            user_prompt=prompt,
            temperature=temperature,
            json_mode=True
        )
    except Exception as llm_err:
        logger.error(f"[{node_name}] LLM call exception: {llm_err}")
        response = type('obj', (object,), {'success': False, 'parsed': None, 'error': str(llm_err)})()
    
    duration_ms = int((time.time() - start_time) * 1000)
    
    # Parse alphas into candidates
    pending_alphas = []
    implementation_notes = ""
    alternatives_considered = []
    
    if response.success and response.parsed and isinstance(response.parsed, dict):
        parsed = response.parsed
        raw_alphas = parsed.get("alphas", []) or []
        implementation_notes = parsed.get("implementation_notes", "")
        alternatives_considered = parsed.get("alternatives_considered", [])
        
        for alpha_data in raw_alphas:
            # Handle both old format (hypothesis) and new format (hypothesis_tested)
            hypothesis_text = alpha_data.get("hypothesis_tested", alpha_data.get("hypothesis", ""))
            
            # Handle both old format (string) and new format (dict) for explanation
            explanation_raw = alpha_data.get("explanation", "")
            if isinstance(explanation_raw, dict):
                explanation = f"{explanation_raw.get('approach', '')} - {explanation_raw.get('market_logic', '')}"
            else:
                explanation = explanation_raw
            
            candidate = AlphaCandidate(
                expression=alpha_data.get("expression", ""),
                hypothesis=hypothesis_text,
                explanation=explanation,
                expected_sharpe=alpha_data.get("expected_sharpe")
            )
            
            # Attach additional metadata for tracking
            candidate.metadata = {
                "fields_used": alpha_data.get("fields_used", []),
                "complexity": alpha_data.get("complexity", "unknown"),
                "novelty_level": alpha_data.get("novelty_level", "unknown"),
            }
            
            if candidate.expression and candidate.expression.strip():
                pending_alphas.append(candidate)
    
    _debug_log("A", "nodes.py:code_gen:result", "Alpha code generation complete", {
        "alphas_generated": len(pending_alphas),
        "target": state.num_alphas_target,
        "duration_ms": duration_ms,
        "llm_success": response.success,
        "temperature": temperature,
        "implementation_notes": implementation_notes[:100] if implementation_notes else ""
    })
    
    logger.info(f"[{node_name}] Complete | alphas={len(pending_alphas)}")
    
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
    
    return {
        "pending_alphas": pending_alphas,
        "current_alpha_index": 0,
        **trace_update
    }
