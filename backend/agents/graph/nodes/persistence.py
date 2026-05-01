"""
Persistence nodes for LangGraph workflow.

Contains:
- node_save_results: Save alpha results to database
"""

from typing import Dict
from loguru import logger
from langchain_core.runnables import RunnableConfig

from backend.agents.graph.state import MiningState, AlphaResult, FailureRecord
from backend.agents.graph.nodes.base import record_trace
from backend.agents.graph.early_stop import should_stop_early, summarise_round


# =============================================================================
# NODE: Save Results
# =============================================================================

async def node_save_results(state: MiningState, config: RunnableConfig = None) -> Dict:
    """
    Batch process and save ALL results (Successes and Failures).
    
    Input State:
        - pending_alphas
    
    Output Updates:
        - generated_alphas (appends successes)
        - failures (appends failures)
        - pending_alphas (cleared)
        - trace_steps
    """
    node_name = "SAVE_RESULTS"
    trace_service = config.get("configurable", {}).get("trace_service") if config else None
    
    success_batch = []
    fail_batch = []
    
    logger.info(f"[{node_name}] Starting batch save | total={len(state.pending_alphas)}")
    
    for alpha in state.pending_alphas:
        # W6: persist PASS and PASS_PROVISIONAL alphas. Provisional entries
        # become seeds for the few-shot pool consumed by next-round LLM
        # generation (rag_service.get_recent_pass_examples).
        if alpha.quality_status in ("PASS", "PASS_PROVISIONAL"):
            res = AlphaResult(
                expression=alpha.expression,
                hypothesis=alpha.hypothesis,
                explanation=alpha.explanation,
                alpha_id=alpha.alpha_id,
                metrics=alpha.metrics,
                quality_status=alpha.quality_status,
                # Tier system: propagate lineage + wrapper provenance to DB row
                parent_alpha_id=alpha.parent_alpha_id,
                wrapper_kind=alpha.wrapper_kind,
            )
            success_batch.append(res)
            logger.info(
                f"[{node_name}] Alpha Saved | id={alpha.alpha_id} "
                f"status={alpha.quality_status}"
            )

        else:
            # Determine error type and message
            err_type = "UNKNOWN"
            err_msg = "Unknown error"
            
            if alpha.is_valid is False:
                err_type = "SYNTAX_ERROR"
                err_msg = alpha.validation_error or "Syntax Error"
            elif alpha.is_simulated and not alpha.simulation_success:
                err_type = "SIMULATION_ERROR"
                err_msg = alpha.simulation_error or "Simulation Failed"
            elif alpha.quality_status == "FAIL":
                err_type = "QUALITY_CHECK_FAILED"
                err_msg = "Metrics below threshold"
            else:
                err_type = "OTHER"
                err_msg = "Unknown failure"
            
            rec = FailureRecord(
                expression=alpha.expression,
                error_type=err_type,
                error_message=err_msg,
                details={"metrics": alpha.metrics, "hypothesis": alpha.hypothesis}
            )
            fail_batch.append(rec)
    
    # W1: round-level history + early-stop policy
    pass_count = sum(1 for a in state.pending_alphas if a.quality_status == "PASS")
    optimize_count = sum(
        1 for a in state.pending_alphas
        if a.quality_status in ("OPTIMIZE", "PASS_PROVISIONAL")
    )
    fail_count = sum(1 for a in state.pending_alphas if a.quality_status in ("FAIL", "REJECT"))
    round_summary = summarise_round(state.pending_alphas, pass_count, optimize_count, fail_count)
    round_summary["round_index"] = state.current_round + 1
    new_round_history = state.round_history + [round_summary]

    # Look at max_iterations from RunnableConfig if available; default 10
    max_iter_default = 10
    try:
        max_iter = (config.get("configurable", {}) if config else {}).get("max_iterations") or max_iter_default
    except Exception:
        max_iter = max_iter_default

    early_stop, early_stop_reason = should_stop_early(new_round_history, int(max_iter))
    if early_stop:
        logger.warning(
            f"[{node_name}] Early stop triggered after round "
            f"{round_summary['round_index']}: {early_stop_reason}"
        )

    # Record trace
    if trace_service:
        await record_trace(
            state, trace_service, node_name,
            {},
            {
                "saved": len(success_batch),
                "failed": len(fail_batch),
                "round_summary": round_summary,
                "early_stopped": early_stop,
                "early_stop_reason": early_stop_reason,
            },
            0,
            "SUCCESS",
            None
        )

    return {
        "generated_alphas": state.generated_alphas + success_batch,
        "failures": state.failures + fail_batch,
        "pending_alphas": [],
        "current_alpha_index": 0,
        "round_history": new_round_history,
        "current_round": state.current_round + 1,
        "early_stopped": early_stop,
        "early_stop_reason": early_stop_reason,
    }
