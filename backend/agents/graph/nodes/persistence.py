"""
Persistence nodes for LangGraph workflow.

Contains:
- node_save_results: Save alpha results to database
- is_expression_persisted_in_task: V-19.8 RESUME dedup helper
"""

from datetime import datetime
from typing import Dict, List, Optional
from loguru import logger
from langchain_core.runnables import RunnableConfig

from backend.agents.graph.state import MiningState, AlphaResult, FailureRecord
from backend.agents.graph.nodes.base import record_trace
from backend.agents.graph.early_stop import should_stop_early, summarise_round


# V-26.88 (2026-05-13): module-level singleton AlphaSemanticValidator used
# only for fields_used extraction (no field/type strictness). The class
# is heavyweight at construction time — operator registry pulls from the
# DB-loaded module-level cache, but we still pay attribute-init cost per
# alpha. Reusing one stateless instance for the "give me used_fields"
# call drops per-alpha overhead from ~milliseconds to a function call.
#
# `_FIELDS_USED_VALIDATOR` is intentionally created with empty fields +
# no strict checks so it never raises on unknown fields/types — it only
# walks the AST and returns the field set. Thread-safe because validate()
# is pure (no instance mutation; it builds a local ValidationResult).
_FIELDS_USED_VALIDATOR = None  # lazily constructed in _get_fields_used_validator


def _get_fields_used_validator():
    """Return the module-level fields-extraction validator. Lazy init so
    importing this module doesn't pay validator construction cost when
    the persistence path isn't used (e.g. in unit tests that mock around it)."""
    global _FIELDS_USED_VALIDATOR
    if _FIELDS_USED_VALIDATOR is None:
        from backend.alpha_semantic_validator import AlphaSemanticValidator
        _FIELDS_USED_VALIDATOR = AlphaSemanticValidator(
            fields=[], operators=None,
            strict_field_check=False, strict_type_check=False,
        )
    return _FIELDS_USED_VALIDATOR


def _resolve_metrics_snapshot_at(metrics_dict: Dict, fallback: datetime) -> datetime:
    """V-26.91: pick the best available timestamp for when this alpha's
    metrics were actually produced.

    Order of preference:
      1. metrics["dateModified"] — BRAIN echoes this on the alpha resource.
      2. metrics["sim_completed_at"] — set by some retry paths.
      3. fresh `datetime.utcnow()` — better than batch-start but still
         a few seconds late for early-bucket alphas.
      4. `fallback` — only used when steps 1-3 all fail unexpectedly.

    Returns a naive UTC datetime (matches the existing column shape).
    """
    if not isinstance(metrics_dict, dict):
        return fallback
    for key in ("dateModified", "sim_completed_at"):
        raw = metrics_dict.get(key)
        if not raw:
            continue
        # Tolerate datetime, ISO-8601 string, or epoch number.
        if isinstance(raw, datetime):
            return raw.replace(tzinfo=None)
        if isinstance(raw, (int, float)):
            try:
                return datetime.utcfromtimestamp(float(raw))
            except (OverflowError, OSError, ValueError):
                continue
        if isinstance(raw, str):
            try:
                # Trim 'Z' suffix; datetime.fromisoformat accepts the rest.
                s = raw[:-1] if raw.endswith("Z") else raw
                dt = datetime.fromisoformat(s)
                return dt.replace(tzinfo=None)
            except ValueError:
                continue
    try:
        return datetime.utcnow()
    except Exception:
        return fallback


# =============================================================================
# V-19.8 RESUME helper — expression-hash dedup for paused-and-resumed sessions
# =============================================================================

async def is_expression_persisted_in_task(
    db_session,
    task_id: int,
    expression: str,
) -> bool:
    """Return True if an expression with the same hash already lives in
    `alphas` under this task_id.

    V-19.4 resume_session calls this before re-submitting expressions
    that the prior worker generated but never finished simulating.
    Uses ix_alphas_task_expr_hash (partial index) for O(log n) lookup.
    """
    if not expression:
        return False
    from sqlalchemy import select, exists
    from backend.alpha_semantic_validator import compute_expression_hash
    from backend.models import Alpha

    expr_hash = compute_expression_hash(expression)
    if not expr_hash:
        return False
    stmt = select(
        exists().where(
            Alpha.task_id == task_id,
            Alpha.expression_hash == expr_hash,
        )
    )
    result = await db_session.execute(stmt)
    return bool(result.scalar())


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
    from backend.alpha_semantic_validator import compute_expression_hash
    from backend.models import Alpha

    # V-17 (2026-05-04): mirrors workflow.run_with_persistence — populate
    # fields_used so cross-dataset analytics work for T2/T3 incremental saves.
    # V-26.88 (2026-05-13): reuse the module-level validator singleton
    # instead of constructing a fresh AlphaSemanticValidator per alpha.
    def _extract_used_fields(expr: str) -> list:
        if not expr:
            return []
        try:
            v = _get_fields_used_validator()
            return list(v.validate(expr).used_fields)
        except Exception:
            return []

    # V-19.2 (2026-05-05): per-row SAVEPOINT — see workflow.run_with_persistence
    # for full rationale. One bad row no longer rolls back the whole seed batch.
    from backend.agents.graph.persistence_errors import log_persistence_error

    # V-26.87 (2026-05-13): the original V-19.3 pre-check SELECT'd existing
    # alpha_ids to label cross-task duplicates BEFORE the savepoint INSERT.
    # That was needed when V-19.2 used ORM add()+flush — IntegrityError raised
    # late. After V-19.8 switched to pg_insert(...).on_conflict_do_nothing()
    # the SELECT became redundant: ON CONFLICT handles dedup atomically and
    # we already log the skipped alpha_ids by inspecting the INSERT result.
    # Removing the SELECT saves one DB round-trip per batch and closes the
    # race window the SELECT introduced anyway (worker A SELECTs, worker B
    # INSERTs, worker A INSERTs -> ON CONFLICT skip — labelling that as a
    # "cross-task dup" was already misleading).
    #
    # We still track cross-task skips for observability: when ON CONFLICT
    # fires we look at the existing row's task_id (cheap, no extra round-
    # trip — added to the INSERT path below).
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    # V-26.91 (2026-05-13): pre-fix used one wall-clock for the whole
    # batch, which collapsed the per-alpha sim completion times into the
    # moment the batch crossed _incremental_save_alphas. Cascade phases
    # span minutes; later auditing of "when did this alpha actually
    # complete simulating" lost that precision. We now prefer BRAIN's
    # `dateModified` from the alpha metrics (set by simulate response)
    # and fall back to per-alpha wall-clock at persist time.
    batch_fallback_at = datetime.utcnow()
    out: List[AlphaResult] = []
    inserted_alpha_ids: List[str] = []  # alpha_ids that successfully landed
    on_conflict_skipped: List[str] = []  # alpha_ids skipped by ON CONFLICT (race / cross-task)
    skipped_no_alpha_id: List[str] = []  # V-26.92 observability
    for alpha in pending_alphas:
        if alpha.quality_status not in ("PASS", "PASS_PROVISIONAL"):
            continue
        # V-26.92 (2026-05-13): PASS-status alpha with no alpha_id is a
        # broken upstream contract — INSERT would store a NULL alpha_id
        # row (Postgres unique constraint tolerates multiple NULLs) which
        # we'd then have to clean up manually. Skip with a logger.warning
        # so the situation is observable but the batch can still land.
        if not alpha.alpha_id:
            skipped_no_alpha_id.append(getattr(alpha, "expression", "?")[:60])
            logger.warning(
                f"[_incremental_save_alphas] V-26.92 skipping PASS-status "
                f"alpha with no alpha_id (likely sim returned None): "
                f"expression={getattr(alpha, 'expression', '?')[:120]!r}"
            )
            continue
        # V-26.87: cross-task dedup handled by ON CONFLICT below — no more
        # pre-batch SELECT round-trip.
        metrics_dict = alpha.metrics if isinstance(alpha.metrics, dict) else {}
        expr_hash = compute_expression_hash(alpha.expression) if alpha.expression else None

        # V-26.91: per-alpha snapshot_at. BRAIN's dateModified is ISO-string
        # in metrics; parse leniently and fall back to a fresh wall-clock
        # for THIS alpha rather than the batch start.
        snapshot_at = _resolve_metrics_snapshot_at(metrics_dict, batch_fallback_at)

        # V-26.89 (2026-05-13): compute fields_used pre-INSERT so it lands
        # in the same outer commit as the alpha row. Pre-fix wrote it in
        # a SECOND commit after the alpha INSERT had already committed —
        # a worker crash between the two left rows with fields_used=NULL
        # and no scheduled backfill. Now both arrive atomically.
        try:
            fields_used_for_insert = _extract_used_fields(alpha.expression) or None
        except Exception:
            fields_used_for_insert = None

        values_dict = dict(
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
            # V-26.89: fields_used now part of the same INSERT, atomic
            # with the rest of the row. Post-commit UPDATE retained below
            # as a defensive backfill for rows that miss this path (e.g.
            # legacy code path or partial reconstruction during resume).
            fields_used=fields_used_for_insert,
            # Phase 2 B4: typed Hypothesis link
            hypothesis_id=hypothesis_id,
        )
        try:
            async with db_session.begin_nested():
                stmt = (
                    pg_insert(Alpha)
                    .values(**values_dict)
                    .on_conflict_do_nothing(index_elements=["alpha_id"])
                    .returning(Alpha.id)
                )
                result = await db_session.execute(stmt)
                inserted_id = result.scalar_one_or_none()
            if inserted_id is None:
                # ON CONFLICT skipped (alpha_id collided after SELECT pre-check)
                if alpha.alpha_id:
                    on_conflict_skipped.append(alpha.alpha_id)
                logger.info(
                    f"[_incremental_save_alphas] V-19.8 ON CONFLICT skip "
                    f"alpha_id={alpha.alpha_id} (race-window collision)"
                )
                continue
            if alpha.alpha_id:
                inserted_alpha_ids.append(alpha.alpha_id)
        except Exception as e:
            import traceback as _tb
            logger.error(
                f"[_incremental_save_alphas] V-19.8 alpha INSERT savepoint rolled back: "
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

    # V-19.8 (2026-05-10) watchdog liveness signal. Update task.last_alpha_persisted_at
    # only when at least 1 row landed — empty rounds (all FAIL) shouldn't reset
    # the dead-detection clock. Updated in same outer commit so it's atomic
    # with the alpha INSERTs.
    if inserted_alpha_ids:
        try:
            from datetime import timezone as _tz
            from sqlalchemy import update as _sa_update
            from backend.models import MiningTask
            # last_alpha_persisted_at is TIMESTAMP WITH TIME ZONE; use a tz-
            # aware UTC value to avoid asyncpg tz-subtraction errors.
            await db_session.execute(
                _sa_update(MiningTask)
                .where(MiningTask.id == task_id)
                .values(last_alpha_persisted_at=datetime.now(_tz.utc))
            )
        except Exception as _e:
            logger.warning(
                f"[_incremental_save_alphas] V-19.8 last_alpha_persisted_at "
                f"update failed (non-fatal): {_e}"
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
            # V-26.90 (2026-05-13): redis-based rate limit so a burst of
            # 20+ PASS alphas in one cascade phase doesn't dump 20
            # refresh_can_submit_for_alpha tasks onto celery within the
            # same second. Each landed alpha tries to claim a 60s slot;
            # if the slot is held, enqueue is skipped (the previous slot
            # holder's refresh will pick up this alpha when it sweeps).
            try:
                from backend.tasks.redis_pool import get_redis_client
                cli = get_redis_client()
                rate_key = "can_submit:enqueue_rate"
                # Allow up to 6 enqueues per 60s window across all workers.
                # Sliding count via INCR + EXPIRE; once over 6 the new
                # alpha falls back to the periodic V-22.12.1 sweep.
                # V-27.72: atomic INCR+EXPIRE via Lua. The old
                # `incr` then `if current == 1: expire` pair is non-atomic —
                # a SIGKILL between the two leaves the key with no TTL, which
                # then stays > 6 forever and permanently rate-limits every
                # worker's can_submit refresh enqueue. The Lua also
                # self-heals a key already orphaned (TTL < 0) by a pre-fix
                # crash.
                current = int(cli.eval(
                    "local c = redis.call('INCR', KEYS[1]) "
                    "if c == 1 or redis.call('TTL', KEYS[1]) < 0 then "
                    "redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1])) end "
                    "return c",
                    1, rate_key, 60,
                ))
                if current > 6:
                    logger.info(
                        f"[_incremental_save_alphas] V-26.90 can_submit refresh "
                        f"rate-limited (window count={current}); skipping enqueue "
                        f"for alpha_id={alpha.alpha_id} — sweep will catch up"
                    )
                    continue
            except Exception as _e:
                # Fail-open: rate limit unavailable → enqueue as before.
                logger.debug(f"[_incremental_save_alphas] V-26.90 rate-limit check failed (proceeding): {_e}")
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

    # V-22.1 (2026-05-10): record_success_pattern per PASS alpha. The
    # cascade execution path (_run_cascade_phase / _prefetch_round_isolated)
    # never invokes feedback_agent.learn_from_round — that's a daily-beat
    # task. Without per-round writes the SUCCESS_PATTERN pool stays
    # frozen at whatever the daily feedback last produced, and V-22's
    # brain_status feedback loop has nothing to update. Writing here
    # closes the loop: record SUCCESS_PATTERN with placeholder brain_*
    # fields → 30s later refresh_can_submit_for_alpha back-fills the
    # verdict → next round's RAG retrieval surfaces it to the LLM.
    if state.pending_alphas:
        try:
            from backend.agents.services.rag_service import RAGService
            db_session = configurable.get("db_session")
            if db_session is not None:
                rag = RAGService(db_session)
                kb_written = 0
                for alpha in state.pending_alphas:
                    if alpha.quality_status not in ("PASS", "PASS_PROVISIONAL"):
                        continue
                    if not alpha.expression:
                        continue
                    # V-27.73: symmetric with _incremental_save_alphas'
                    # `not alpha.alpha_id` skip (V-26.92). A sim that returned
                    # None has no alpha_id — it can't enter the alphas table,
                    # so it must NOT enter the KB SUCCESS_PATTERN pool either.
                    if not alpha.alpha_id:
                        continue
                    # V-26.93 (2026-05-13): skip KB write when no hypothesis
                    # link is available AND Phase 2 (hypothesis-keyed KB) is
                    # the active level. Without this guard the KB ends up
                    # mixing untyped (legacy) rows with hypothesis-tagged
                    # rows, weakening the V-26.12 family-boost retrieve
                    # signal. We still allow writes when the active level
                    # is 0/1 because those tasks legitimately have no
                    # hypothesis_id and the KB is the only learning signal.
                    active_level = configurable.get("hypothesis_centric_level") or 0
                    if active_level >= 2 and current_hypothesis_id is None:
                        logger.warning(
                            f"[{node_name}] V-26.93 skip SUCCESS_PATTERN write: "
                            f"level={active_level} but hypothesis_id=None "
                            f"for alpha_id={alpha.alpha_id}"
                        )
                        continue
                    metrics_dict = alpha.metrics if isinstance(alpha.metrics, dict) else {}
                    try:
                        await rag.record_success_pattern(
                            expression=alpha.expression,
                            metrics=metrics_dict,
                            region=state.region,
                            dataset_id=state.dataset_id,
                            alpha_id=alpha.alpha_id,
                            hypothesis_id=current_hypothesis_id,
                            experiment_variant=str(
                                configurable.get("experiment_variant")
                            ) if configurable.get("experiment_variant") else None,
                        )
                        kb_written += 1
                    except Exception as _e:
                        logger.warning(
                            f"[{node_name}] V-22.1 record_success_pattern failed for "
                            f"{alpha.alpha_id}: {_e}"
                        )
                if kb_written:
                    logger.info(
                        f"[{node_name}] V-22.1 wrote {kb_written} SUCCESS_PATTERN "
                        f"entries (brain_status placeholders pending refresh)"
                    )
        except Exception as _e:
            logger.warning(f"[{node_name}] V-22.1 SUCCESS_PATTERN write skipped: {_e}")

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
            details={"metrics": alpha.metrics, "hypothesis": alpha.hypothesis},
            # V-25.B (2026-05-13): hypothesis link for FAIL alphas. Uses
            # the same resolved current_hypothesis_id (scalar with list[0]
            # fallback for LangGraph propagation drops, identical to the
            # PASS path above).
            hypothesis_id=current_hypothesis_id,
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
                # B5 v2: LLM-based attribution if llm_service in configurable.
                # mining_agent.run_mining_iteration injects this; legacy callers
                # leave it None and fall back to heuristic.
                llm_service=configurable.get("llm_service"),
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
    llm_service=None,
) -> Dict[int, List[Dict]]:
    """Round-level lifecycle update for every hypothesis proposed this round.

    B5 v1: heuristic attribution via early_stop.classify_attribution
           (75% rule on syntax+simulate vs quality fails).
    B5 v2 (2026-05-06): when llm_service is provided, defer to
           classify_attribution_llm which reads hypothesis statement +
           sample alpha attempts and judges semantically. Falls back to
           heuristic on LLM failure / empty hypothesis / cheap-skip cases.

    For each hypothesis_id in state.current_hypothesis_ids:
      1. Compute round counts: alpha_count, pass_count, syntax_fail,
         simulate_fail, quality_fail, best_sharpe
      2. Classify attribution (LLM if available, heuristic fallback)
      3. Append entry to history_so_far[hid]
      4. Call lifecycle transitions:
           - alpha_count > 0  → mark_active (all hids — V-19.6)
           - pass_count > 0   → mark_promoted (primary only — V-19.6)
           - should_abandon   → mark_abandoned (primary only — V-19.6)
    """
    from backend.agents.graph.early_stop import (
        classify_attribution,
        should_abandon_hypothesis,
    )
    from backend.agents.graph.attribution import classify_attribution_llm
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

    # B5 v2: LLM-based attribution when llm_service available; falls back
    # to heuristic on LLM failure / empty hypothesis / unknown shortcuts.
    # Resolve hypothesis statement for the LLM context — read from the
    # primary hypothesis row (just persisted by B3).
    hypothesis_statement = None
    if llm_service is not None and primary_hid is not None:
        try:
            async with AsyncSessionLocal() as _qdb:
                from backend.models import Hypothesis as _H
                _row = await _qdb.get(_H, primary_hid)
                if _row is not None:
                    hypothesis_statement = _row.statement
        except Exception as _e:
            logger.warning(f"[B5 v2] hypothesis statement lookup failed: {_e}")

    attribution, attribution_reason = await classify_attribution_llm(
        hypothesis_statement=hypothesis_statement,
        pending_alphas=pending_alphas,
        alpha_count=alpha_count,
        pass_count=pass_count,
        syntax_fail_count=syntax_fail,
        simulate_fail_count=simulate_fail,
        quality_fail_count=quality_fail,
        llm_service=llm_service,
    )
    entry = {
        "round_index": round_index,
        "alpha_count": alpha_count,
        "pass_count": pass_count,
        "syntax_fail_count": syntax_fail,
        "simulate_fail_count": simulate_fail,
        "quality_fail_count": quality_fail,
        "attribution": attribution,
        "attribution_reason": attribution_reason,  # B5 v2: LLM rationale
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
            #
            # G — Hypothesis Refinement Loop (2026-05-06): when abandon
            # would fire, attempt LLM refinement first. If LLM returns a
            # refined child statement → create new Hypothesis with
            # parent_hypothesis_id=primary, mark parent SUPERSEDED instead
            # of ABANDONED. node_hypothesis (next round) detects unused
            # refined children and reuses them, closing the feedback loop.
            superseded_via_refine: List[int] = []
            if primary_hid is not None:
                should_abandon, abandon_reason = should_abandon_hypothesis(
                    history_out.get(primary_hid, []),
                    hypothesis_id=primary_hid,
                )
                if should_abandon:
                    refined_child_id = None
                    if llm_service is not None:
                        from backend.agents.graph.hypothesis_refine import (
                            refine_hypothesis_llm, find_chain_depth,
                        )
                        from backend.services.hypothesis_service import (
                            HypothesisCreateData,
                        )
                        from backend.models import HypothesisKind
                        try:
                            chain_depth = await find_chain_depth(primary_hid, _hdb)
                            parent_h = await svc.get_by_id(primary_hid)
                            if parent_h is not None:
                                # Sample fail expressions from this round
                                sample_fails = []
                                for a in pending_alphas:
                                    if a.quality_status in ("FAIL", "REJECT"):
                                        expr = (a.expression or "")[:100]
                                        sample_fails.append(expr)
                                refined = await refine_hypothesis_llm(
                                    parent_statement=parent_h.statement,
                                    parent_rationale=parent_h.rationale or "",
                                    history=history_out.get(primary_hid, []),
                                    sample_fail_exprs=sample_fails,
                                    llm_service=llm_service,
                                    current_chain_depth=chain_depth,
                                )
                                if refined is not None:
                                    child_data = HypothesisCreateData(
                                        statement=refined.statement,
                                        rationale=refined.rationale,
                                        region=parent_h.region,
                                        universe=parent_h.universe,
                                        kind=HypothesisKind.INVESTMENT_THESIS.value,
                                        target_tier=parent_h.target_tier,
                                        confidence=refined.confidence,
                                        novelty=refined.novelty,
                                        dataset_pool=parent_h.dataset_pool or [],
                                        parent_hypothesis_id=primary_hid,
                                        experiment_variant=parent_h.experiment_variant,
                                    )
                                    child_row = await svc.create_hypothesis(child_data)
                                    refined_child_id = child_row.id
                                    if await svc.mark_superseded(primary_hid, refined_child_id):
                                        superseded_via_refine.append(primary_hid)
                                        logger.info(
                                            f"[G refine] parent={primary_hid} → child={refined_child_id} "
                                            f"reason={refined.refinement_reason[:80]!r} "
                                            f"chain_depth={chain_depth + 1}"
                                        )
                        except Exception as _re:
                            logger.warning(
                                f"[G refine] failed (will fall through to abandon): {_re}"
                            )

                    # Fall back to abandon if refinement didn't happen
                    if refined_child_id is None:
                        if await svc.mark_abandoned(primary_hid, reason=abandon_reason):
                            abandoned.append(primary_hid)
                            # V-24.A: explicit terminal-path log so abandon
                            # audit can quantify abandon vs supersede ratio
                            logger.info(
                                f"[B6 terminal=ABANDONED] hid={primary_hid} "
                                f"reason={abandon_reason!r}"
                            )
                    else:
                        # G refine path already logged via [G refine] above;
                        # add explicit terminal marker for audit consistency
                        logger.info(
                            f"[B6 terminal=SUPERSEDED] hid={primary_hid} "
                            f"child={refined_child_id} via=G-refine"
                        )
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
