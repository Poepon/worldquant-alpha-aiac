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
from backend.agents.graph.edges import (
    route_after_validate,
    route_check_error
)
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
    """T1 task: feature flag picks LLM-guided vs legacy hypothesis path."""
    if getattr(settings, "T1_USE_LLM_GUIDED_STRATEGY", True):
        return "t1_strategy_select"
    return "hypothesis"


def _route_after_seed_load(state: MiningState) -> str:
    """If tier_seed_load found insufficient seeds, end the run."""
    if getattr(state, "should_stop", False):
        return "END"
    if not state.tier_seeds:
        return "END"
    return "tier_strategy_select"


def _route_after_save_results(state: MiningState) -> str:
    """T1 → END (mining_agent's outer loop handles multi-round).
    T2/T3 → loop to next seed if any remain, else END."""
    if state.factor_tier and state.factor_tier > 1:
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

        # T1 legacy arm (original)
        workflow.add_edge("hypothesis", "code_gen")
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
        initial_state = MiningState(
            task_id=task.id,
            region=task.region,
            universe=task.universe,
            dataset_id=dataset_id,
            fields=fields,
            operators=operators,
            num_alphas_target=num_alphas,
            factor_tier=factor_tier,
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
            from backend.alpha_semantic_validator import compute_expression_hash

            # PR2: tier from result (state.factor_tier, propagated through run()).
            task_factor_tier = result.get("factor_tier", factor_tier)
            from datetime import datetime as _dt
            task_metrics_snapshot_at = _dt.utcnow()

            for alpha_result in result.get("generated_alphas", []):
                try:
                    # P0-fix-2: Compute expression hash for DB-level deduplication
                    expr_hash = compute_expression_hash(alpha_result.expression) if alpha_result.expression else None

                    alpha = Alpha(
                        task_id=task.id,
                        run_id=run_id,
                        alpha_id=alpha_result.alpha_id,
                        expression=alpha_result.expression,
                        expression_hash=expr_hash,  # P0-fix-2: Enable DB deduplication
                        hypothesis=alpha_result.hypothesis,
                        logic_explanation=alpha_result.explanation,
                        region=task.region,
                        universe=task.universe,
                        dataset_id=dataset_id,
                        quality_status=alpha_result.quality_status,
                        metrics=alpha_result.metrics,
                        # Tier system: per-task factor_tier + per-alpha lineage.
                        factor_tier=task_factor_tier,
                        parent_alpha_id=getattr(alpha_result, "parent_alpha_id", None),
                        metrics_snapshot_at=task_metrics_snapshot_at,
                    )
                    self.db.add(alpha)
                except Exception as e:
                    logger.warning(f"[MiningWorkflow] Failed to add alpha: {e}")
            
            # Persist failures
            for failure in result.get("failures", []):
                try:
                    fail_record = AlphaFailure(
                        task_id=task.id,
                        run_id=run_id,
                        expression=failure.expression[:2000] if failure.expression else None,  # Limit length
                        error_type=failure.error_type,
                        error_message=failure.error_message[:500] if failure.error_message else None  # Limit length
                    )
                    self.db.add(fail_record)
                except Exception as e:
                    logger.warning(f"[MiningWorkflow] Failed to add failure record: {e}")
            
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
                        self.db.add(step)
                    except Exception as e:
                        logger.warning(f"[MiningWorkflow] Failed to add trace step: {e}")
            
            await self.db.commit()
            logger.info(f"[MiningWorkflow] 持久化完成 | task={task.id}")
            
        except Exception as e:
            logger.error(f"[MiningWorkflow] Persistence failed: {e}")
            # Rollback failed transaction to allow subsequent operations
            try:
                await self.db.rollback()
            except Exception:
                pass
            # Don't raise - return result anyway so mining continues
        
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
