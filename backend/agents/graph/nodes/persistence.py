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
from backend.agents.graph.nodes.base import record_trace, resolve_db
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

async def _read_bandit_arm_for_round(db_session, task_id: int) -> Optional[str]:
    """G1 Phase A (2026-05-19): read the bandit-selected arm for THIS round
    from ``mining_tasks.config['contextual_bandit_v1']['last_select']``.

    Returns the arm name string, or None when:
      * ``ENABLE_DIRECTION_BANDIT=False`` (state never written)
      * Round 1 of a task (bandit's ``_evolve_strategy`` cycle hasn't run yet)
      * task.config or last_select missing / malformed
      * any DB error (soft-fail to keep persistence hot path resilient)

    Single cheap SELECT per save batch (1-4 alphas typical) — preferable to
    threading the arm through 3 layers of LangGraph configurable / kwargs
    just for an observability stamp.
    """
    try:
        from backend.config import settings as _g1_settings
        if not getattr(_g1_settings, "ENABLE_DIRECTION_BANDIT", False):
            return None
        from sqlalchemy import select as _g1_select
        from backend.models.task import MiningTask
        stmt = _g1_select(MiningTask.config).where(MiningTask.id == task_id).limit(1)
        row = (await db_session.execute(stmt)).scalar_one_or_none()
        if not isinstance(row, dict):
            return None
        state = row.get("contextual_bandit_v1")
        if not isinstance(state, dict):
            return None
        last_select = state.get("last_select")
        # Persisted shape: [[region, category, failure], arm_name] (per
        # ContextualDirectionBandit.to_dict). Tolerate tuple/list variants
        # in case of future schema drift; reject anything else as None.
        if isinstance(last_select, (list, tuple)) and len(last_select) == 2:
            arm = last_select[1]
            if isinstance(arm, str) and arm:
                return arm
        return None
    except Exception as _e:
        # Hot path — log at debug, do NOT raise into the save batch.
        logger.debug(
            f"[_read_bandit_arm_for_round] task_id={task_id} soft-fail: {_e}"
        )
        return None


async def _incremental_save_alphas(
    db_session,
    task_id: int,
    run_id: Optional[int],
    region: str,
    universe: str,
    dataset_id: str,
    pending_alphas: List,
    hypothesis_id: Optional[int] = None,
    g8_forest_referenced_ids: Optional[List[int]] = None,
) -> List["AlphaResult"]:
    """Write PASS / PASS_PROVISIONAL Alpha rows directly to DB at save_results
    time rather than buffering in state.generated_alphas until workflow returns.

    Post tier-system removal (2026-05-18) this is the only persistence path —
    the old factor_tier=2/3 gate has been removed, FLAT_CONTINUOUS sessions get
    1-4 INSERT/round via the same code.

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

    # G1 Phase A (2026-05-19): stamp every PASS alpha's metrics dict with the
    # bandit-recommended arm for this round. ``last_select`` was persisted at
    # the end of the PRIOR round by ``_evolve_strategy`` → bandit cycle, so it
    # reflects "the arm Thompson sampling picked for THIS round". Reading it
    # here (one cheap SELECT per batch — typically 1-4 alphas) means the
    # ``alphas.metrics`` JSONB carries arm provenance, enabling per-arm PASS-
    # rate analytics WITHOUT joining direction_bandit_log on (task_id, round).
    #
    # When ``ENABLE_DIRECTION_BANDIT=False`` the stamp is None (key omitted).
    # Round 1 (cold start, no prior _evolve_strategy run) also writes None.
    # Soft-fail: any read error leaves metrics untouched (no exception
    # bubbles into the persistence hot path).
    bandit_arm_for_round: Optional[str] = await _read_bandit_arm_for_round(
        db_session, task_id
    )

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

    # V-27.45: hypothesis reuse TOCTOU guard. V-22.13 reuse (generation.py)
    # read this hypothesis's status in a since-closed session; a concurrent
    # B5 mark_abandoned may have flipped it to terminal (ABANDONED/SUPERSEDED)
    # in the race window. Re-check here, close to the INSERT — terminal →
    # drop the link, the alpha rows still land with hypothesis_id=NULL
    # (consistent with alpha.py "NULL for legacy alphas"; B5 attribution is
    # None-safe). Fail-open: a check failure keeps the link (pre-fix behaviour).
    from backend.config import settings as _cfg_settings
    if (
        hypothesis_id is not None
        and getattr(_cfg_settings, "HYPOTHESIS_REUSE_TERMINAL_GUARD_ENABLED", True)
    ):
        try:
            from backend.services.hypothesis_service import HypothesisService
            _terminal = await HypothesisService(db_session).filter_terminal_ids(
                [hypothesis_id]
            )
            if hypothesis_id in _terminal:
                logger.warning(
                    f"[_incremental_save_alphas] V-27.45 hypothesis "
                    f"{hypothesis_id} is terminal — dropping link on this "
                    f"batch's alpha rows (hypothesis_id → NULL)"
                )
                hypothesis_id = None
        except Exception as _tg_e:
            logger.warning(
                f"[_incremental_save_alphas] V-27.45 terminal check failed "
                f"(non-fatal, link kept): {_tg_e}"
            )

    # P1 (2026-05-19, plan v1.3.1 §3.2.3 [V1.0-M4 / V1.1]): widen accepted
    # statuses to include FAIL when ENABLE_FAIL_ALPHA_PERSIST ON.
    # BRAIN-accepted FAIL alphas (alpha_id present + is_simulated + sim
    # success) carry full metrics and a BRAIN handle — they belong in the
    # entity store (alphas table), not the failure log.
    from backend.config import settings as _persist_settings
    _persist_fail = bool(
        getattr(_persist_settings, "ENABLE_FAIL_ALPHA_PERSIST", False)
    )
    _accepted_statuses = {"PASS", "PASS_PROVISIONAL"}
    if _persist_fail:
        _accepted_statuses.add("FAIL")

    for alpha in pending_alphas:
        if alpha.quality_status not in _accepted_statuses:
            continue
        # V-26.92 (2026-05-13): PASS-status alpha with no alpha_id is a
        # broken upstream contract — INSERT would store a NULL alpha_id
        # row (Postgres unique constraint tolerates multiple NULLs) which
        # we'd then have to clean up manually. Skip with a logger.warning
        # so the situation is observable but the batch can still land.
        if not alpha.alpha_id:
            skipped_no_alpha_id.append(getattr(alpha, "expression", "?")[:60])
            logger.warning(
                f"[_incremental_save_alphas] V-26.92 skipping {alpha.quality_status}-status "
                f"alpha with no alpha_id (likely sim returned None): "
                f"expression={getattr(alpha, 'expression', '?')[:120]!r}"
            )
            continue
        # P1: FAIL alpha sanity — require is_simulated + simulation_success
        # to ensure BRAIN really accepted it. Without this, a future code
        # path that constructs FAIL alphas with alpha_id but no real sim
        # (e.g., test mock leak) would write a bogus row.
        if alpha.quality_status == "FAIL":
            if not (alpha.is_simulated and alpha.simulation_success):
                skipped_no_alpha_id.append(
                    getattr(alpha, "expression", "?")[:60]
                )
                logger.warning(
                    f"[_incremental_save_alphas] P1 skipping FAIL alpha "
                    f"without (is_simulated + simulation_success); "
                    f"alpha_id={alpha.alpha_id} expression="
                    f"{getattr(alpha, 'expression', '?')[:120]!r}"
                )
                continue
        # V-26.87: cross-task dedup handled by ON CONFLICT below — no more
        # pre-batch SELECT round-trip.
        metrics_dict = alpha.metrics if isinstance(alpha.metrics, dict) else {}
        # G1 Phase A (2026-05-19): stamp bandit-recommended arm on PASS alpha
        # metrics. Mutates in place so the JSONB INSERT below picks it up.
        # Only stamps when bandit ran AND we got an arm (flag-OFF / round 1
        # → bandit_arm_for_round is None → key omitted).
        if bandit_arm_for_round:
            if not isinstance(alpha.metrics, dict):
                alpha.metrics = dict(metrics_dict)
            alpha.metrics["_direction_bandit_recommended_arm"] = bandit_arm_for_round
            metrics_dict = alpha.metrics
        # G8 Phase A follow-up (2026-05-19): stamp forest-referenced hypothesis
        # IDs so reverse attribution analytics ("alphas generated under what
        # forest context") can join without re-reading prompt state. Empty
        # list / None → key omitted (flag OFF / no rows qualified).
        if g8_forest_referenced_ids:
            if not isinstance(alpha.metrics, dict):
                alpha.metrics = dict(metrics_dict)
            alpha.metrics["_g8_forest_referenced_ids"] = list(g8_forest_referenced_ids)
            metrics_dict = alpha.metrics
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
        # NB: fall back to [] not None. A fieldless expression with None here
        # persists as a JSONB scalar 'null' (ORM None → json-null, not SQL
        # NULL), which breaks jsonb_array_elements_text in field_fitness_stats.
        # [] matches the column's default=[] intent and is array-typed.
        try:
            fields_used_for_insert = _extract_used_fields(alpha.expression) or []
        except Exception:
            fields_used_for_insert = []

        # (B 2026-05-22 / a-fix 2026-05-23) Attribute the alpha to the dataset of
        # its ACTUAL fields, derived from fields_used — NOT the FLAT/ONESHOT
        # anchor passed in ``dataset_id``. A cross-dataset hypothesis
        # (HYPOTHESIS_CENTRIC) anchors on one dataset (e.g. pv96) but the LLM may
        # generate fields from another (e.g. analyst4); stamping the anchor
        # mis-attributes the alpha and corrupts the dataset bandit's per-dataset
        # reward (the anchor gets credited for another dataset's sims). Fields are
        # ground truth → derive first; fall back to the anchor only when no field
        # resolves (catalog gap / fieldless expr). For ONESHOT (anchor == the
        # mined dataset) derive returns the same value, so this is a no-op there.
        # build_field_dataset_map is TTL-cached so per-alpha calls are cheap.
        try:
            from backend.dataset_attribution import (
                build_field_dataset_map,
                resolve_dataset_id,
            )

            _fdm = await build_field_dataset_map(db_session, region, universe)
            _row_dataset_id = resolve_dataset_id(
                fields_used_for_insert, _fdm, anchor=dataset_id
            )
        except Exception:
            _row_dataset_id = dataset_id  # soft-fail → the anchor (legacy behavior)

        values_dict = dict(
            task_id=task_id,
            alpha_id=alpha.alpha_id,
            expression=alpha.expression,
            expression_hash=expr_hash,
            hypothesis=alpha.hypothesis,
            logic_explanation=alpha.explanation,
            region=region,
            universe=universe,
            dataset_id=_row_dataset_id,
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
            parent_alpha_id=alpha.parent_alpha_id,
            metrics_snapshot_at=snapshot_at,
            # V-26.89: fields_used now part of the same INSERT, atomic
            # with the rest of the row. Post-commit UPDATE retained below
            # as a defensive backfill for rows that miss this path (e.g.
            # legacy code path or partial reconstruction during resume).
            fields_used=fields_used_for_insert,
            # Phase 2 B4: typed Hypothesis link
            hypothesis_id=hypothesis_id,
            # F2 (Sprint 2 review fix): B1 R11 added the column + stamp in
            # evaluation, but the previous INSERT path skipped it → column
            # always NULL, /ops/r11/capacity-stats range scans found nothing.
            # The value lives in alpha.metrics['capacity_usd_estimate'] when
            # ENABLE_CAPACITY_SCORE was ON at sim time. Promote to the
            # column so both paths surface (column for indexed range scans,
            # metrics JSONB for forward-compat).
            capacity_usd_estimate=(
                alpha.metrics.get("capacity_usd_estimate")
                if isinstance(alpha.metrics, dict) else None
            ),
        )
        # delay-0 native mining: persist the delay the sim ACTUALLY ran at.
        # metrics._sim_settings.delay is ground truth (stamped by node_simulate
        # / flip-retry). This is the ONLY live mined-alpha persist path (post
        # tier-system removal), so without it every alpha takes the column
        # default (1) and delay-0 alphas are mislabeled delay-1 even though the
        # BRAIN sim used delay-0. Omit when absent → column default (1) applies,
        # so delay-1 alphas (sim delay=1) store 1 unchanged.
        _sim_delay = (
            (metrics_dict.get("_sim_settings") or {}).get("delay")
            if isinstance(metrics_dict, dict) else None
        )
        if _sim_delay is not None:
            values_dict["delay"] = int(_sim_delay)
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

            # G5 follow-up (2026-05-19): outcome back-fill. When this is a
            # G5 offspring alpha (metrics carries _g5_crossover_parent_ids),
            # append the new alpha.id to the matching g5_crossover_log row's
            # outcome_alpha_ids JSONB array, bump outcome_pass_count (if PASS).
            # Closes the parent → offspring attribution loop so /ops/g5/
            # crossover-stats can compute true offspring PASS rate without
            # re-scanning alphas.metrics. Soft-fail: any error logged but
            # never breaks the round.
            _g5_parents = None
            try:
                _g5_parents = metrics_dict.get("_g5_crossover_parent_ids") if isinstance(metrics_dict, dict) else None
            except Exception:
                _g5_parents = None
            if isinstance(_g5_parents, list) and len(_g5_parents) >= 2 and inserted_id:
                try:
                    from sqlalchemy import text as _g5_text
                    _g5_is_pass = alpha.quality_status in ("PASS", "PASS_PROVISIONAL")
                    async with db_session.begin_nested():
                        await db_session.execute(_g5_text(
                            "UPDATE g5_crossover_log "
                            "SET outcome_alpha_ids = COALESCE(outcome_alpha_ids, '[]'::jsonb) "
                            "                       || to_jsonb(:new_id::int), "
                            "    outcome_pass_count = COALESCE(outcome_pass_count, 0) "
                            "                         + CASE WHEN :is_pass THEN 1 ELSE 0 END "
                            "WHERE parent_a_alpha_id = :pa AND parent_b_alpha_id = :pb"
                        ), {
                            "new_id": int(inserted_id),
                            "is_pass": bool(_g5_is_pass),
                            "pa": int(_g5_parents[0]),
                            "pb": int(_g5_parents[1]),
                        })
                except Exception as _g5_bf:
                    logger.warning(
                        f"[_incremental_save_alphas] G5 outcome back-fill failed "
                        f"(non-fatal): {_g5_bf}"
                    )
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
    # P1 (2026-05-19): broaden to match the INSERT filter so FAIL alphas
    # also get fields_used populated.
    from sqlalchemy import update as _sa_update, select
    inserted_set = set(inserted_alpha_ids)
    fields_used_updated = 0
    for alpha in pending_alphas:
        if alpha.quality_status not in _accepted_statuses:
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
    # P1 (2026-05-19): when flag ON, BRAIN-accepted FAIL alphas also appear
    # in the result list (matching the broadened INSERT filter at line 287).
    for alpha in pending_alphas:
        if alpha.quality_status not in _accepted_statuses:
            continue
        # P1: same FAIL sanity as the upstream INSERT filter — skip FAIL
        # without is_simulated + simulation_success (otherwise the result
        # references a row that wasn't actually INSERTed).
        if alpha.quality_status == "FAIL" and not (
            alpha.is_simulated and alpha.simulation_success
        ):
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


def _classify_alpha_failure(alpha, *, persist_fail: bool):
    """Classify one pending alpha into a (error_type, error_message) failure, or
    None when it must NOT be recorded as a failure.

    Single source of truth for the failure taxonomy, shared by node_save_results
    (batch path) and _incremental_save_failures (pipeline path). Returns None for:
    PASS/PASS_PROVISIONAL, retryable transient sims (V-27.61), and BRAIN-accepted
    FAIL alphas when ENABLE_FAIL_ALPHA_PERSIST routes them to the entity store.
    """
    if alpha.quality_status in ("PASS", "PASS_PROVISIONAL"):
        return None
    # P1: BRAIN-accepted FAIL already routed to the alphas table (success path).
    if (
        persist_fail
        and alpha.quality_status == "FAIL"
        and alpha.alpha_id
        and alpha.is_simulated
        and alpha.simulation_success
    ):
        return None
    # V-27.61: retryable transient BRAIN failures are not hypothesis evidence.
    if isinstance(alpha.metrics, dict) and alpha.metrics.get("_sim_retryable"):
        return None

    if isinstance(alpha.metrics, dict) and alpha.metrics.get("_pre_brain_skip"):
        if alpha.metrics.get("_skip_kind") == "dedup":
            return "DEDUP_SKIP", (
                alpha.simulation_error
                or "DB duplicate: already simulated (no quota consumed)"
            )
        return "PRESIM_SKIP", (
            alpha.simulation_error
            or "Pre-BRAIN skip (classifier/Q10; no quota consumed)"
        )
    if alpha.is_valid is False:
        return "SYNTAX_ERROR", (alpha.validation_error or "Syntax Error")
    if alpha.is_simulated and not alpha.simulation_success:
        return "SIMULATION_ERROR", (alpha.simulation_error or "Simulation Failed")
    if alpha.quality_status == "FAIL":
        if persist_fail:
            logger.warning(
                f"[failure-classify] P1 contract violation: FAIL gate without "
                f"(alpha_id + is_simulated + simulation_success); "
                f"alpha_id={alpha.alpha_id} is_simulated={alpha.is_simulated} "
                f"sim_success={alpha.simulation_success} "
                f"expression={(alpha.expression or '')[:80]!r}"
            )
            return "OTHER", "FAIL gate but BRAIN handle missing / sim unverified"
        return "QUALITY_CHECK_FAILED", "Metrics below threshold"
    return "OTHER", (
        alpha.simulation_error
        or (
            f"Unclassified: quality_status={alpha.quality_status or '<none>'} "
            f"is_simulated={alpha.is_simulated} sim_success={alpha.simulation_success}"
        )
    )


async def _incremental_save_failures(
    db_session,
    task_id: int,
    run_id: Optional[int],
    pending_alphas: List,
    hypothesis_id: Optional[int] = None,
    rag_ab_arm: Optional[str] = None,
    candidate_queue_id: Optional[int] = None,
) -> int:
    """Write AlphaFailure rows for non-PASS pending alphas — the pipeline
    persister's equivalent of node_save_results' failure path (which writes via
    run_with_persistence). Per-row savepoint so one bad row never drops the
    batch; commits once at the end. Returns the number of failure rows written.

    ``candidate_queue_id`` (pool E path only; None for FLAT) stamps the
    candidate_queue PK so the uq_alpha_failures_candidate_queue_id partial-unique
    index dedups a crash-window re-persist — a duplicate INSERT raises
    IntegrityError, which the per-row savepoint below already swallows.
    """
    from backend.config import settings as _persist_settings
    from backend.models import AlphaFailure

    persist_fail = bool(getattr(_persist_settings, "ENABLE_FAIL_ALPHA_PERSIST", False))
    bandit_arm = await _read_bandit_arm_for_round(db_session, task_id)

    written = 0
    for alpha in pending_alphas:
        cls = _classify_alpha_failure(alpha, persist_fail=persist_fail)
        if cls is None:
            continue
        err_type, err_msg = cls
        try:
            rec = AlphaFailure(
                task_id=task_id,
                expression=(alpha.expression[:2000] if alpha.expression else None),
                error_type=err_type,
                error_message=(err_msg[:500] if err_msg else None),
                hypothesis_id=hypothesis_id,
                bandit_arm_recommended=bandit_arm,
                rag_ab_arm=rag_ab_arm,
                candidate_queue_id=candidate_queue_id,
            )
            async with db_session.begin_nested():
                db_session.add(rec)
                await db_session.flush()
            written += 1
        except Exception as _fe:  # noqa: BLE001 — one bad row ≠ dropped batch
            logger.error(f"[_incremental_save_failures] row INSERT failed (skipped): {_fe}")
    if written:
        try:
            await db_session.commit()
        except Exception as _ce:  # noqa: BLE001
            logger.error(f"[_incremental_save_failures] commit failed: {_ce}")
    return written


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

    # Post tier-system removal (2026-05-18): incremental persistence is now
    # the only path for flat sessions — the old factor_tier ∈ {2,3} gate has
    # been retired. With daily_goal=4 this is at most 4 INSERTs per round,
    # well within transaction budget.
    from backend.config import settings as _settings
    use_incremental = (
        getattr(_settings, "T2_INCREMENTAL_PERSISTENCE", True)
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
                pending_alphas=state.pending_alphas,
                hypothesis_id=current_hypothesis_id,
                g8_forest_referenced_ids=getattr(
                    state, "g8_forest_referenced_ids", None,
                ) or None,
            )
            for alpha in state.pending_alphas:
                if alpha.quality_status in ("PASS", "PASS_PROVISIONAL"):
                    logger.info(
                        f"[{node_name}] Alpha Saved (incremental) | id={alpha.alpha_id} "
                        f"status={alpha.quality_status} "
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
                    # P1-D: skip KB SUCCESS_PATTERN for alphas downgraded by
                    # the robustness gate. The alpha row is still persisted for
                    # audit (quality_status remains PROV) but MUST NOT enter
                    # the KB — "只有扰动下仍稳健的进 KB" is the P1-D goal.
                    if (
                        isinstance(alpha.metrics, dict)
                        and alpha.metrics.get("_robustness_failed")
                    ):
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
        # P1 [V1.0-M4 / V1.1] (2026-05-19): BRAIN-accepted alphas all go to
        # alphas table. QUALITY_CHECK_FAILED = BRAIN sim succeeded + alpha_id
        # assigned + full metrics returned, only AIAC gate didn't pass —
        # entity-store value (correlation, submit, parity) requires we keep
        # the row. Plan ~/.claude/plans/alpha-persistence-ontology-refactor-
        # 2026-05-19.md v1.3.1 §3.2.1.
        from backend.config import settings as _persist_settings
        _persist_fail = bool(
            getattr(_persist_settings, "ENABLE_FAIL_ALPHA_PERSIST", False)
        )
        for alpha in state.pending_alphas:
            _is_brain_accepted_fail = (
                _persist_fail
                and alpha.quality_status == "FAIL"
                and alpha.alpha_id            # MUST have BRAIN handle
                and alpha.is_simulated
                and alpha.simulation_success  # exclude HTTP 200 empty/error responses
            )
            if (
                alpha.quality_status in ("PASS", "PASS_PROVISIONAL")
                or _is_brain_accepted_fail
            ):
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
    # P1 [V1.0-M4 / V1.1] (2026-05-19): when ENABLE_FAIL_ALPHA_PERSIST ON,
    # BRAIN-accepted FAIL alphas were already routed to success_batch above;
    # skip the fail_batch path for them. Plan v1.3.1 §3.2.2.
    from backend.config import settings as _persist_settings  # noqa: E402
    _persist_fail = bool(
        getattr(_persist_settings, "ENABLE_FAIL_ALPHA_PERSIST", False)
    )
    for alpha in state.pending_alphas:
        # Failure taxonomy extracted to _classify_alpha_failure (shared with the
        # pipeline persister's _incremental_save_failures). None → not recorded
        # (PASS/PROVISIONAL, retryable, or BRAIN-accepted FAIL when persist-on).
        _cls = _classify_alpha_failure(alpha, persist_fail=_persist_fail)
        if _cls is None:
            continue
        err_type, err_msg = _cls

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
            # RAG A/B (2026-05-21): stamp the round's arm from state here (the
            # reliable source — alphas use the same state.rag_ab_arm). Failures
            # dominate the PASS-per-real-sim denominator, so this must be set.
            rag_ab_arm=(getattr(state, "rag_ab_arm", "") or None),
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
                # V-27.D: pass config so the B5 statement read can reuse the
                # injected db_session via resolve_db.
                config=config,
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

    # R1b.2c wire (2026-05-18): persist cross-round R1b state (pending hypothesis
    # + budget ledger) to MiningTask.config so next round's
    # pipeline round can consume it. Flag-gated by either retry or
    # mutate flag — when both OFF this block is byte-equivalent legacy
    # (early-out before any DB I/O). Soft-fail per plan §6.2: never raises.
    try:
        from backend.config import settings as _r1b_settings
        if (
            bool(getattr(_r1b_settings, "ENABLE_R1B_RETRY_LOOP", False))
            or bool(getattr(_r1b_settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", False))
        ):
            _db = configurable.get("db_session")
            _task_id = getattr(state, "task_id", None)
            if _db is not None and _task_id is not None:
                from backend.models import MiningTask
                from sqlalchemy import select as _sa_select
                _task_row = (
                    await _db.execute(_sa_select(MiningTask).where(MiningTask.id == _task_id))
                ).scalar_one_or_none()
                if _task_row is not None:
                    from backend.agents.graph.nodes.r1b_persistence import (
                        persist_after_round,
                    )
                    await persist_after_round(state, _task_row, _db)
    except Exception as _r1b_ex:
        logger.warning(
            f"[{node_name}] R1b.2c persist_after_round failed (round unaffected): {_r1b_ex}"
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
    config=None,
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
        should_abandon_hypothesis_from_memory,
    )
    from backend.agents.graph.attribution import classify_attribution_llm
    from backend.config import settings as _cfg_settings
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

    # V-27.92 / V-27.71 / V-27.61: split this round's alphas into REAL /
    # flip-retry / retryable buckets BEFORE counting. The state machine
    # (mark_active / mark_promoted / should_abandon) must see a CLEAN
    # alpha_count:
    #   - flip-retry products (metadata.flipped) are implementation-layer
    #     salvage, not fresh hypothesis evidence — folding them into
    #     alpha_count inflated it and could false-trigger mark_active (V-27.71)
    #   - retryable alphas (metrics._sim_retryable) are transient BRAIN
    #     failures, not a real test of the hypothesis (V-27.61)
    # flip_* / retryable_count are tracked on their own for the DB row.
    real_alphas, flip_alphas, retryable_alphas = [], [], []
    for a in pending_alphas:
        _m = a.metrics if isinstance(getattr(a, "metrics", None), dict) else {}
        _md = a.metadata if isinstance(getattr(a, "metadata", None), dict) else {}
        if _m.get("_sim_retryable"):
            retryable_alphas.append(a)
        elif _md.get("flipped"):
            flip_alphas.append(a)
        else:
            real_alphas.append(a)

    # Real counts — these drive the lifecycle state machine.
    alpha_count = len(real_alphas)
    pass_count = sum(
        1 for a in real_alphas
        if a.quality_status in ("PASS", "PASS_PROVISIONAL")
    )
    syntax_fail = sum(1 for a in real_alphas if a.is_valid is False)
    simulate_fail = sum(
        1 for a in real_alphas
        if a.is_valid is not False and a.is_simulated and not a.simulation_success
    )
    quality_fail = sum(
        1 for a in real_alphas
        if a.is_valid is not False
        and (a.is_simulated and a.simulation_success)
        and a.quality_status in ("FAIL", "REJECT")
    )
    # Separate tracks for the DB row — NOT state-machine inputs.
    flip_alpha_count = len(flip_alphas)
    flip_pass_count = sum(
        1 for a in flip_alphas
        if a.quality_status in ("PASS", "PASS_PROVISIONAL")
    )
    retryable_count = len(retryable_alphas)

    # best_sharpe spans real + flip (flip is still a real return profile for
    # this signal direction); it's a display/log value, not an abandon input.
    best_sharpe = 0.0
    for a in real_alphas + flip_alphas:
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
            # V-27.D: pure read — reuse the workflow-injected db_session
            # when present. The lifecycle WRITES below keep self-opening
            # their own AsyncSessionLocal (transaction isolation, intended).
            async with resolve_db(config) as _qdb:
                from backend.models import Hypothesis as _H
                _row = await _qdb.get(_H, primary_hid)
                if _row is not None:
                    hypothesis_statement = _row.statement
        except Exception as _e:
            logger.warning(f"[B5 v2] hypothesis statement lookup failed: {_e}")

    # V-27.71: attribution sees only real_alphas + clean counts — flip
    # products are implementation-layer salvage and would dilute the
    # hypothesis-vs-implementation signal.
    #
    # V-27.92 followup (flip-only 轮): when a round produced ONLY flip
    # products (real_alphas empty, flips non-empty), running attribution on
    # an empty list is degenerate. Semantically the outcome IS a
    # hypothesis-level result — the stated direction failed the hard gate
    # and only the sign-flipped variant carried signal — so attribute it
    # explicitly to "hypothesis" instead of asking the LLM to classify
    # nothing. This is what lets a consistently-wrong-direction hypothesis
    # be abandoned (the empty-round guard in should_abandon no longer masks
    # flip-only rounds).
    if not real_alphas and flip_alphas:
        attribution = "hypothesis"
        attribution_reason = (
            "flip-only round — stated hypothesis direction failed the hard "
            "gate; only the sign-flipped variant produced signal"
        )
    else:
        attribution, attribution_reason = await classify_attribution_llm(
            hypothesis_statement=hypothesis_statement,
            pending_alphas=real_alphas,
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
        "flip_alpha_count": flip_alpha_count,
        "flip_pass_count": flip_pass_count,
        "retryable_count": retryable_count,
        "attribution": attribution,
        "attribution_reason": attribution_reason,  # B5 v2: LLM rationale
        "best_sharpe": best_sharpe,
    }

    # Append to all hids proposed this round (every emitted hypothesis gets
    # the same attribution since they shared a code_gen pass).
    # V-27.92: history_out is now only a DISPLAY cache (trace step + return
    # value). The authoritative abandon input is the hypothesis_round_stats
    # table written below; only the flag-off path still reads history_out.
    history_out = dict(history_so_far)
    for hid in hids:
        history_out[hid] = list(history_out.get(hid, [])) + [entry]

    use_db_stats = bool(
        getattr(_cfg_settings, "HYPOTHESIS_ABANDON_USE_DB_STATS", True)
    )
    task_id = getattr(state, "task_id", None)

    # Lifecycle DB updates — fresh session so we don't conflict with the
    # incremental persistence path's session transaction state.
    #
    # V-19.6 (2026-05-06) ghost-promotion fix: B4 links every alpha in the
    # round to the PRIMARY hypothesis. Non-primary hypotheses share the
    # round's code_gen pass but never have alphas linked to them. Post-fix:
    #   - mark_active: all hids (they all got tried via shared code_gen)
    #   - mark_promoted: ONLY primary (only primary owns the PASS alphas)
    #   - mark_abandoned: ONLY primary (consistent with promotion ownership)
    abandoned: List[int] = []
    promoted: List[int] = []
    activated: List[int] = []
    try:
        async with AsyncSessionLocal() as _hdb:
            svc = HypothesisService(_hdb)

            # V-27.92: persist this round's per-hid detail to
            # hypothesis_round_stats FIRST, so should_abandon's SELECT below
            # sees it within this transaction. Always written (even when the
            # flag is off) — the table is additive and zero-risk; only the
            # abandon DECISION is gated by the flag.
            #
            # Wrapped in a SAVEPOINT: a write failure here (e.g. an invalid
            # task_id FK) must NOT abort the V-19.6 lifecycle transitions
            # below. On failure the abandon decision degrades to "no round
            # detail" — safe, it just won't false-abandon.
            #
            # V-27.92 followup (savepoint 粒度): one SAVEPOINT *per hid*,
            # not one shared across all hids. A failure on hid N used to
            # roll back the already-succeeded upserts for hids 0..N-1 too;
            # now each hid degrades independently. The V-19.6 lifecycle
            # transitions below sit outside every savepoint, unaffected.
            for hid in hids:
                try:
                    async with _hdb.begin_nested():
                        await svc.upsert_round_stats(
                            hypothesis_id=hid,
                            task_id=task_id,
                            round_index=round_index,
                            alpha_count=alpha_count,
                            pass_count=pass_count,
                            syntax_fail_count=syntax_fail,
                            simulate_fail_count=simulate_fail,
                            quality_fail_count=quality_fail,
                            flip_alpha_count=flip_alpha_count,
                            flip_pass_count=flip_pass_count,
                            retryable_count=retryable_count,
                            attribution=attribution,
                            attribution_reason=attribution_reason,
                            best_sharpe=best_sharpe,
                        )
                except Exception as _rs_ex:
                    logger.warning(
                        f"[B5] hypothesis_round_stats upsert failed for "
                        f"hid={hid} (non-fatal, this hid's abandon decision "
                        f"degrades to no-detail): {_rs_ex}"
                    )
            await _hdb.flush()

            # V-27.92 followup (flip-only 轮): a round that produced only
            # flip products still TRIED the hypothesis — it found the stated
            # direction wrong. So mark_active fires on real OR flip alphas;
            # otherwise a flip-only-productive hypothesis stays PROPOSED
            # forever, invisible to the state machine.
            for hid in hids:
                if alpha_count > 0 or flip_alpha_count > 0:
                    if await svc.mark_active(hid):
                        activated.append(hid)
            # Promote only the primary — non-primary hypotheses don't have
            # alpha rows attributed to them (would be PROMOTED with
            # alpha_count=0). pass_count is the REAL count: a round whose
            # only PASS came from a flip-retry product does NOT promote the
            # hypothesis (V-27.71 decision — flip is implementation salvage).
            if pass_count > 0 and primary_hid is not None:
                if await svc.mark_promoted(primary_hid):
                    promoted.append(primary_hid)
            # Abandonment: only primary owns alphas, so only primary is ever
            # ABANDONED. V-27.92: the decision now reads the
            # hypothesis_round_stats table (authoritative — survives worker
            # restart / Celery task-boundary switch / V-20.1 prefetch round's
            # isolated session). Flag off → legacy in-memory history_out path.
            # V-27.B: G-refine removed — abandon goes straight to mark_abandoned.
            if primary_hid is not None:
                if use_db_stats:
                    should_abandon, abandon_reason = await should_abandon_hypothesis(
                        _hdb, hypothesis_id=primary_hid,
                    )
                else:
                    should_abandon, abandon_reason = (
                        should_abandon_hypothesis_from_memory(
                            history_out.get(primary_hid, []),
                            hypothesis_id=primary_hid,
                        )
                    )
                if should_abandon:
                    if await svc.mark_abandoned(primary_hid, reason=abandon_reason):
                        abandoned.append(primary_hid)
                        # V-24.A: explicit terminal-path log for abandon audit
                        logger.info(
                            f"[B6 terminal=ABANDONED] hid={primary_hid} "
                            f"reason={abandon_reason!r}"
                        )
            # V-19.5 (2026-05-06): NO refresh_stats here. This helper runs
            # inside node_save_results, BEFORE workflow.run_with_persistence's
            # outer commit — querying alphas would see 0 rows for the current
            # round. (V-27.92: the hypothesis_round_stats rows above don't
            # have this problem — they're written from the in-memory
            # pending_alphas, not queried back from the alphas table.) The
            # authoritative refresh stays post-commit in run_with_persistence.
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
