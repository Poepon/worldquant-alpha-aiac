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
    hypothesis_id: Optional[int] = None,
) -> List["AlphaResult"]:
    """For T2/T3: write Alpha rows directly to DB at save_results time
    rather than buffering in state.generated_alphas until workflow returns.

    This makes PASSes visible to the frontend / FactorLibrary stats almost
    instantly per seed, and prevents catastrophic data loss if a long-
    running T2 task (1+ hour for 8 seeds) crashes mid-loop.

    Returns AlphaResult list with persisted=True + db_id set, so
    workflow.run_with_persistence's batch path skips them.
    """
    from backend.alpha_semantic_validator import (
        compute_expression_hash,
        AlphaSemanticValidator,
    )
    from backend.models import Alpha

    # V-17 (2026-05-04): mirrors workflow.run_with_persistence — populate
    # fields_used so cross-dataset analytics work for T2/T3 incremental saves.
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

    # V-19.2 (2026-05-05): per-row SAVEPOINT — see workflow.run_with_persistence
    # for full rationale. One bad row no longer rolls back the whole seed batch.
    from backend.agents.graph.persistence_errors import log_persistence_error

    # V-19.3 (2026-05-06): pre-batch SELECT existing alpha_ids to short-circuit
    # cross-task duplicates BEFORE the savepoint INSERT. Same rationale as
    # workflow.run_with_persistence — sign-flip retry can produce expressions
    # that BRAIN normalizes to an alpha_id another task already owns.
    from sqlalchemy import select as _sa_select
    candidate_alpha_ids = [
        a.alpha_id for a in pending_alphas
        if a.alpha_id and a.quality_status in ("PASS", "PASS_PROVISIONAL")
    ]
    cross_task_dup_ids: set = set()
    if candidate_alpha_ids:
        try:
            r = await db_session.execute(
                _sa_select(Alpha.alpha_id).where(
                    Alpha.alpha_id.in_(candidate_alpha_ids)
                )
            )
            cross_task_dup_ids = {row[0] for row in r.fetchall()}
        except Exception as _e:
            logger.warning(
                f"[_incremental_save_alphas] V-19.3 cross-task dedup query failed: {_e}"
            )

    snapshot_at = datetime.utcnow()
    out: List[AlphaResult] = []
    inserted_alpha_ids: List[str] = []  # alpha_ids that successfully landed
    cross_task_skipped: List[str] = []  # alpha_ids skipped as cross-task dups
    for alpha in pending_alphas:
        if alpha.quality_status not in ("PASS", "PASS_PROVISIONAL"):
            continue
        # V-19.3: cross-task duplicate skip
        if alpha.alpha_id and alpha.alpha_id in cross_task_dup_ids:
            cross_task_skipped.append(alpha.alpha_id)
            logger.info(
                f"[_incremental_save_alphas] V-19.3 skip cross-task duplicate "
                f"alpha_id={alpha.alpha_id} (already owned by another task) "
                f"expr={(alpha.expression or '')[:100]!r}"
            )
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
            # Phase 2 B4: typed Hypothesis link
            hypothesis_id=hypothesis_id,
        )
        try:
            async with db_session.begin_nested():
                db_session.add(row)
                await db_session.flush()
            if alpha.alpha_id:
                inserted_alpha_ids.append(alpha.alpha_id)
        except Exception as e:
            import traceback as _tb
            logger.error(
                f"[_incremental_save_alphas] V-19.2 alpha INSERT savepoint rolled back: "
                f"{type(e).__name__}: {e} | alpha_id={alpha.alpha_id}"
            )
            log_persistence_error(
                task_id=task_id,
                phase="incremental_alpha_insert",
                exc=e,
                alpha_id=alpha.alpha_id,
                expression=alpha.expression,
                quality_status=alpha.quality_status,
                extra={
                    "factor_tier": factor_tier,
                    "dataset_id": dataset_id,
                    "traceback_inline": _tb.format_exc(),
                },
            )

    # Outer commit releases all successful savepoints. Pre-V-19.2 a single
    # failed row aborted everything; now only the failed savepoint is gone.
    try:
        await db_session.commit()
    except Exception as e:
        import traceback as _tb
        logger.error(
            f"[_incremental_save_alphas] V-19.2 outer commit failed: "
            f"{type(e).__name__}: {e}"
        )
        log_persistence_error(
            task_id=task_id,
            phase="incremental_outer_commit",
            exc=e,
            extra={
                "factor_tier": factor_tier,
                "dataset_id": dataset_id,
                "n_pending": len(pending_alphas),
                "traceback_inline": _tb.format_exc(),
            },
        )
        try:
            await db_session.rollback()
        except Exception:
            pass
        # Empty out — no rows landed. Caller falls back to buffered path.
        return []

    # V-19.1 (2026-05-05): post-commit fields_used population for T2/T3
    # incremental path. V-19.2: scope to only those that actually inserted —
    # alpha_ids whose savepoint rolled back are not in DB so the UPDATE would
    # be a no-op anyway, but skipping them keeps the log tidy.
    from sqlalchemy import update as _sa_update, select
    inserted_set = set(inserted_alpha_ids)
    fields_used_updated = 0
    for alpha in pending_alphas:
        if alpha.quality_status not in ("PASS", "PASS_PROVISIONAL"):
            continue
        if not alpha.alpha_id or not alpha.expression:
            continue
        if alpha.alpha_id not in inserted_set:
            continue
        try:
            fids = _extract_used_fields(alpha.expression)
            if not fids:
                continue
            await db_session.execute(
                _sa_update(Alpha)
                .where(Alpha.task_id == task_id, Alpha.alpha_id == alpha.alpha_id)
                .values(fields_used=fids)
            )
            fields_used_updated += 1
        except Exception as _e:
            logger.warning(
                f"[_incremental_save_alphas] V-19.1 fields_used update failed for "
                f"alpha_id={alpha.alpha_id}: {_e}"
            )
    if fields_used_updated:
        try:
            await db_session.commit()
        except Exception as _e:
            logger.warning(f"[_incremental_save_alphas] V-19.1 commit failed: {_e}")

    # Build AlphaResult list. V-19.2: persisted=True only for rows that
    # actually inserted; failed savepoints come back persisted=False so the
    # workflow's batch path (also savepoint-protected now) gets a retry —
    # at worst it logs the same error twice, never silently drops.
    for alpha in pending_alphas:
        if alpha.quality_status not in ("PASS", "PASS_PROVISIONAL"):
            continue
        landed = bool(alpha.alpha_id and alpha.alpha_id in inserted_set)
        db_id = None
        if landed:
            stmt = select(Alpha).where(
                Alpha.task_id == task_id, Alpha.alpha_id == alpha.alpha_id
            ).limit(1)
            result = await db_session.execute(stmt)
            row = result.scalar_one_or_none()
            db_id = row.id if row else None
        out.append(AlphaResult(
            expression=alpha.expression,
            hypothesis=alpha.hypothesis,
            explanation=alpha.explanation,
            alpha_id=alpha.alpha_id,
            metrics=alpha.metrics,
            quality_status=alpha.quality_status,
            parent_alpha_id=alpha.parent_alpha_id,
            wrapper_kind=alpha.wrapper_kind,
            persisted=landed,
            db_id=db_id,
            hypothesis_id=hypothesis_id,
        ))
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

    # Plan v5+ §Phase 2 B4: typed Hypothesis link. Captured from state at the
    # moment alphas are saved so each AlphaResult / Alpha row knows which
    # hypothesis it derived from. None when level<2 / propose persistence
    # failed — workflow's INSERT path writes alpha.hypothesis_id=NULL in
    # that case (legacy compat).
    #
    # Smoke-test (2026-05-06) revealed LangGraph scalar field propagation
    # can drop current_hypothesis_id while the list current_hypothesis_ids
    # still propagates. Fallback to list[0] for B4 robustness.
    current_hypothesis_id = getattr(state, "current_hypothesis_id", None)
    if current_hypothesis_id is None:
        _hids = getattr(state, "current_hypothesis_ids", None) or []
        if _hids:
            current_hypothesis_id = _hids[0]

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
                hypothesis_id=current_hypothesis_id,
            )
            for alpha in state.pending_alphas:
                if alpha.quality_status in ("PASS", "PASS_PROVISIONAL"):
                    logger.info(
                        f"[{node_name}] Alpha Saved (incremental) | id={alpha.alpha_id} "
                        f"status={alpha.quality_status} tier=T{state.factor_tier} "
                        f"hypothesis_id={current_hypothesis_id}"
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
                    hypothesis_id=current_hypothesis_id,
                )
                success_batch.append(res)
                logger.info(
                    f"[{node_name}] Alpha Saved (buffered) | id={alpha.alpha_id} "
                    f"status={alpha.quality_status} hypothesis_id={current_hypothesis_id}"
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

    # ------------------------------------------------------------------
    # Plan v5+ §Phase 2 B5/B6 — typed Hypothesis feedback + abandon
    # ------------------------------------------------------------------
    # When state.current_hypothesis_ids is populated (level≥2), classify
    # each hypothesis's round outcome by attribution and update lifecycle.
    # Triggers mark_active / mark_promoted / mark_abandoned via service.
    new_hypothesis_round_history = dict(state.hypothesis_round_history or {})
    if state.current_hypothesis_ids:
        try:
            new_hypothesis_round_history = await _process_hypothesis_feedback(
                state=state,
                round_index=round_summary["round_index"],
                pending_alphas=state.pending_alphas,
                history_so_far=new_hypothesis_round_history,
                trace_service=trace_service,
            )
        except Exception as _ex:
            logger.warning(
                f"[{node_name}] B5 hypothesis feedback failed (non-fatal): {_ex}"
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
        "hypothesis_round_history": new_hypothesis_round_history,
    }


# =============================================================================
# Plan v5+ §Phase 2 B5 — Hypothesis feedback helper
# =============================================================================

async def _process_hypothesis_feedback(
    *,
    state,
    round_index: int,
    pending_alphas: List,
    history_so_far: Dict[int, List[Dict]],
    trace_service=None,
) -> Dict[int, List[Dict]]:
    """Round-level lifecycle update for every hypothesis proposed this round.

    Per Plan v5+ §B5 the long-term goal is to call Experiment2Feedback (LLM-
    based attribution). For now we use a heuristic classifier
    (early_stop.classify_attribution) which is cheaper and sufficient for
    the abandon trigger — LLM upgrade is backlog (B5 v2).

    For each hypothesis_id in state.current_hypothesis_ids:
      1. Compute round counts: alpha_count, pass_count, syntax_fail,
         simulate_fail, quality_fail, best_sharpe
      2. Classify attribution via heuristic
      3. Append entry to history_so_far[hid]
      4. Call lifecycle transitions:
           - alpha_count > 0  → mark_active (idempotent: PROPOSED→ACTIVE)
           - pass_count > 0   → mark_promoted (PROPOSED|ACTIVE→PROMOTED)
           - should_abandon_hypothesis(history) → mark_abandoned
    """
    from backend.agents.graph.early_stop import (
        classify_attribution,
        should_abandon_hypothesis,
    )
    from backend.database import AsyncSessionLocal
    from backend.services.hypothesis_service import HypothesisService

    hids = list(state.current_hypothesis_ids or [])
    if not hids:
        return history_so_far

    # Map hypothesis_id → list of pending_alphas linked to it. The LLM
    # writes h["hypothesis_id"] back into each parsed dict (B3); the alpha
    # candidates carry alpha.hypothesis text but not the id, so for now we
    # attribute ALL alphas in this round to the PRIMARY hypothesis_id.
    # When B3 evolves to per-alpha hypothesis tracking this becomes a
    # filter; until then it's "primary gets the round's outcome".
    primary_hid = state.current_hypothesis_id or hids[0]

    # Aggregate counts across this round's alphas
    alpha_count = len(pending_alphas)
    pass_count = sum(
        1 for a in pending_alphas
        if a.quality_status in ("PASS", "PASS_PROVISIONAL")
    )
    syntax_fail = sum(1 for a in pending_alphas if a.is_valid is False)
    simulate_fail = sum(
        1 for a in pending_alphas
        if a.is_valid is not False and a.is_simulated and not a.simulation_success
    )
    quality_fail = sum(
        1 for a in pending_alphas
        if a.is_valid is not False
        and (a.is_simulated and a.simulation_success)
        and a.quality_status in ("FAIL", "REJECT")
    )
    best_sharpe = 0.0
    for a in pending_alphas:
        m = getattr(a, "metrics", None) or {}
        sh = m.get("sharpe")
        if sh is not None:
            try:
                best_sharpe = max(best_sharpe, float(sh))
            except Exception:
                pass

    attribution = classify_attribution(
        alpha_count=alpha_count,
        pass_count=pass_count,
        syntax_fail_count=syntax_fail,
        simulate_fail_count=simulate_fail,
        quality_fail_count=quality_fail,
    )
    entry = {
        "round_index": round_index,
        "alpha_count": alpha_count,
        "pass_count": pass_count,
        "syntax_fail_count": syntax_fail,
        "simulate_fail_count": simulate_fail,
        "quality_fail_count": quality_fail,
        "attribution": attribution,
        "best_sharpe": best_sharpe,
    }

    # Append to all hids proposed this round (every emitted hypothesis
    # gets the same attribution since they shared a code_gen pass)
    history_out = dict(history_so_far)
    for hid in hids:
        history_out[hid] = list(history_out.get(hid, [])) + [entry]

    # Lifecycle DB updates — fresh session so we don't conflict with the
    # incremental persistence path's session transaction state.
    #
    # V-19.6 (2026-05-06) ghost-promotion fix: B4 links every alpha in the
    # round to the PRIMARY hypothesis (state.current_hypothesis_id). Non-
    # primary hypotheses in current_hypothesis_ids share the round's
    # code_gen pass but never have alphas linked to them. Pre-fix every hid
    # got mark_promoted on any round PASS — producing "ghost PROMOTED" rows
    # with alpha_count=0 (e.g. task 143's hids 210/211/212). Post-fix:
    #   - mark_active: all hids (they all got tried via shared code_gen)
    #   - mark_promoted: ONLY primary (only primary owns the PASS alphas)
    #   - mark_abandoned: ONLY primary (consistent with promotion ownership)
    abandoned: List[int] = []
    promoted: List[int] = []
    activated: List[int] = []
    try:
        async with AsyncSessionLocal() as _hdb:
            svc = HypothesisService(_hdb)
            for hid in hids:
                if alpha_count > 0:
                    if await svc.mark_active(hid):
                        activated.append(hid)
            # Promote / abandon only the primary — non-primary hypotheses
            # don't have alpha rows attributed to them, so promoting them
            # would be semantically wrong (PROMOTED with alpha_count=0).
            if pass_count > 0 and primary_hid is not None:
                if await svc.mark_promoted(primary_hid):
                    promoted.append(primary_hid)
            # Abandonment: only the primary's history is authoritative since
            # all hids share the same round entry (same code_gen pass). And
            # only primary owns alphas, so only primary should ever be
            # ABANDONED. abandon fires when last N rounds had 0 PASS +
            # attribution=hypothesis (pass_count for THIS round can be
            # anything — should_abandon checks the cumulative history).
            if primary_hid is not None:
                should_abandon, abandon_reason = should_abandon_hypothesis(
                    history_out.get(primary_hid, [])
                )
                if should_abandon:
                    if await svc.mark_abandoned(primary_hid, reason=abandon_reason):
                        abandoned.append(primary_hid)
            # V-19.5 (2026-05-06): NO refresh_stats here. This helper runs
            # inside node_save_results, BEFORE workflow.run_with_persistence's
            # outer commit. Querying alphas at this point sees 0 rows for
            # the current round (uncommitted), so refresh_stats would
            # incorrectly write 0 to alpha_count/pass_count/sharpe_max.
            # The authoritative refresh now happens post-commit in
            # workflow.run_with_persistence.
            await _hdb.commit()
    except Exception as _ex:
        logger.warning(
            f"[B5] hypothesis lifecycle DB update failed (non-fatal): {_ex}"
        )

    logger.info(
        f"[B5] hypothesis feedback round={round_index} primary={primary_hid} "
        f"attribution={attribution} alphas={alpha_count} pass={pass_count} "
        f"activated={activated} promoted={promoted} abandoned={abandoned}"
    )

    # Trace step (HYPOTHESIS_FEEDBACK) — use the same record_trace helper as
    # the rest of the workflow nodes so step_order / persistence stays
    # consistent.
    if trace_service:
        try:
            await record_trace(
                state, trace_service, "HYPOTHESIS_FEEDBACK",
                {
                    "primary_hypothesis_id": primary_hid,
                    "all_hypothesis_ids": hids,
                    "round_index": round_index,
                },
                {
                    "attribution": attribution,
                    "round_summary": entry,
                    "activated": activated,
                    "promoted": promoted,
                    "abandoned": abandoned,
                },
                0,
                "SUCCESS",
                None,
            )
        except Exception as _ex:
            logger.warning(f"[B5] record_trace failed: {_ex}")

    return history_out
