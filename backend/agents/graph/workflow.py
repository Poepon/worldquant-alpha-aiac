"""
LangGraph Mining Workflow
Orchestrates the complete alpha mining state graph
"""

from typing import Dict, List, Optional, Any, Annotated
from functools import partial
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from backend.agents.graph.state import MiningState, AlphaResult, FailureRecord
from backend.agents.graph.nodes import (
    node_rag_query,
    node_distill_context,
    node_hypothesis,
    node_code_gen,
    node_validate,
    node_self_correct,
    node_simulate,
    node_evaluate,
    node_save_results
)
from backend.agents.graph.nodes.t1_nodes import (
    node_t1_strategy_select,
    node_t1_expand,
)
from backend.agents.graph.nodes.tier_seed import (
    node_tier_seed_load,
    node_tier_strategy_select,
    node_tier_wrap_one,
)
from backend.agents.graph.edges import route_after_validate
from backend.agents.services import LLMService, RAGService, get_llm_service
from backend.adapters.brain_adapter import BrainAdapter
from backend.config import settings
from backend.models import MiningTask


# =============================================================================
# Tier-aware routing functions (PR2)
# =============================================================================

def _route_at_start(state: MiningState) -> str:
    """Conditional entry point. T1 → legacy/llm-guided rag flow; T2/T3 → tier_seed_load."""
    if state.factor_tier and state.factor_tier > 1:
        return "tier_seed_load"
    return "rag_query"


def _route_after_distill(state: MiningState) -> str:
    """T1 task: routing after distill_context.

    Plan v5+ §Phase 1 C-architecture (2026-05-04):
      - Phase 1 (available_dataset_pool > 1): hypothesis 节点先选 dataset,
        然后 → t1_strategy_select(可见 union fields)→ t1_expand
      - T1_USE_LLM_GUIDED_STRATEGY=True (legacy default): t1_strategy_select
      - T1_USE_LLM_GUIDED_STRATEGY=False: hypothesis → code_gen (legacy)
    """
    pool = getattr(state, "available_dataset_pool", []) or []
    if len(pool) > 1:
        return "hypothesis"  # Phase 1
    if getattr(settings, "T1_USE_LLM_GUIDED_STRATEGY", True):
        return "t1_strategy_select"
    return "hypothesis"  # legacy hypothesis path


def _route_after_hypothesis(state: MiningState) -> str:
    """After hypothesis node: Phase 1 → t1_strategy_select; legacy → code_gen.

    Phase 1 active means hypothesis has populated current_hypothesis_datasets +
    current_hypothesis_fields, and we want t1_strategy_select to consume them.
    Legacy (T1_USE_LLM_GUIDED_STRATEGY=False) keeps hypothesis → code_gen.
    """
    pool = getattr(state, "available_dataset_pool", []) or []
    if len(pool) > 1:
        return "t1_strategy_select"
    return "code_gen"


def _route_after_seed_load(state: MiningState) -> str:
    """If tier_seed_load found insufficient seeds, end the run."""
    if getattr(state, "should_stop", False):
        return "END"
    if not state.tier_seeds:
        return "END"
    return "tier_strategy_select"


def _route_after_save_results(state: MiningState) -> str:
    """T1 → END (mining_agent's outer loop handles multi-round).
    T2/T3 → loop to next seed if any remain, else END.

    Bug fix (2026-05-03): respect state.early_stopped flag set by
    node_save_results. Without this, T2/T3 graph kept advancing through
    seeds even after W1 round-level early-stop fired, causing one task to
    run 31 outer iterations × 12 seeds = 400+ rounds over 13 hours and
    pinning a worker indefinitely. See spike task 34/37/38 incident.
    """
    if state.factor_tier and state.factor_tier > 1:
        if state.early_stopped:
            return "END"
        next_idx = state.current_seed_index + 1
        if next_idx < len(state.tier_seeds):
            return "tier_strategy_select"
    return "END"


async def node_advance_seed(state: MiningState, config=None) -> Dict:
    """Pure state increment: advance current_seed_index for next T2/T3 round."""
    return {"current_seed_index": state.current_seed_index + 1}


class MiningWorkflow:
    """
    LangGraph-based mining workflow.
    
    Features:
    - Strongly typed state (Pydantic)
    - Conditional edges for self-correction loops
    - Full trace recording
    - Configurable checkpointing
    """
    
    def __init__(
        self,
        db: AsyncSession,
        brain: BrainAdapter = None,
        llm_service: LLMService = None,
        checkpointer: Optional[BaseCheckpointSaver] = None
    ):
        self.db = db
        self.brain = brain or BrainAdapter()
        self.llm_service = llm_service or get_llm_service()
        self.rag_service = RAGService(db)
        self.checkpointer = checkpointer
        
        self._graph = self._build_graph()
        
        logger.info("[MiningWorkflow] Initialized")
    
    def _build_graph(self) -> StateGraph:
        """
        Build the mining state graph.
        
        Graph structure (Batch):
        START -> rag_query -> distill_context -> hypothesis -> code_gen 
                 -> validate <--> self_correct
                    | (All processed)
                    v
                 simulate -> evaluate -> save_results -> END
        """
        # Create graph with state type
        workflow = StateGraph(MiningState)
        
        # =====================================================================
        # Add Nodes
        # =====================================================================
        
        # RAG query node (bind dependencies)
        workflow.add_node(
            "rag_query",
            partial(node_rag_query, rag_service=self.rag_service)
        )

        # Distill Context node
        workflow.add_node(
            "distill_context",
            partial(node_distill_context, llm_service=self.llm_service)
        )
        
        # Hypothesis node
        workflow.add_node(
            "hypothesis",
            partial(node_hypothesis, llm_service=self.llm_service)
        )
        
        # Code Generation node
        workflow.add_node(
            "code_gen",
            partial(node_code_gen, llm_service=self.llm_service)
        )
        
        # Validation node (no external deps)
        workflow.add_node("validate", node_validate)
        
        # Self-correction node
        workflow.add_node(
            "self_correct",
            partial(node_self_correct, llm_service=self.llm_service)
        )
        
        # Simulation node
        workflow.add_node(
            "simulate",
            partial(node_simulate, brain=self.brain)
        )
        
        # Evaluation node
        workflow.add_node(
            "evaluate",
            partial(node_evaluate, brain=self.brain)
        )
        
        # Save results node (handles both success and failure saving)
        workflow.add_node("save_results", node_save_results)

        # PR2: T1 LLM-guided generation nodes
        workflow.add_node("t1_strategy_select", node_t1_strategy_select)
        workflow.add_node("t1_expand", node_t1_expand)

        # PR2: T2/T3 tier-seed-driven wrapping nodes
        workflow.add_node("tier_seed_load", node_tier_seed_load)
        workflow.add_node("tier_strategy_select", node_tier_strategy_select)
        workflow.add_node("tier_wrap_one", node_tier_wrap_one)
        workflow.add_node("advance_seed", node_advance_seed)

        # =====================================================================
        # Add Edges
        # =====================================================================

        # PR2: Conditional entry point — T1 enters via rag_query, T2/T3 via
        # tier_seed_load. set_conditional_entry_point lets the same compiled
        # graph serve both task shapes.
        workflow.set_conditional_entry_point(
            _route_at_start,
            {
                "rag_query": "rag_query",
                "tier_seed_load": "tier_seed_load",
            },
        )

        # T1 path
        workflow.add_edge("rag_query", "distill_context")
        # PR2: After distill_context, route between legacy hypothesis path and
        # the new LLM-guided strategy/expand path based on
        # T1_USE_LLM_GUIDED_STRATEGY feature flag.
        workflow.add_conditional_edges(
            "distill_context",
            _route_after_distill,
            {
                "hypothesis": "hypothesis",
                "t1_strategy_select": "t1_strategy_select",
            },
        )

        # T1 LLM-guided arm
        workflow.add_edge("t1_strategy_select", "t1_expand")
        workflow.add_edge("t1_expand", "validate")

        # Phase 1 (C-architecture): hypothesis → t1_strategy_select when
        # cross-dataset pool active; otherwise legacy hypothesis → code_gen.
        # _route_after_hypothesis decides per-state.
        workflow.add_conditional_edges(
            "hypothesis",
            _route_after_hypothesis,
            {
                "t1_strategy_select": "t1_strategy_select",
                "code_gen": "code_gen",
            },
        )
        workflow.add_edge("code_gen", "validate")

        # After validate: existing self-correct vs simulate routing (shared by
        # all T1 paths and by T2/T3 wrap-one output).
        workflow.add_conditional_edges(
            "validate",
            route_after_validate,
            {
                "simulate": "simulate",
                "self_correct": "self_correct"
            }
        )
        workflow.add_edge("self_correct", "validate")

        # T2/T3 path
        # tier_seed_load may early-stop (insufficient seeds); route accordingly.
        workflow.add_conditional_edges(
            "tier_seed_load",
            _route_after_seed_load,
            {
                "tier_strategy_select": "tier_strategy_select",
                "END": END,
            },
        )
        workflow.add_edge("tier_strategy_select", "tier_wrap_one")
        # T2/T3 variants skip VALIDATE (programmatic _dedup_and_validate already
        # filtered them) and go straight to simulate.
        workflow.add_edge("tier_wrap_one", "simulate")

        # Shared post-pipeline
        workflow.add_edge("simulate", "evaluate")
        workflow.add_edge("evaluate", "save_results")

        # After save_results: T1 → END (mining_agent loops); T2/T3 → next seed
        # via advance_seed → tier_strategy_select, or END if last seed.
        workflow.add_conditional_edges(
            "save_results",
            _route_after_save_results,
            {
                "tier_strategy_select": "advance_seed",
                "END": END,
            },
        )
        workflow.add_edge("advance_seed", "tier_strategy_select")

        return workflow
    
    def compile(self):
        """Compile the graph with optional checkpointer."""
        return self._graph.compile(checkpointer=self.checkpointer)
    
    async def run(
        self,
        task: MiningTask,
        dataset_id: str,
        fields: List[Dict],
        operators: List[str],
        num_alphas: int = 3,
        config: Dict[str, Any] = None,
        factor_tier: int = 1,
    ) -> Dict[str, Any]:
        """
        Execute the mining workflow.

        Args:
            task: Mining task instance
            dataset_id: Dataset to mine
            fields: Available data fields
            operators: Available operators
            num_alphas: Target number of alphas
            factor_tier: 1 / 2 / 3. PR2 — derived from task.agent_mode by the
                router (AUTONOMOUS_TIER1 → 1, AUTONOMOUS_TIER2 → 2,
                AUTONOMOUS_TIER3 → 3, AUTONOMOUS → 1). evaluation node and
                future tier_seed nodes branch on this.

        Returns:
            Dictionary with generated_alphas, failures, trace_steps, factor_tier.
        """
        logger.info(
            f"[MiningWorkflow] 开始执行 | "
            f"task={task.id} dataset={dataset_id} target={num_alphas} tier={factor_tier}"
        )

        # Initialize state
        configurable = (config or {}).get("configurable", {}) if config else {}
        available_dataset_pool = configurable.get("available_dataset_pool", []) or []
        initial_state = MiningState(
            task_id=task.id,
            region=task.region,
            universe=task.universe,
            dataset_id=dataset_id,
            fields=fields,
            operators=operators,
            num_alphas_target=num_alphas,
            factor_tier=factor_tier,
            available_dataset_pool=available_dataset_pool,
        )
        
        # Compile and run
        app = self.compile()
        
        # Execute graph (Synchronous-style for full state)
        # We use invoke to ensure we get the accumulated final state, NOT just partial updates
        final_state = await app.ainvoke(initial_state, config=config)
        
        # Log completion
        logger.info("[MiningWorkflow] Worklfow execution finished")
        
        # Get results
        generated_alphas = []
        failures = []
        
        if hasattr(final_state, 'generated_alphas'):
            generated_alphas = final_state.generated_alphas
        elif isinstance(final_state, dict):
            generated_alphas = final_state.get('generated_alphas', [])
        
        if hasattr(final_state, 'failures'):
            failures = final_state.failures
        elif isinstance(final_state, dict):
            failures = final_state.get('failures', [])
        
        logger.info(
            f"[MiningWorkflow] 执行完成 | "
            f"success={len(generated_alphas)} failed={len(failures)}"
        )
        
        # Extract tier from final_state for run_and_persist propagation. Falls
        # back to the input factor_tier if state didn't carry it (legacy path).
        if hasattr(final_state, 'factor_tier'):
            ft = final_state.factor_tier
        elif isinstance(final_state, dict):
            ft = final_state.get('factor_tier', factor_tier)
        else:
            ft = factor_tier

        return {
            "generated_alphas": generated_alphas,
            "failures": failures,
            "trace_steps": final_state.trace_steps if hasattr(final_state, 'trace_steps') else [],
            "factor_tier": ft,
        }
    
    async def run_with_persistence(
        self,
        task: MiningTask,
        dataset_id: str,
        fields: List[Dict],
        operators: List[str],
        num_alphas: int = 3,
        config: Dict[str, Any] = None,
        factor_tier: int = 1,
    ):
        """
        Execute workflow and persist results to database.

        factor_tier (PR2): caller-supplied tier. The router maps task.agent_mode
        to {1,2,3} and passes here. Defaults to 1 to keep legacy AUTONOMOUS
        path identical to T1 behavior under ENABLE_FACTOR_TIERING.
        """
        from backend.models import Alpha, AlphaFailure, TraceStep

        result = await self.run(
            task, dataset_id, fields, operators, num_alphas, config,
            factor_tier=factor_tier,
        )

        configurable = (config or {}).get("configurable", {})
        run_id = configurable.get("run_id")

        try:
            # Persist alphas
            # P0-fix-2: Import hash function for deduplication
            from backend.alpha_semantic_validator import (
                compute_expression_hash,
                AlphaSemanticValidator,
            )

            # Plan v5+ §Phase 1 fix (2026-05-04): extract fields used in each
            # expression so cross-dataset analytics work. Previously the
            # mining pipeline never set Alpha.fields_used, leaving it as
            # default `[]`; cross-dataset rate metrics silently relied on
            # BRAIN-synced rows (task_id=1 only).
            def _extract_used_fields(expr: str) -> list:
                if not expr:
                    return []
                try:
                    v = AlphaSemanticValidator(
                        fields=[], operators=None,
                        strict_field_check=False, strict_type_check=False,
                    )
                    return list(v.validate(expr).used_fields)
                except Exception:
                    return []

            # PR2: tier from result (state.factor_tier, propagated through run()).
            task_factor_tier = result.get("factor_tier", factor_tier)
            from datetime import datetime as _dt
            task_metrics_snapshot_at = _dt.utcnow()

            # B7: collect alpha objects to enqueue can_submit refresh after commit
            # (need .id which is only populated post-flush).
            can_submit_refresh_targets: list = []

            # V-19.2 (2026-05-05): per-row SAVEPOINT persistence. Pre-V-19.2
            # used a single batch commit so one row's IntegrityError rolled
            # back the entire batch (spike B5/B6 + B7 lost 7/8 tasks of
            # alphas). Now each row is wrapped in self.db.begin_nested(),
            # which issues a SAVEPOINT; on exception only that savepoint
            # rolls back, leaving siblings + outer transaction intact.
            # Errors are also written to logs/persistence_errors.log to
            # bypass the loguru→stderr→Celery-logfile truncation that hid
            # the original silent rollbacks.
            from backend.agents.graph.persistence_errors import (
                log_persistence_error,
            )

            # V-19.3 (2026-05-06): pre-batch SELECT existing alpha_ids to
            # short-circuit known cross-task duplicates. The PR5 sign-flip
            # retry path bypasses node_simulate's filter_unsimulated_expressions
            # check, so when BRAIN normalizes a flipped expression to one
            # already in our alphas table (e.g. task=115 flipping to
            # multiply(-1, ts_rank(returns, 20)) → BRAIN reuses ZY2K0nwn
            # owned by task=83), the INSERT would hit uq_alpha_id. V-19.2
            # caught these via savepoint and logged them, but the new alpha
            # row was still lost. V-19.3 detects them BEFORE INSERT and skips
            # cleanly with an INFO log — no error, no data loss illusion.
            from sqlalchemy import select as _sa_select
            candidate_alpha_ids = [
                ar.alpha_id for ar in result.get("generated_alphas", [])
                if ar.alpha_id and not getattr(ar, "persisted", False)
            ]
            cross_task_dup_ids: set = set()
            if candidate_alpha_ids:
                try:
                    r = await self.db.execute(
                        _sa_select(Alpha.alpha_id).where(
                            Alpha.alpha_id.in_(candidate_alpha_ids)
                        )
                    )
                    cross_task_dup_ids = {row[0] for row in r.fetchall()}
                except Exception as _e:
                    logger.warning(
                        f"[MiningWorkflow] V-19.3 cross-task dedup query failed: {_e} "
                        f"— falling back to per-row savepoint catch"
                    )

            alpha_inserted = 0
            alpha_skipped = 0
            alpha_skipped_dup = 0
            for alpha_result in result.get("generated_alphas", []):
                # PR7 — skip rows already INSERTed by node_save_results in
                # incremental mode. AlphaResult.persisted is set True only
                # by _incremental_save_alphas; legacy buffered path leaves
                # it False, so this gate is a no-op for T1 / disabled-flag.
                if getattr(alpha_result, "persisted", False):
                    alpha_skipped += 1
                    continue
                # V-19.3: cross-task alpha_id collision — BRAIN dedup gave us
                # an alpha_id that another task already owns. Skip cleanly.
                if alpha_result.alpha_id and alpha_result.alpha_id in cross_task_dup_ids:
                    alpha_skipped_dup += 1
                    logger.info(
                        f"[MiningWorkflow] V-19.3 skip cross-task duplicate "
                        f"alpha_id={alpha_result.alpha_id} (already owned by another task) "
                        f"expr={(alpha_result.expression or '')[:100]!r}"
                    )
                    continue
                try:
                    expr_hash = compute_expression_hash(alpha_result.expression) if alpha_result.expression else None
                    metrics_dict = alpha_result.metrics if isinstance(alpha_result.metrics, dict) else {}

                    alpha = Alpha(
                        task_id=task.id,
                        run_id=run_id,
                        alpha_id=alpha_result.alpha_id,
                        expression=alpha_result.expression,
                        expression_hash=expr_hash,
                        hypothesis=alpha_result.hypothesis,
                        logic_explanation=alpha_result.explanation,
                        region=task.region,
                        universe=task.universe,
                        dataset_id=dataset_id,
                        quality_status=alpha_result.quality_status,
                        metrics=alpha_result.metrics,
                        is_sharpe=metrics_dict.get("sharpe"),
                        is_fitness=metrics_dict.get("fitness"),
                        is_turnover=metrics_dict.get("turnover"),
                        is_returns=metrics_dict.get("returns"),
                        is_drawdown=metrics_dict.get("drawdown"),
                        is_margin=metrics_dict.get("margin"),
                        is_long_count=metrics_dict.get("longCount"),
                        is_short_count=metrics_dict.get("shortCount"),
                        factor_tier=task_factor_tier,
                        parent_alpha_id=getattr(alpha_result, "parent_alpha_id", None),
                        metrics_snapshot_at=task_metrics_snapshot_at,
                        # Phase 2 B4: typed Hypothesis link from
                        # AlphaResult.hypothesis_id (set by node_save_results
                        # from state.current_hypothesis_id)
                        hypothesis_id=getattr(alpha_result, "hypothesis_id", None),
                    )
                    # V-19.2: SAVEPOINT per row. flush() inside the nested
                    # transaction surfaces IntegrityError immediately so the
                    # savepoint rolls back this row only.
                    async with self.db.begin_nested():
                        self.db.add(alpha)
                        await self.db.flush()
                    alpha_inserted += 1
                    if alpha_result.quality_status in ("PASS", "PASS_PROVISIONAL") and alpha_result.alpha_id:
                        can_submit_refresh_targets.append(alpha)
                except Exception as e:
                    import traceback as _tb
                    logger.error(
                        f"[MiningWorkflow] V-19.2 alpha INSERT savepoint rolled back: "
                        f"{type(e).__name__}: {e} | alpha_id={getattr(alpha_result, 'alpha_id', None)}"
                    )
                    log_persistence_error(
                        task_id=task.id,
                        phase="alpha_insert",
                        exc=e,
                        alpha_id=getattr(alpha_result, "alpha_id", None),
                        expression=getattr(alpha_result, "expression", None),
                        quality_status=getattr(alpha_result, "quality_status", None),
                        extra={
                            "metrics_keys": list((alpha_result.metrics or {}).keys()) if isinstance(getattr(alpha_result, "metrics", None), dict) else "non-dict",
                            "factor_tier": task_factor_tier,
                            "dataset_id": dataset_id,
                            "traceback_inline": _tb.format_exc(),
                        },
                    )

            # Persist failures (per-row savepoint same as alphas)
            failure_inserted = 0
            for failure in result.get("failures", []):
                try:
                    fail_record = AlphaFailure(
                        task_id=task.id,
                        run_id=run_id,
                        expression=failure.expression[:2000] if failure.expression else None,
                        error_type=failure.error_type,
                        error_message=failure.error_message[:500] if failure.error_message else None,
                        # V-25.B (2026-05-13): typed Hypothesis link.
                        # persistence.py FailureRecord.hypothesis_id carries
                        # the resolved scalar (with list[0] fallback) so the
                        # FK is consistent with the PASS-path Alpha.hypothesis_id.
                        hypothesis_id=getattr(failure, "hypothesis_id", None),
                    )
                    async with self.db.begin_nested():
                        self.db.add(fail_record)
                        await self.db.flush()
                    failure_inserted += 1
                except Exception as e:
                    import traceback as _tb
                    logger.error(
                        f"[MiningWorkflow] V-19.2 failure INSERT savepoint rolled back: "
                        f"{type(e).__name__}: {e}"
                    )
                    log_persistence_error(
                        task_id=task.id,
                        phase="failure_insert",
                        exc=e,
                        expression=getattr(failure, "expression", None),
                        extra={
                            "error_type": getattr(failure, "error_type", None),
                            "traceback_inline": _tb.format_exc(),
                        },
                    )
            
            # Persist trace steps (ONLY if TraceService was NOT used)
            # If TraceService is in config, we assume it handled real-time persistence
            has_realtime_trace = config and config.get("configurable", {}).get("trace_service")

            if not has_realtime_trace:
                for trace in result.get("trace_steps", []):
                    try:
                        step = TraceStep(
                            task_id=task.id,
                            run_id=run_id,
                            step_type=trace.step_type,
                            step_order=trace.step_order,
                            input_data=trace.input_data,
                            output_data=trace.output_data,
                            duration_ms=trace.duration_ms,
                            status=trace.status,
                            error_message=trace.error_message
                        )
                        async with self.db.begin_nested():
                            self.db.add(step)
                            await self.db.flush()
                    except Exception as e:
                        logger.warning(f"[MiningWorkflow] V-19.2 trace step savepoint rolled back: {e}")
                        log_persistence_error(
                            task_id=task.id,
                            phase="trace_step_insert",
                            exc=e,
                            extra={"step_type": getattr(trace, "step_type", None)},
                        )

            await self.db.commit()
            logger.info(
                f"[MiningWorkflow] V-19.3 persistence done | task={task.id} "
                f"alpha_inserted={alpha_inserted} alpha_skipped_persisted={alpha_skipped} "
                f"alpha_skipped_cross_task_dup={alpha_skipped_dup} "
                f"failure_inserted={failure_inserted}"
            )

            # V-19.1 (2026-05-05): post-commit fields_used population.
            # Decoupled from Alpha INSERT to dodge the silent-rollback that
            # affected B5/B6 batches. Each row is updated in its own try
            # block — failure here only loses fields_used for THAT alpha,
            # never the entire batch.
            try:
                fields_used_updated = 0
                for alpha_result in result.get("generated_alphas", []):
                    if getattr(alpha_result, "persisted", False):
                        continue
                    aid = getattr(alpha_result, "alpha_id", None)
                    expr = getattr(alpha_result, "expression", None)
                    if not aid or not expr:
                        continue
                    try:
                        from sqlalchemy import update as _sa_update
                        fids = _extract_used_fields(expr)
                        if not fids:
                            continue
                        await self.db.execute(
                            _sa_update(Alpha)
                            .where(Alpha.task_id == task.id, Alpha.alpha_id == aid)
                            .values(fields_used=fids)
                        )
                        fields_used_updated += 1
                    except Exception as _e:
                        logger.warning(
                            f"[MiningWorkflow] V-19.1 fields_used update failed for "
                            f"alpha_id={aid}: {_e}"
                        )
                if fields_used_updated:
                    await self.db.commit()
                    logger.info(
                        f"[MiningWorkflow] V-19.1 fields_used populated for "
                        f"{fields_used_updated} alphas"
                    )
            except Exception as _ex:
                logger.warning(f"[MiningWorkflow] V-19.1 fields_used loop failed: {_ex}")

            # Plan v5+ §B7 post-fix (2026-05-06): refresh hypothesis denormalized
            # stats AFTER outer commit. Pre-V-19.5 the refresh ran inside
            # _process_hypothesis_feedback (called from node_save_results, BEFORE
            # outer commit) so the JOIN against alphas saw 0 rows for the round
            # that just landed — Hypothesis row stayed alpha_count=0 / pass_count=0
            # even when PROMOTED. Doing it here means committed alpha rows are
            # visible.
            try:
                touched_hids = set()
                for alpha_result in result.get("generated_alphas", []):
                    hid = getattr(alpha_result, "hypothesis_id", None)
                    if hid is not None:
                        touched_hids.add(hid)
                # V-26.26 (2026-05-13): also collect hypothesis_ids from the
                # FAIL path. Previously only generated_alphas (PASS / REJECTED
                # rows in `alphas` table) triggered refresh_stats — alpha_failures
                # writes were ignored. Combined with V-26.13 (refresh_stats now
                # counts alpha_failures), this lets a hypothesis whose attempts
                # all hit validation / sim errors still advance PROPOSED→ACTIVE
                # and eventually trigger B6 abandon.
                for failure in result.get("failures", []):
                    fhid = getattr(failure, "hypothesis_id", None)
                    if fhid is not None:
                        touched_hids.add(fhid)
                if touched_hids:
                    from backend.services.hypothesis_service import HypothesisService
                    svc = HypothesisService(self.db)
                    refreshed = 0
                    for hid in touched_hids:
                        try:
                            await svc.refresh_stats(hid)
                            refreshed += 1
                        except Exception as _e:
                            logger.warning(
                                f"[MiningWorkflow] V-19.5 refresh_stats failed for "
                                f"hypothesis_id={hid}: {_e}"
                            )
                    if refreshed:
                        await self.db.commit()
                        logger.info(
                            f"[MiningWorkflow] V-19.5 refreshed stats for "
                            f"{refreshed} hypotheses"
                        )
            except Exception as _ex:
                logger.warning(
                    f"[MiningWorkflow] V-19.5 hypothesis stats refresh loop failed: {_ex}"
                )

            # B7: post-commit, alpha.id is populated — enqueue async can_submit
            # refresh for each new PASS/PROV alpha. 30s countdown lets BRAIN
            # finish async checks before we re-fetch.
            if can_submit_refresh_targets:
                from backend.tasks.refresh_tasks import enqueue_can_submit_refresh
                for a in can_submit_refresh_targets:
                    enqueue_can_submit_refresh(a.id, a.alpha_id, countdown=30)
                logger.info(
                    f"[MiningWorkflow] enqueued {len(can_submit_refresh_targets)} can_submit refreshes"
                )
            
        except Exception as e:
            # V-19.2 (2026-05-05): with per-row savepoints this outer except
            # should now only fire for catastrophic issues (session crashed,
            # connection lost, etc.) — per-row IntegrityError is handled in
            # the savepoint blocks above. We still log defensively.
            import traceback as _tb
            n_alphas = len(result.get("generated_alphas", []) or [])
            n_failures = len(result.get("failures", []) or [])
            logger.error(
                f"[MiningWorkflow] V-19.2 outer persistence FAILED: "
                f"{type(e).__name__}: {e}\n"
                f"  task_id={getattr(task, 'id', None)}\n"
                f"  generated_alphas={n_alphas}, failures={n_failures}\n"
                f"  factor_tier={factor_tier}, dataset={dataset_id}\n"
                f"  traceback:\n{_tb.format_exc()}"
            )
            try:
                from backend.agents.graph.persistence_errors import (
                    log_persistence_error,
                )
                log_persistence_error(
                    task_id=getattr(task, "id", None),
                    phase="outer_commit",
                    exc=e,
                    extra={
                        "n_alphas": n_alphas,
                        "n_failures": n_failures,
                        "factor_tier": factor_tier,
                        "dataset_id": dataset_id,
                        "traceback_inline": _tb.format_exc(),
                    },
                )
            except Exception:
                pass
            try:
                await self.db.rollback()
            except Exception:
                pass

        return result


def create_mining_graph(
    db: AsyncSession,
    brain: BrainAdapter = None,
    llm_service: LLMService = None
) -> MiningWorkflow:
    """
    Factory function to create mining workflow.
    
    Usage:
        workflow = create_mining_graph(db, brain)
        result = await workflow.run(task, dataset_id, fields, operators)
    """
    return MiningWorkflow(
        db=db,
        brain=brain,
        llm_service=llm_service
    )
