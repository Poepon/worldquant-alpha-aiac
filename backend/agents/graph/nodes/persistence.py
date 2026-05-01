"""
Persistence nodes for LangGraph workflow.

Contains:
- node_save_results: Save alpha results to database
"""

from datetime import datetime
from typing import Dict, List, Optional
from loguru import logger
from langchain_core.runnables import RunnableConfig

from backend.agents.graph.state import MiningState, AlphaResult, FailureRecord
from backend.agents.graph.nodes.base import record_trace
from backend.agents.graph.early_stop import should_stop_early, summarise_round


# =============================================================================
# PR7 — Incremental persistence helpers (T2/T3)
# =============================================================================

async def _incremental_save_alphas(
    db_session,
    task_id: int,
    run_id: Optional[int],
    region: str,
    universe: str,
    dataset_id: str,
    factor_tier: int,
    pending_alphas: List,
) -> List["AlphaResult"]:
    """For T2/T3: write Alpha rows directly to DB at save_results time
    rather than buffering in state.generated_alphas until workflow returns.

    This makes PASSes visible to the frontend / FactorLibrary stats almost
    instantly per seed, and prevents catastrophic data loss if a long-
    running T2 task (1+ hour for 8 seeds) crashes mid-loop.

    Returns AlphaResult list with persisted=True + db_id set, so
    workflow.run_with_persistence's batch path skips them.
    """
    from backend.alpha_semantic_validator import compute_expression_hash
    from backend.models import Alpha

    snapshot_at = datetime.utcnow()
    out: List[AlphaResult] = []
    for alpha in pending_alphas:
        if alpha.quality_status not in ("PASS", "PASS_PROVISIONAL"):
            continue
        metrics_dict = alpha.metrics if isinstance(alpha.metrics, dict) else {}
        expr_hash = compute_expression_hash(alpha.expression) if alpha.expression else None

        row = Alpha(
            task_id=task_id,
            run_id=run_id,
            alpha_id=alpha.alpha_id,
            expression=alpha.expression,
            expression_hash=expr_hash,
            hypothesis=alpha.hypothesis,
            logic_explanation=alpha.explanation,
            region=region,
            universe=universe,
            dataset_id=dataset_id,
            quality_status=alpha.quality_status,
            metrics=alpha.metrics,
            is_sharpe=metrics_dict.get("sharpe"),
            is_fitness=metrics_dict.get("fitness"),
            is_turnover=metrics_dict.get("turnover"),
            is_returns=metrics_dict.get("returns"),
            is_drawdown=metrics_dict.get("drawdown"),
            is_margin=metrics_dict.get("margin"),
            is_long_count=metrics_dict.get("longCount"),
            is_short_count=metrics_dict.get("shortCount"),
            factor_tier=factor_tier,
            parent_alpha_id=alpha.parent_alpha_id,
            metrics_snapshot_at=snapshot_at,
        )
        db_session.add(row)
    # Flush + commit per seed batch so frontend sees them immediately and a
    # crash mid-task only loses subsequent seeds, not committed PASSes.
    await db_session.flush()
    await db_session.commit()

    # Re-iterate to build the AlphaResult list with the now-assigned ids.
    # We need to query rows back since we don't keep references after flush.
    # Simpler: query by task_id + alpha_id (BRAIN id, unique).
    from sqlalchemy import select
    for alpha in pending_alphas:
        if alpha.quality_status not in ("PASS", "PASS_PROVISIONAL"):
            continue
        if alpha.alpha_id:
            stmt = select(Alpha).where(
                Alpha.task_id == task_id, Alpha.alpha_id == alpha.alpha_id
            ).limit(1)
            result = await db_session.execute(stmt)
            row = result.scalar_one_or_none()
            db_id = row.id if row else None
        else:
            db_id = None
        out.append(AlphaResult(
            expression=alpha.expression,
            hypothesis=alpha.hypothesis,
            explanation=alpha.explanation,
            alpha_id=alpha.alpha_id,
            metrics=alpha.metrics,
            quality_status=alpha.quality_status,
            parent_alpha_id=alpha.parent_alpha_id,
            wrapper_kind=alpha.wrapper_kind,
            persisted=True,
            db_id=db_id,
        ))
        # B7: schedule can_submit refresh ~30s out so BRAIN has time to finish
        # async checks (CONCENTRATED_WEIGHT, LOW_SUB_UNIVERSE_SHARPE).
        if db_id is not None and alpha.alpha_id:
            from backend.tasks.refresh_tasks import enqueue_can_submit_refresh
            enqueue_can_submit_refresh(db_id, alpha.alpha_id, countdown=30)
    return out


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

    PR7 — for T2/T3 with T2_INCREMENTAL_PERSISTENCE=True, writes Alpha rows
    immediately instead of only buffering. workflow.run_with_persistence's
    end-of-task batch loop skips already-persisted rows.
    """
    node_name = "SAVE_RESULTS"
    configurable = (config.get("configurable", {}) if config else {}) or {}
    trace_service = configurable.get("trace_service")

    success_batch: List[AlphaResult] = []
    fail_batch = []

    logger.info(f"[{node_name}] Starting batch save | total={len(state.pending_alphas)}")

    # PR7 — incremental persistence path for T2/T3
    from backend.config import settings as _settings
    use_incremental = (
        getattr(_settings, "T2_INCREMENTAL_PERSISTENCE", True)
        and (getattr(state, "factor_tier", None) in (2, 3))
        and configurable.get("db_session") is not None
    )

    if use_incremental:
        try:
            success_batch = await _incremental_save_alphas(
                db_session=configurable["db_session"],
                task_id=state.task_id,
                run_id=configurable.get("run_id"),
                region=state.region,
                universe=state.universe,
                dataset_id=state.dataset_id,
                factor_tier=state.factor_tier,
                pending_alphas=state.pending_alphas,
            )
            for alpha in state.pending_alphas:
                if alpha.quality_status in ("PASS", "PASS_PROVISIONAL"):
                    logger.info(
                        f"[{node_name}] Alpha Saved (incremental) | id={alpha.alpha_id} "
                        f"status={alpha.quality_status} tier=T{state.factor_tier}"
                    )
        except Exception as e:
            logger.error(f"[{node_name}] incremental persistence failed: {e}; "
                         "falling back to in-memory buffering")
            success_batch = []
            use_incremental = False

    if not use_incremental:
        # Original behavior — buffer in state.generated_alphas; workflow
        # writes to DB after returning.
        for alpha in state.pending_alphas:
            if alpha.quality_status in ("PASS", "PASS_PROVISIONAL"):
                res = AlphaResult(
                    expression=alpha.expression,
                    hypothesis=alpha.hypothesis,
                    explanation=alpha.explanation,
                    alpha_id=alpha.alpha_id,
                    metrics=alpha.metrics,
                    quality_status=alpha.quality_status,
                    parent_alpha_id=alpha.parent_alpha_id,
                    wrapper_kind=alpha.wrapper_kind,
                )
                success_batch.append(res)
                logger.info(
                    f"[{node_name}] Alpha Saved (buffered) | id={alpha.alpha_id} "
                    f"status={alpha.quality_status}"
                )

    # Failure path — buffered the same way regardless of incremental /
    # batch mode, since AlphaFailure rows are bulk-written by
    # run_with_persistence. (Could be made incremental too in a follow-up.)
    for alpha in state.pending_alphas:
        if alpha.quality_status in ("PASS", "PASS_PROVISIONAL"):
            continue

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
