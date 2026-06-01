"""
Mining Tasks - Background tasks for alpha mining

Contains the main mining task execution logic.
"""

import asyncio
import os
import random
from datetime import datetime
from typing import Optional

from sqlalchemy import select, update, func, case, and_
from sqlalchemy.orm.attributes import flag_modified  # Phase 1.5-B JSONB dirty trigger
from loguru import logger

from backend.celery_app import celery_app
from backend.config import settings
from backend.database import AsyncSessionLocal
from backend.agents import MiningAgent
from backend.adapters.brain_adapter import BrainAdapter
from backend.models import (
    MiningTask, DatasetMetadata, DatasetCellStats, Operator, DataField,
    DataFieldCellStats, ExperimentRun,
)

# Default BRAIN delay. MiningTask has no delay column, so a delay-0 native
# session carries delay in task.config["delay"] (set by start_flat_session).
# _task_delay(task) resolves it; absent/1 = the established delay-1 path so
# every cell join + sim below stays byte-for-byte unchanged.
_FLAT_DELAY = 1
from backend.tasks import run_async


def _task_delay(task) -> int:
    """BRAIN delay for this task's cell joins + sim. delay-0 native mining
    (②/B) stamps task.config['delay']=0; default _FLAT_DELAY (1) = the
    established path. Never raises — bad config falls back to delay-1."""
    try:
        return int((getattr(task, "config", None) or {}).get("delay", _FLAT_DELAY))
    except (TypeError, ValueError):
        return _FLAT_DELAY


# A FLAT session reuses ONE process-global httpx client (BrainAdapter's
# _GLOBAL_CLIENT singleton) for its entire multi-hour run. Empirically
# (2026-05-26) that client degrades after ~10 rounds — its keepalive sockets go
# stale and BRAIN sim polls then hang on perpetual Retry-After until the 20-min
# round backstop cancels them (3 in a row → session bails). A FRESH adapter in a
# separate process sims the same expression in ~2min, proving BRAIN/creds/code
# are fine — only the long-lived client rots. Per the worldquant-miner "fresh
# session per batch" pattern we proactively recreate the client every N rounds
# (and after any failed round) to force fresh connections + a clean re-auth.
_BRAIN_CLIENT_REFRESH_EVERY = 8


async def _refresh_brain_client(brain) -> bool:
    """Force a fresh BRAIN httpx client + session on the long-lived FLAT adapter.
    Uses only existing BrainAdapter methods (close/get_client/ensure_session) so
    it never touches the adapter's own sim/poll code. Never raises — a refresh
    failure is non-fatal (the next round retries on whatever client exists)."""
    try:
        from backend.adapters.brain_adapter import BrainAdapter
        await BrainAdapter.close()                       # aclose() + null the global client
        brain.client = await BrainAdapter.get_client()   # fresh client → fresh connections
        await brain.ensure_session()                     # re-auth on the fresh client
        logger.info("[flat] BRAIN client refreshed (fresh connections + re-auth)")
        return True
    except Exception as _e:  # noqa: BLE001 — refresh is best-effort
        logger.warning(f"[flat] BRAIN client refresh failed (non-fatal): {_e}")
        return False


# ---------------------------------------------------------------------------
# Phase 1.5-C: TaskSchema v2 dual-source readers
# ---------------------------------------------------------------------------
# Per plan v1.3 §3.3 + memory [[project_phase15_block1_shipped_2026_05_17]]:
# Block 1 (Revision A/B) wrote new columns dual-write with legacy. Block 2
# (here) flips read paths to prefer new cols when ENABLE_TASK_SCHEMA_V2=True.
# Default OFF preserves legacy behavior; flag flip is byte-equivalent for
# tasks created after Revision B backfill (2026-05-17).


def _is_cascade_schedule(task) -> bool:
    """Detect cascade-scheduled tasks (always refused post tier-removal).

    Cascade is permanently retired (phase15-D + tier-system removal). Any
    surviving row with schedule='CASCADE' is a historical artifact — the
    dispatcher marks it FAILED with cutover guidance.
    """
    sched = getattr(task, "schedule", None) or ""
    return sched.upper() == "CASCADE"


# Terminal/halted task states that a (re)delivered run_mining_task must NOT
# re-run — the Celery-redelivery + watchdog-revival guard. A LEGIT dispatch
# always arrives PENDING (fresh ONESHOT) or RUNNING (fresh FLAT / resume), so
# these are only ever seen on a stale redelivery/revival. (2026-05-28 task 3735:
# STOPPED/EARLY_STOPPED/PAUSED were missing → a redelivered STOPPED task set
# itself RUNNING and re-mined for minutes.)
_TASK_SKIP_STATES = ("COMPLETED", "FAILED", "STOPPED", "EARLY_STOPPED", "PAUSED")


def _pipeline_heartbeat_timeout():
    """Session-level heartbeat-abort window for the pipeline. Catches the freeze
    CLASS (poisoned asyncpg pool / queue deadlock / any unwrapped await) that
    per-op `op_timeout` cannot — see tasks 3737/3738/3739. Hard-capped below the
    watchdog dead-threshold so the abort always fires BEFORE watchdog dup-revives
    a wedged session. None / 0 = disabled."""
    hb = int(getattr(settings, "SIM_PIPELINE_HEARTBEAT_TIMEOUT_SEC", 900) or 0)
    if hb <= 0:
        return None
    wd_sec = int(getattr(settings, "CASCADE_WATCHDOG_DEAD_MIN", 25) or 25) * 60
    # Cap at watchdog-180s so even a tight watchdog override leaves headroom
    # for the abort cleanup before revive fires.
    return float(min(hb, max(60, wd_sec - 180)))


def _pipeline_op_timeout():
    """Per-operation hard deadline for the sim pipeline, HARD-CAPPED below the
    watchdog dead-threshold (task 3736). The watchdog's liveness signal is the
    latest trace_step; when BRAIN stalls every sim, trace only refreshes when a
    hung op times out and flushes its failure-trace. So op_timeout must fire
    (and refresh trace) well before the watchdog declares the session dead and
    spuriously revives it. Cap at watchdog-5min so the invariant holds even if
    SIM_PIPELINE_OP_TIMEOUT_SEC is misconfigured above the window. None = off."""
    op = int(getattr(settings, "SIM_PIPELINE_OP_TIMEOUT_SEC", 600) or 0)
    if op <= 0:
        return None
    wd_sec = int(getattr(settings, "CASCADE_WATCHDOG_DEAD_MIN", 25) or 25) * 60
    return float(min(op, max(60, wd_sec - 300)))


@celery_app.task(bind=True, name="backend.tasks.run_mining_task")
def run_mining_task(self, task_id: int, run_id: int | None = None):
    """
    Run a complete mining task.
    Called when a task is started via API.
    
    Args:
        task_id: The mining task ID
        run_id: Optional experiment run ID
    """
    logger.info(f"Starting mining task {task_id} (run_id={run_id})")
    
    async def _run():
        async with AsyncSessionLocal() as db:
            run: ExperimentRun | None = None

            # Get task
            query = select(MiningTask).where(MiningTask.id == task_id)
            result = await db.execute(query)
            task = result.scalar_one_or_none()
            
            if not task:
                logger.error(f"Task {task_id} not found")
                return {"error": "Task not found"}

            # Idempotency check (2026-05-03; STOPPED/EARLY_STOPPED/PAUSED added
            # 2026-05-28 — task 3735): skip if the task is in a terminal/halted
            # state. Celery redelivers unack'd queued tasks after a worker
            # restart, and the watchdog dispatches revival tasks; we must NOT
            # re-run a task the operator/system has already STOPPED. The original
            # tuple only had COMPLETED/FAILED (the comment even named
            # EARLY_STOPPED but the code dropped it), so a redelivered STOPPED
            # task fell through to line ~295 which set status=RUNNING, defeating
            # the STOPPED state and the per-round STOPPED checks below — task 3735
            # re-mined for minutes despite being STOPPED. Every LEGIT dispatch
            # arrives PENDING (fresh ONESHOT) or RUNNING (fresh FLAT / resume —
            # resume_flat_session flips RUNNING before dispatch; intervene never
            # dispatches), so skipping these non-RUNNING states is safe.
            if task.status in _TASK_SKIP_STATES:
                logger.info(f"Task {task_id} in terminal/halted state {task.status}, skipping (redelivery/revival guard)")
                return {"skipped": True, "status": task.status}

            # 2026-05-11: concurrent-run guard (cascade-stuck-T2 RCA).
            # Multiple run_mining_task celery deliveries can stack up for one
            # task (manual resume + watchdog 5min revive + crash redelivery)
            # and then all run concurrently — each worker reads task.
            # cascade_phase independently, all enter T2 phase, none advances
            # T1 phase cleanly. Empirically observed 3 workers in same
            # _run_cascade_phase tier=2 at the same millisecond after restart,
            # with 0 RAG_QUERY trace entries over 1h46m.
            #
            # First-attempt fix (b6a6c97): pg_try_advisory_lock. Did NOT work
            # because SQLAlchemy AsyncSession uses connection pool — each
            # db.execute() may check out a different connection from pool,
            # and pg advisory locks are session/connection-scoped. Lock got
            # released between queries.
            #
            # Revised fix: Redis SET NX EX. Cross-process, cross-connection,
            # crash-safe via TTL (3h = max expected cascade run). Worker exits
            # cleanly on duplicate; redis cleanup on normal exit + TTL safety.
            cascade_lock_key = f"cascade_lock:task:{task_id}"
            cascade_lock_acquired = False
            from backend.tasks.redis_pool import (
                acquire_cascade_lock,
                release_cascade_lock,
                peek_lock_holder,
                verify_lock_ownership,
            )
            _lock_ttl = getattr(settings, "CASCADE_LOCK_TTL_SEC", 10800)
            _takeover_enabled = getattr(settings, "CASCADE_LOCK_TAKEOVER_ENABLED", True)

            # V-27.1: peek the run's config_snapshot for a watchdog-handed
            # takeover token BEFORE touching the lock. READ-ONLY query — no
            # run is created here; _get_or_create_run below still does the
            # authoritative create/attach (so a duplicate exit leaves no
            # orphan run row). If the watchdog revived this task it already
            # atomically took over the lock with this token, so we CLAIM it
            # (verify ownership) rather than acquire a fresh one — the latter
            # is what let the old worker keep running alongside the
            # replacement (the V-27.1 double-run race).
            _handed_token = None
            if run_id is not None and _takeover_enabled:
                _peek_run = (
                    await db.execute(
                        select(ExperimentRun).where(ExperimentRun.id == run_id)
                    )
                ).scalar_one_or_none()
                if _peek_run is not None and isinstance(_peek_run.config_snapshot, dict):
                    _handed_token = _peek_run.config_snapshot.get("cascade_lock_token")

            if _handed_token:
                # --- Claim path: the watchdog already owns the lock for us ---
                cascade_lock_token = _handed_token
                ownership = verify_lock_ownership(cascade_lock_key, cascade_lock_token)
                if ownership == "OWNED":
                    cascade_lock_acquired = True
                    logger.info(
                        f"[cascade] task={task_id} claimed watchdog takeover "
                        f"lock (token={cascade_lock_token})"
                    )
                elif ownership == "MISSING":
                    # TTL expired between takeover and worker start-up — fall
                    # back to a fresh acquire with the same token.
                    try:
                        cascade_lock_acquired = acquire_cascade_lock(
                            cascade_lock_key, cascade_lock_token,
                            ttl_sec=_lock_ttl, run_id=run_id,
                            worker_pid=os.getpid(),
                        )
                    except Exception as _e:
                        logger.error(
                            f"[cascade] redis unreachable re-acquiring expired "
                            f"takeover lock for task={task_id} (fail-closed): {_e}"
                        )
                        return {
                            "skipped": True,
                            "reason": "redis_unavailable",
                            "task_id": task_id,
                        }
                    if cascade_lock_acquired:
                        logger.warning(
                            f"[cascade] task={task_id} takeover lock had "
                            f"expired; re-acquired fresh with the same token"
                        )
                elif ownership == "UNKNOWN":
                    # Redis blip — fail-closed, don't run unprotected.
                    logger.error(
                        f"[cascade] redis UNKNOWN verifying takeover lock for "
                        f"task={task_id} (fail-closed)"
                    )
                    return {
                        "skipped": True,
                        "reason": "redis_unavailable",
                        "task_id": task_id,
                    }
                # ownership == "LOST" → cascade_lock_acquired stays False.
                if not cascade_lock_acquired:
                    holder = peek_lock_holder(cascade_lock_key) or {}
                    holder_str = holder.get("token", "?")
                    logger.warning(
                        f"[cascade] task={task_id} takeover lock no longer "
                        f"ours (ownership={ownership}, held by {holder_str}); "
                        f"this worker exits as duplicate."
                    )
                    return {
                        "skipped": True,
                        "reason": "duplicate_active_run",
                        "task_id": task_id,
                        "lock_holder": holder_str,
                    }
            else:
                # --- Normal acquire path — token is this celery dispatch ---
                cascade_lock_token = self.request.id  # unique per celery dispatch
                try:
                    cascade_lock_acquired = acquire_cascade_lock(
                        cascade_lock_key, cascade_lock_token,
                        ttl_sec=_lock_ttl, run_id=run_id,
                        worker_pid=os.getpid(),
                    )
                except Exception as _e:
                    # V-26.27: fail-closed when Redis is unreachable. The
                    # previous behavior set cascade_lock_acquired=True
                    # (fail-open) which allowed duplicate cascade workers to
                    # run concurrently — the exact bug the lock was added to
                    # prevent. Better to skip this dispatch; celery beat /
                    # watchdog will retry.
                    logger.error(
                        f"[cascade] redis lock unreachable, refusing to dispatch "
                        f"task={task_id} (fail-closed): {_e}"
                    )
                    return {
                        "skipped": True,
                        "reason": "redis_unavailable",
                        "task_id": task_id,
                    }
                if not cascade_lock_acquired:
                    holder = peek_lock_holder(cascade_lock_key) or {}
                    holder_str = holder.get("token", "?")
                    logger.warning(
                        f"[cascade] Task {task_id} already has an active run "
                        f"(redis lock held by {holder_str}); celery_task="
                        f"{self.request.id} exits as duplicate."
                    )
                    return {
                        "skipped": True,
                        "reason": "duplicate_active_run",
                        "task_id": task_id,
                        "lock_holder": holder_str,
                    }

            # V-26.4: Lua-atomic release. Only deletes the key if it still
            # holds our token — if our TTL expired and another worker
            # re-acquired, or the watchdog took over with a different token,
            # this is a no-op (V-27.1: the old worker cannot clobber the
            # replacement's lock).
            def _release_lock():
                release_cascade_lock(cascade_lock_key, cascade_lock_token)

            # Update status to RUNNING + freeze BRAIN role snapshot
            # (P3-Brain plan §8.3). 改 instance-level 写 + 单 commit 替代 bulk
            # UPDATE,因为 bulk UPDATE 不会 flush instance-level task.config
            # 改动。merge 模式保留 hypothesis_centric_variant 等 task_service
            # create_task 时塞入的 config keys + 未来任何 task.config key。
            # config JSONB 未用 MutableDict 包裹 — 必须整体重新赋值整个 dict
            # object 才能触发 SQLAlchemy instrumented setter。
            #
            # 早 commit 是必须的:后续 _prefetch_round_isolated 用独立 session,
            # 读不到本 session 的 uncommitted 修改(R4-C10)。
            # _prefetch_round_isolated 路径只读 snapshot,绝不写入(R5-#1 防回归)。
            task.status = "RUNNING"
            if not isinstance(task.config, dict):
                task.config = {}
            if "brain_role_snapshot" not in task.config:
                task.config = {
                    **task.config,
                    "brain_role_snapshot": {
                        "brain_consultant_mode_at_start": settings.ENABLE_BRAIN_CONSULTANT_MODE,
                        "effective_default_test_period": settings.effective_default_test_period,
                        "effective_sharpe_submit_min": settings.effective_sharpe_submit_min,
                        "effective_region_universes": dict(settings.effective_region_universes),
                    },
                }
            await db.commit()

            # Create or attach ExperimentRun
            run = await _get_or_create_run(db, task, run_id, self.request.id)

            # V-19.2 (2026-05-10): CONTINUOUS_CASCADE mining mode dispatch.
            # When the task is created via POST /mining-session/start the
            # mining_mode column is 'CONTINUOUS_CASCADE' — drop into the
            # persistent service loop instead of the discrete dataset loop.
            # Discrete tasks (mining_mode='DISCRETE', the default) keep
            # original behavior 100%.
            if _is_cascade_schedule(task):
                # phase15-D PR3c (2026-05-18): cascade dispatch retired
                # unconditionally. The ENABLE_CASCADE_LEGACY kill-switch
                # (PR1) has been removed alongside this dispatch and the
                # entire mining_session router — cascade tasks can no
                # longer be created via API. Pre-PR3c historical cascade
                # rows that somehow get dispatched (e.g. via direct DB
                # status='PENDING' set) are marked FAILED with cutover
                # guidance. Rollback path: git revert PR3c + restore the
                # deleted router + revert this branch.
                logger.warning(
                    f"[phase15-D PR3c] cascade dispatch refused for task {task_id} "
                    f"— legacy retired; use POST /api/v1/ops/start-flat-session"
                )
                try:
                    await db.execute(
                        update(MiningTask)
                        .where(MiningTask.id == task_id)
                        .values(status="FAILED")
                    )
                    if run is not None:
                        run.status = "FAILED"
                        run.finished_at = datetime.utcnow()
                        run.error_message = (
                            "cascade legacy retired (phase15-D PR3c); "
                            "use POST /api/v1/ops/start-flat-session"
                        )[:500]
                    await db.commit()
                except Exception as db_err:
                    logger.error(f"[phase15-D PR3c] failed to mark task FAILED: {db_err}")
                return None
                # phase15-D PR3d (2026-05-18): _run_continuous_cascade body
                # + helpers (_run_cascade_phase / _prefetch_round_isolated /
                # _verify_cascade_ownership / _resolve_cascade_phase) DELETED
                # together — net ~730 LoC trim. Cascade dispatch always-FAILS
                # above; no further code path reaches the deleted helpers.

            # Post tier-system removal (2026-05-18): flat sessions are the only
            # continuous mining path. Detect via task.schedule (mining_mode
            # column dropped). Gated by ENABLE_FLAT_CONTINUOUS — if a FLAT task
            # is dispatched while the flag is OFF, refuse to run (defensive:
            # should be unreachable when /ops/start-flat-session gates correctly).
            if (task.schedule or "").upper() == "FLAT":
                if not getattr(settings, "ENABLE_FLAT_CONTINUOUS", False):
                    logger.warning(
                        f"[flat] task_id={task_id} dispatched as FLAT "
                        f"but ENABLE_FLAT_CONTINUOUS=False; marking FAILED"
                    )
                    await db.execute(
                        update(MiningTask)
                        .where(MiningTask.id == task_id)
                        .values(status="FAILED")
                    )
                    if run is not None:
                        run.status = "FAILED"
                        run.finished_at = datetime.utcnow()
                        run.error_message = "ENABLE_FLAT_CONTINUOUS flag is OFF"
                    await db.commit()
                    return {"error": "ENABLE_FLAT_CONTINUOUS=False"}
                try:
                    return await _run_flat_iteration(
                        db, task, run, self.request.id,
                        lock_key=cascade_lock_key,
                        lock_token=cascade_lock_token,
                    )
                except Exception as e:
                    logger.error(f"[flat] Task {task_id} failed: {e}")
                    await db.rollback()
                    try:
                        await db.execute(
                            update(MiningTask)
                            .where(MiningTask.id == task_id)
                            .values(status="FAILED")
                        )
                        if run is not None:
                            run.status = "FAILED"
                            run.finished_at = datetime.utcnow()
                            run.error_message = str(e)[:500]
                        await db.commit()
                    except Exception as db_err:
                        logger.error(f"[flat] failed to mark task FAILED: {db_err}")
                        await db.rollback()
                    raise
                finally:
                    _release_lock()

            try:
                async with BrainAdapter() as brain:
                    mining_agent = MiningAgent(db, brain)

                    # Get datasets to mine
                    datasets = await _get_datasets_to_mine(db, task)
                    
                    if not datasets:
                        logger.warning(f"No datasets found for mining in {task.region}/{task.universe}")
                        if run is not None:
                            run.status = "FAILED"
                            run.finished_at = datetime.utcnow()
                            run.error_message = "No datasets found"
                            await db.commit()
                        return {"warning": "No datasets found"}

                    # Get operators
                    operators = await _get_operators(db)
                    
                    # Mine each dataset
                    total_alphas = 0
                    for dataset_id in datasets:
                        # Check if task should continue.
                        # Bug fix (2026-05-03): include EARLY_STOPPED in the
                        # halt list. Previously when mining_agent's W1 round-
                        # level early-stop fired and set task.status =
                        # EARLY_STOPPED, this dataset loop ignored it and
                        # marched into the next dataset, re-entering
                        # run_evolution_loop and running another full
                        # max_iterations × seed-pool cycle. T2/T3 spike tasks
                        # accumulated 31 evolution_loop entries over 10+ hrs
                        # before finally exhausting the dataset list.
                        await db.refresh(task)
                        if task.status in ["STOPPED", "PAUSED", "EARLY_STOPPED"]:
                            logger.info(f"Task {task_id} {task.status}, stopping dataset loop")
                            break
                        
                        if task.progress_current >= task.daily_goal:
                            logger.info(f"Task {task_id} reached goal")
                            break
                        
                        # Get fields — main dataset + universal PV supplement
                        # (D1: cross-dataset alpha support).
                        _td = _task_delay(task)
                        fields = await _get_dataset_fields(db, dataset_id, task.region, task.universe, _td)

                        if not fields:
                            logger.warning(f"No fields found for dataset {dataset_id}, skipping")
                            continue

                        if dataset_id != "pv1":
                            pv_supplement = await _get_universal_pv_fields(db, task.region, task.universe, _td)
                            n_before = len(fields)
                            fields = _merge_field_pools(fields, pv_supplement)
                            n_added = len(fields) - n_before
                            if n_added:
                                logger.info(
                                    f"[mining] dataset={dataset_id} merged {n_added} universal-PV fields "
                                    f"(total fields={len(fields)})"
                                )
                        
                        # Calculate remaining alphas needed
                        remaining_goal = task.daily_goal - task.progress_current
                        if remaining_goal <= 0:
                            logger.info(f"Task {task_id} already reached goal, stopping")
                            break

                        # Plan v5+ §Phase 1 (A2): build available_dataset_pool
                        # for cross-dataset hypothesis. When HYPOTHESIS_CENTRIC_LEVEL
                        # is 0 the pool stays empty and node_hypothesis falls back
                        # to single-anchor behavior. Once enabled the LLM picks
                        # 1-3 datasets in the pool and node_code_gen unions
                        # those datasets' field pools.
                        from backend.config import settings as _hge_settings
                        active_level = (task.config or {}).get(
                            "hypothesis_centric_variant",
                            _hge_settings.HYPOTHESIS_CENTRIC_LEVEL,
                        )
                        if active_level >= 1:
                            complementary = await _get_complementary_datasets(
                                db, task, dataset_id,
                                k=_hge_settings.PHASE1_COMPLEMENTARY_DATASET_K,
                            )
                            available_dataset_pool = [dataset_id] + complementary
                            logger.info(
                                f"[mining] Phase 1 active (level={active_level}) "
                                f"dataset_pool={available_dataset_pool}"
                            )
                        else:
                            available_dataset_pool = []

                        # Run evolution loop
                        try:
                            # PR4 fix — honor the task's configured daily_goal as
                            # num_alphas_per_round (was hardcoded 4 ignoring user
                            # input). Option A (2026-05-27): fallback now reads
                            # settings.ALPHAS_PER_ROUND (default 10, was dead
                            # config) instead of a hardcoded 4.
                            num_per_round = task.daily_goal or settings.ALPHAS_PER_ROUND
                            # 2026-05-21: per-round hard deadline (see pipeline round).
                            # TimeoutError is caught by the except below → loop continues.
                            result = await asyncio.wait_for(
                                mining_agent.run_evolution_loop(
                                    task=task,
                                    dataset_id=dataset_id,
                                    fields=fields,
                                    operators=operators,
                                    max_iterations=task.max_iterations or 10,
                                    target_alphas=remaining_goal,
                                    num_alphas_per_round=num_per_round,
                                    run_id=run.id,
                                    available_dataset_pool=available_dataset_pool,
                                    # Plan v5+ §Phase 2 (B3): typed Hypothesis
                                    # persistence triggers when level>=2. Variant
                                    # tags rows for F-5 KB isolation.
                                    hypothesis_centric_level=int(active_level or 0),
                                    experiment_variant=str(
                                        (task.config or {}).get("hypothesis_centric_variant", active_level)
                                    ),
                                ),
                                timeout=settings.MINING_ROUND_TIMEOUT_SEC,
                            )
                            
                            # Update progress
                            task.progress_current += result.get("total_success", 0)
                            await db.commit()
                            
                            total_alphas += len(result.get("all_alphas", []))
                            
                            logger.info(
                                f"Evolution loop for {dataset_id} complete | "
                                f"iterations={result.get('iterations_completed')} "
                                f"success={result.get('total_success')}"
                            )
                            
                            if result.get("target_reached"):
                                logger.info(f"Task {task_id} reached goal via evolution loop")
                                break
                                
                        except Exception as e:
                            logger.error(f"Evolution loop failed for {dataset_id}: {e}")
                            # Rollback any failed transaction before continuing
                            await db.rollback()
                            continue
                
                # Mark task complete
                await db.execute(
                    update(MiningTask)
                    .where(MiningTask.id == task_id)
                    .values(status="COMPLETED")
                )

                if run is not None:
                    run.status = "COMPLETED"
                    run.finished_at = datetime.utcnow()
                await db.commit()
                
                logger.info(f"Task {task_id} completed: {total_alphas} alphas mined")
                return {"success": True, "alphas_mined": total_alphas}
                
            except Exception as e:
                logger.error(f"Task {task_id} failed: {e}")
                # Rollback any failed transaction before updating status
                await db.rollback()
                
                try:
                    await db.execute(
                        update(MiningTask)
                        .where(MiningTask.id == task_id)
                        .values(status="FAILED")
                    )

                    if run is not None:
                        run.status = "FAILED"
                        run.finished_at = datetime.utcnow()
                        run.error_message = str(e)[:500]  # Limit error message length
                    await db.commit()
                except Exception as db_err:
                    logger.error(f"Failed to update task status: {db_err}")
                    await db.rollback()
                raise
            finally:
                # 2026-05-11 cascade-stuck-T2: release Redis lock for discrete
                # task path. CONTINUOUS_CASCADE path has its own finally above.
                _release_lock()

    return run_async(_run())


async def _get_or_create_run(db, task, run_id, celery_task_id):
    """Get or create an experiment run."""
    if run_id is not None:
        run_query = select(ExperimentRun).where(ExperimentRun.id == run_id)
        run_res = await db.execute(run_query)
        run = run_res.scalar_one_or_none()

        if run and run.task_id != task.id:
            raise ValueError(f"ExperimentRun {run_id} does not belong to task {task.id}")

        if run is None:
            run = ExperimentRun(
                id=run_id,
                task_id=task.id,
                status="RUNNING",
                trigger_source="API",
                celery_task_id=celery_task_id,
                config_snapshot=_create_config_snapshot(task),
                strategy_snapshot={},
            )
            db.add(run)
            await db.commit()
            await db.refresh(run)
        else:
            run.status = "RUNNING"
            run.trigger_source = "API"
            run.celery_task_id = celery_task_id
            run.error_message = None
            await db.commit()
    else:
        run = ExperimentRun(
            task_id=task.id,
            status="RUNNING",
            trigger_source="API",
            celery_task_id=celery_task_id,
            config_snapshot=_create_config_snapshot(task),
            strategy_snapshot={},
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
    
    return run


def _create_config_snapshot(task):
    """Create a config snapshot for experiment run."""
    return {
        "task": {
            "region": task.region,
            "universe": task.universe,
            "dataset_strategy": task.dataset_strategy,
            "target_datasets": task.target_datasets,
            "daily_goal": task.daily_goal,
            "config": task.config,
        },
    }


async def _get_datasets_to_mine(db, task):
    """Get list of dataset IDs to mine.

    V-13 fix (2026-05-03): when mining_weight ties (currently all=1 across
    USA/TOP3000), Postgres ORDER BY DESC returns implicit ctid order, which
    pinned model16 to position 0 forever. T1's daily_goal=4 then broke out
    of the dataset loop on first dataset, so model51..fundamental6..others
    were never explored. Adding func.random() as secondary sort distributes
    starting dataset uniformly across runs while still honoring real
    mining_weight differences when they exist.
    """
    if task.dataset_strategy == "SPECIFIC" and task.target_datasets:
        return task.target_datasets

    # Auto-explore: get top datasets by weight, randomize ties.
    # Cell-stats normalization: mining_weight lives on dataset_cell_stats per
    # (universe, delay). LEFT JOIN the task's cell so a dataset whose cell is not
    # yet synced for this universe still appears (COALESCE→1.0 default) rather
    # than vanishing from the mining list.
    _delay = _task_delay(task)
    weight = func.coalesce(DatasetCellStats.mining_weight, 1.0)
    ds_query = (
        select(DatasetMetadata.dataset_id)
        .select_from(DatasetMetadata)
        .outerjoin(
            DatasetCellStats,
            and_(
                DatasetCellStats.dataset_ref == DatasetMetadata.id,
                DatasetCellStats.universe == task.universe,
                DatasetCellStats.delay == _delay,
            ),
        )
        .where(DatasetMetadata.region == task.region)
        .order_by(weight.desc(), func.random())
        .limit(10)
    )
    # delay-0 native mining: require the dataset to actually have a cell at THIS
    # delay. The permissive OUTER join + COALESCE(weight,1.0) otherwise surfaces
    # delay-1-only datasets (model16/model77/pv96/… → zero delay-0 fields), which
    # then instant-skip in pipeline round — burning round slots, diluting
    # the bandit, and inflating trace_steps.iteration (session appears to "start
    # at round 2"). delay-1 keeps the permissive join (every synced dataset has a
    # delay-1 cell; see the LEFT-JOIN rationale above) → unchanged.
    if _delay != _FLAT_DELAY:
        ds_query = ds_query.where(DatasetCellStats.id.isnot(None))
    ds_result = await db.execute(ds_query)
    return [row[0] for row in ds_result.all()]


async def _get_complementary_datasets(
    db, task, anchor_dataset_id: str, k: int = 3
) -> list[str]:
    """Plan v5+ §Phase 1 (A2): pick K complementary dataset_ids alongside
    the anchor, to form the available_dataset_pool the LLM hypothesis
    node may pick from.

    Strategy: same region/universe, mining_weight DESC, exclude the anchor,
    randomized ties. K defaults to settings.PHASE1_COMPLEMENTARY_DATASET_K
    when positive; 0 returns empty list (legacy single-anchor behavior).

    Why a separate query (not just slicing _get_datasets_to_mine output):
    that function already trims to top-10 globally, and we want the anchor
    to always lead. Per-anchor query keeps the pool deterministic w.r.t.
    the anchor while still random across ties.
    """
    if k <= 0:
        return []
    _delay = _task_delay(task)
    weight = func.coalesce(DatasetCellStats.mining_weight, 1.0)
    ds_query = (
        select(DatasetMetadata.dataset_id)
        .select_from(DatasetMetadata)
        .outerjoin(
            DatasetCellStats,
            and_(
                DatasetCellStats.dataset_ref == DatasetMetadata.id,
                DatasetCellStats.universe == task.universe,
                DatasetCellStats.delay == _delay,
            ),
        )
        .where(
            DatasetMetadata.region == task.region,
            DatasetMetadata.dataset_id != anchor_dataset_id,
        )
        .order_by(weight.desc(), func.random())
        .limit(k)
    )
    # delay-0: exclude delay-1-only datasets that have no cell at this delay
    # (same rationale as _get_datasets_to_mine — they'd field-skip the round).
    if _delay != _FLAT_DELAY:
        ds_query = ds_query.where(DatasetCellStats.id.isnot(None))
    rows = (await db.execute(ds_query)).all()
    return [row[0] for row in rows]


async def _get_operators(db):
    """Get operators for mining."""
    # ORDER BY category, name — deterministic, and ensures any downstream cap
    # spans categories rather than slicing off the (insertion-last) Cross
    # Sectional / Group operators (id 51-66). See plan a-streamed-wren.
    op_query = (
        select(Operator)
        .where(Operator.is_active == True)
        .order_by(Operator.category, Operator.name)
    )
    op_result = await db.execute(op_query)
    
    operators = []
    for op in op_result.scalars().all():
        operators.append({
            "name": op.name,
            "category": op.category,
            "description": op.description,
            "definition": op.definition
        })
    
    if not operators:
        # Fallback if DB is empty
        logger.warning("No operators found in DB, using basic set")
        operators = [
            {"name": "ts_rank", "category": "Time Series", "description": "Rank over time", "definition": "ts_rank(x, d)"},
            {"name": "ts_mean", "category": "Time Series", "description": "Mean over time", "definition": "ts_mean(x, d)"},
            {"name": "ts_std_dev", "category": "Time Series", "description": "Std Dev over time", "definition": "ts_std_dev(x, d)"},
            {"name": "ts_corr", "category": "Time Series", "description": "Correlation", "definition": "ts_corr(x, y, d)"},
            {"name": "ts_product", "category": "Time Series", "description": "Product over time", "definition": "ts_product(x, d)"},
            {"name": "ts_sum", "category": "Time Series", "description": "Sum over time", "definition": "ts_sum(x, d)"}
        ]
    
    return operators


async def _get_dataset_fields(db, dataset_id, region, universe, delay=_FLAT_DELAY):
    """Get fields for a dataset (cell-stats normalization: datasets/datafields are
    universe-invariant defs; the per-(universe, delay) cell supplies is_active).
    delay defaults to 1; a delay-0 session passes delay=0 so the LLM sees the
    delay-0-available field roster (sparser, partly distinct from delay-1)."""
    ds_meta_stmt = select(DatasetMetadata.id).where(
        DatasetMetadata.dataset_id == dataset_id,
        DatasetMetadata.region == region,
    )
    ds_meta_id = (await db.execute(ds_meta_stmt)).scalar_one_or_none()

    if ds_meta_id is None:
        return []

    # is_active == True (2026-05-22): exclude fields BRAIN rejects as
    # "Invalid data field" — auto-deactivated by prune_invalid_datafields.
    # Without this filter is_active was a dead flag and a stale catalog field
    # (e.g. pv96_eq_dvd_cash_cg_amt, 107 sim failures/wk once the dataset
    # bandit steered onto long-dormant pv96) kept being offered to the LLM.
    # is_active is now per (universe, delay) on datafield_cell_stats — join the
    # mining cell so a field deactivated in this universe is hidden here.
    # Value fields first (2026-05-23): GROUP-heavy datasets (pv13: 135 GROUP /
    # 30 MATRIX) would otherwise crowd MATRIX/VECTOR value fields out of the
    # downstream [:60]/[:30] caps, leaving the LLM only group fields to (mis)use
    # as value inputs. Order non-GROUP (value) fields ahead of GROUP.
    fields_stmt = (
        select(DataField.field_id, DataField.field_name, DataField.description, DataField.field_type)
        .join(
            DataFieldCellStats,
            and_(
                DataFieldCellStats.datafield_ref == DataField.id,
                DataFieldCellStats.universe == universe,
                DataFieldCellStats.delay == delay,
            ),
        )
        .where(
            DataField.dataset_id == ds_meta_id,
            DataFieldCellStats.is_active.is_(True),
        )
        .order_by(
            case((DataField.field_type == "GROUP", 1), else_=0),
            DataField.field_id,
        )
    )
    rows = (await db.execute(fields_stmt)).all()

    return [
        {"id": fid, "name": fname, "description": desc, "type": ftype}
        for (fid, fname, desc, ftype) in rows
    ]


# D1 — Universal price-volume field whitelist. Every mining round adds these
# alongside the main dataset's fields, so LLM can produce cross-dataset
# alphas (e.g. fundamental signal × returns) — verified pattern in BRAIN
# user gold alphas (top-3 sharpe>2.3 all use fundamental6 + returns).
# Hard-coded list rather than top-N by coverage to keep behavior stable
# even if BRAIN's pv field roster changes.
_UNIVERSAL_PV_FIELDS = (
    "close", "open", "high", "low",
    "volume", "vwap",
    "returns",
    "cap", "sharesout",
    "adv5", "adv20",
    "amount",
)


async def _get_universal_pv_fields(db, region, universe, delay=_FLAT_DELAY):
    """Pull the canonical PV fields (price-volume) regardless of which
    dataset is being mined. Returns at most |_UNIVERSAL_PV_FIELDS| entries
    that exist in the datafields table for this region/universe/delay.
    delay defaults to 1; a delay-0 session passes delay=0.
    """
    pv_meta_id = (await db.execute(
        select(DatasetMetadata.id).where(
            DatasetMetadata.dataset_id == "pv1",
            DatasetMetadata.region == region,
        )
    )).scalar_one_or_none()
    if pv_meta_id is None:
        return []
    fields_stmt = (
        select(DataField.field_id, DataField.field_name, DataField.description, DataField.field_type)
        .join(
            DataFieldCellStats,
            and_(
                DataFieldCellStats.datafield_ref == DataField.id,
                DataFieldCellStats.universe == universe,
                DataFieldCellStats.delay == delay,
            ),
        )
        .where(DataField.dataset_id == pv_meta_id)
        .where(DataField.field_id.in_(_UNIVERSAL_PV_FIELDS))
        .where(DataFieldCellStats.is_active.is_(True))
    )
    rows = (await db.execute(fields_stmt)).all()
    return [
        {"id": fid, "name": fname, "description": desc, "type": ftype}
        for (fid, fname, desc, ftype) in rows
    ]


def _merge_field_pools(primary, supplement):
    """Place supplement fields at the *front* of the merged list, then primary.
    Reason: build_t1_strategy_user_prompt truncates available_fields to the
    first 80 entries; if supplement (typically ~10 universal PV fields) is
    appended at the tail it gets dropped for primary datasets with >70
    fields (e.g. fundamental6 has 886). Putting supplement first guarantees
    cross-dataset signal candidates are always visible to the LLM.
    """
    sup_ids = {f["id"] for f in supplement if f.get("id")}
    out = list(supplement)
    for f in primary:
        fid = f.get("id")
        if fid and fid not in sup_ids:
            out.append(f)
    return out


# =============================================================================
# V-19.2 CONTINUOUS_CASCADE main loop (2026-05-10)
# =============================================================================
# Persistent mining service: while not paused, cycle T1 → T2 → T3 phases
# for fixed round budgets per phase (round-driven per IX-2). Phase skip
# when local + global seed pool both insufficient (IX-1 hybrid C). T3
# Post tier-system removal (2026-05-18): cascade body + tier-aware seed
# counters retired. Flat session is the only continuous path.


def _get_active_level(task) -> int:
    """Active hypothesis_centric level for this task (HGE Phase 1+)."""
    from backend.config import settings as _hge
    raw = (task.config or {}).get(
        "hypothesis_centric_variant",
        _hge.HYPOTHESIS_CENTRIC_LEVEL,
    )
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


async def _build_dataset_pool(db, task, dataset_id: str) -> list:
    """Build available_dataset_pool for cross-dataset hypothesis (Phase 1)."""
    from backend.config import settings as _hge
    active_level = _get_active_level(task)
    if active_level >= 1:
        complementary = await _get_complementary_datasets(
            db, task, dataset_id, k=_hge.PHASE1_COMPLEMENTARY_DATASET_K,
        )
        return [dataset_id] + complementary
    return []


async def _prepare_round_fields(db, task, dataset_id: str):
    """Load + merge fields for a single round. Returns None if dataset has
    no fields (caller should skip)."""
    _td = _task_delay(task)
    fields = await _get_dataset_fields(db, dataset_id, task.region, task.universe, _td)
    if not fields:
        return None
    if dataset_id != "pv1":
        pv_supplement = await _get_universal_pv_fields(db, task.region, task.universe, _td)
        fields = _merge_field_pools(fields, pv_supplement)
    return fields


def _verify_cascade_ownership(lock_key: str, token: str, *, where: str) -> bool:
    """V-27.1: round-boundary lock ownership self-check. Returns True if the
    worker should keep running, False if it should exit gracefully.

      OWNED   → True  (we still hold the lock)
      UNKNOWN → True  (Redis blip — a transient error must NEVER make a live
                       worker self-terminate; the RCA safety floor.)
      LOST    → False (watchdog took over; a replacement worker is running)
      MISSING → False (lock vanished — TTL expired / cleared; don't keep
                       running unprotected)

    Returns True unconditionally when CASCADE_LOCK_TAKEOVER_ENABLED is off,
    so the flag is a full kill-switch for the self-exit path.

    phase15-D PR3d (2026-05-18): name kept (legacy "cascade" prefix) but now
    used SOLELY by _run_flat_iteration for FLAT session ownership self-check.
    The CASCADE_* settings + redis_pool function names are similarly legacy
    naming — full rename deferred to a future hygiene PR (no behavior change).
    """
    if not getattr(settings, "CASCADE_LOCK_TAKEOVER_ENABLED", True):
        return True
    from backend.tasks.redis_pool import renew_cascade_lock, verify_lock_ownership
    state = verify_lock_ownership(lock_key, token)
    if state in ("OWNED", "UNKNOWN"):
        if state == "OWNED":
            _ttl = getattr(settings, "CASCADE_LOCK_TTL_SEC", 10800)
            if not renew_cascade_lock(lock_key, token, _ttl):
                logger.warning(
                    f"[lock-ownership] {where}: lock renew returned 0 "
                    f"(redis blip or token mismatch) — continuing; the next "
                    f"boundary check will catch a genuine loss"
                )
        elif state == "UNKNOWN":
            logger.warning(
                f"[lock-ownership] {where}: redis UNKNOWN — continuing "
                f"(a transient error must not self-terminate a live worker)"
            )
        return True
    logger.warning(
        f"[lock-ownership] {where}: lock state={state} — this worker has "
        f"been taken over, exiting gracefully"
    )
    return False


async def _rebuild_flat_db_session(old_db, task_id, run_id, brain):
    """Discard a possibly-poisoned AsyncSession and return a fresh
    ``(db, task, run, mining_agent)`` bound to a clean connection (2026-05-25).

    Root cause (task 3504): a per-round ``asyncio.wait_for`` timeout cancels
    ``run_evolution_loop`` mid-flight; when the cancel lands inside an asyncpg
    DB op, SQLAlchemy's cleanup raises ``greenlet_spawn has not been called``
    and the shared session is permanently poisoned — every later
    ``db.refresh`` / commit on it re-raises, so one slow round killed the WHOLE
    FLAT session and stranded the cursor. Closing the old session returns its
    (pool-invalidated) connection; the new session checks out a clean one.
    ``task`` / ``run`` are re-fetched into the new session; the caller rebinds
    them and the ``MiningAgent``. ``brain`` may be None (finalization path,
    outside the BrainAdapter context) → no MiningAgent is built.
    """
    for _op in (old_db.rollback, old_db.close):
        try:
            await _op()
        except Exception:  # noqa: BLE001 — a poisoned session may fail both
            pass
    new_db = AsyncSessionLocal()
    task = await new_db.get(MiningTask, task_id)
    run = await new_db.get(ExperimentRun, run_id) if run_id is not None else None
    mining_agent = MiningAgent(new_db, brain) if brain is not None else None
    return new_db, task, run, mining_agent


def _pick_diverse_dataset(datasets, dataset_cov, category_cov, category_of):
    """Option C step-3 explore: pick the dataset minimizing (category coverage,
    dataset coverage) — i.e. spread across data-source CATEGORIES first (the v3
    orthogonality currency: same-category datasets produce correlated alphas;
    cross-category is more orthogonal), then least-covered dataset within. When
    no category map is given each dataset is its own category → degrades to
    plain coverage-greedy (C-step1). Structural + low-overfit.
    """
    if not datasets:
        return None

    def _key(d):
        cat = category_of.get(d, d)
        return (category_cov.get(cat, 0), dataset_cov.get(d, 0))

    return min(datasets, key=_key)


def _dataset_mean_margin(reward, dataset):
    """Mean alpha margin for a dataset this session (reward[d]=(sum, n)); -inf
    when no margins recorded yet (so exploit never picks an unmeasured one)."""
    s, n = reward.get(dataset, (0.0, 0))
    return (s / n) if n else float("-inf")


def _pick_dataset(datasets, dataset_cov, category_cov, category_of, reward, explore_prob, rand):
    """Option C ε-greedy dataset pick: EXPLORE (category-stratified least-covered,
    C-step1 breadth + C-step3 orthogonality-via-category) with probability
    ``explore_prob`` or while no margins are known yet; otherwise EXPLOIT
    (highest mean alpha margin — C-step2 economic value). Low-overfit: ε + dense
    margin + structural category coverage, no historical threshold fitting.
    """
    if not datasets:
        return None
    has_reward = any(reward.get(d, (0.0, 0))[1] for d in datasets)
    if not has_reward or rand() < explore_prob:
        return _pick_diverse_dataset(datasets, dataset_cov, category_cov, category_of)
    return max(datasets, key=lambda d: _dataset_mean_margin(reward, d))


async def _run_flat_iteration(db, task, run, celery_task_id, *, lock_key, lock_token):
    """FLAT continuous-mining path — the producer-consumer pipeline.

    Sole FLAT path since the serial round loop (``_run_one_round_inline``) was
    retired 2026-05-29. Overlaps LLM generation with BRAIN simulation so the
    sim slots stay saturated.

    Session model (the F1 concurrency contract): the producer and the persister
    each get their OWN ``AsyncSessionLocal`` session; the N sim consumers are
    DB-free (node_simulate/node_evaluate open their own ephemeral sessions).
    The injected ``db`` is used only for read-only setup + finalization, never
    during the concurrent run.

    Known gaps not yet ported from the (now-retired) serial loop — must land
    before these matter at scale:
      - per-round BRAIN client refresh (F4) + consecutive-failure session
        rebuild (generation rounds rarely poison a session — sim, the
        hang-prone step, is now in the DB-free consumers);
      - R14 task stop-loss (ENABLE_TASK_STOP_LOSS): the auto-pause brake is not
        evaluated on this path;
      - BRAIN auth-circuit park-and-retry: when the circuit is open the
        producer keeps advancing the cursor (legacy parks on the dataset);
      - trace iteration_offset threading (trace_steps.iteration restarts per
        round) — pairs with the F3 iteration-grouping redefinition.
    """
    from backend.database import AsyncSessionLocal
    from backend.agents.graph.workflow import MiningWorkflow
    from backend.agents.pipeline import run_flat_pipeline_session

    task_id = task.id
    run_id = run.id if run is not None else None
    max_iters = int(getattr(settings, "FLAT_CONTINUOUS_MAX_ITERATIONS", 100) or 100)
    # Per-session override (2026-06-02): task.config["flat_daily_goal"] wins over
    # the global settings.FLAT_CONTINUOUS_DAILY_GOAL when present (start_flat_session
    # validates it >0 → truthy). Absent → global default.
    daily_goal = int(
        (task.config or {}).get("flat_daily_goal")
        or getattr(settings, "FLAT_CONTINUOUS_DAILY_GOAL", 20)
        or 20
    )
    # Option C diversity steering: the pipeline uses a SMALL per-dataset batch
    # (not the legacy ALPHAS_PER_ROUND=10) so coverage-greedy selection spreads
    # the session across many distinct datasets. gen/sim overlap keeps slots
    # saturated despite the small batch (see config note).
    num_alphas = int(getattr(settings, "SIM_PIPELINE_DATASET_BATCH", 4) or 4)

    logger.info(
        f"[flat-pipeline] task={task_id} region={task.region} starting "
        f"pipeline session (num_alphas={num_alphas}, daily_goal={daily_goal})"
    )

    datasets = list(task.target_datasets or [])
    if not datasets:
        datasets = await _get_datasets_to_mine(db, task)
    if not datasets:
        logger.warning(f"[flat-pipeline] task={task_id} no datasets; exiting")
        if run is not None:
            run.status = "COMPLETED"
            run.finished_at = datetime.utcnow()
            await db.commit()
        return {"warning": "no_datasets", "total_alphas": 0}

    operators = await _get_operators(db)

    # Option C step-3: dataset → data-source category, for category-stratified
    # explore (orthogonality via cross-category spread). Soft-fail → empty map
    # (each dataset becomes its own category → degrades to plain coverage).
    _category_of: dict = {}
    try:
        crows = await db.execute(
            select(DatasetMetadata.dataset_id, DatasetMetadata.category).where(
                DatasetMetadata.region == task.region,
                DatasetMetadata.dataset_id.in_(datasets),
            )
        )
        _category_of = {did: (cat or did) for did, cat in crows.all()}
    except Exception as _cat_ex:  # noqa: BLE001
        logger.warning(f"[flat-pipeline] dataset category fetch failed (non-fatal): {_cat_ex}")
        _category_of = {}

    # Bandit weights (read once on the injected session, same query as the loop).
    bandit_on = bool(getattr(settings, "ENABLE_DATASET_VALUE_BANDIT", False))
    ds_weight_map: dict = {}
    if bandit_on:
        try:
            wrows = await db.execute(
                select(DatasetMetadata.dataset_id, DatasetCellStats.mining_weight)
                .select_from(DatasetMetadata)
                .outerjoin(
                    DatasetCellStats,
                    and_(
                        DatasetCellStats.dataset_ref == DatasetMetadata.id,
                        DatasetCellStats.universe == task.universe,
                        DatasetCellStats.delay == _task_delay(task),
                    ),
                )
                .where(
                    DatasetMetadata.region == task.region,
                    DatasetMetadata.dataset_id.in_(datasets),
                )
            )
            ds_weight_map = {
                did: float(w if w is not None else 1.0) for did, w in wrows.all()
            }
        except Exception as _wm_err:  # noqa: BLE001
            logger.warning(f"[flat-pipeline] dataset-bandit weight fetch failed: {_wm_err}")
            ds_weight_map = {}

    # Cursor + iteration count seeded from runtime_state (pause-resume), advanced
    # in-memory by the producer and persisted per round on the producer session.
    state = {"cursor": 0, "iterations": 0}
    if isinstance(run.runtime_state, dict):
        state["cursor"] = int(run.runtime_state.get("flat_cursor", 0) or 0)
        state["iterations"] = int(run.runtime_state.get("flat_iterations", 0) or 0)

    # Why the producer stopped — finalization must NOT mark COMPLETED on an
    # ownership takeover (a replacement worker is meant to continue).
    # `code` is the human-readable reason persisted to task.config[last_stop_reason]
    # + run.runtime_state[stop_reason] for observability — distinguishes natural
    # completion (max_iters / daily_goal) from external interruption
    # (auth_circuit_open / heartbeat_abort / task_paused / ownership_lost).
    _stop_reason = {"ownership_lost": False, "code": None}

    # Option C diversity steering: per-dataset coverage this session (candidates
    # generated). next_round_inputs picks the least-covered dataset → the
    # session spreads across distinct datasets (breadth). Session-local (a
    # resume re-spreads from scratch — acceptable; trace/breadth is observability
    # + steering, not correctness).
    _coverage: dict = {}
    # Option C step-3: per-category coverage (orthogonality via cross-category
    # spread). Incremented alongside _coverage.
    _category_coverage: dict = {}
    # Option C step-2: per-dataset (margin_sum, n) reward — the persister feeds
    # each simulated candidate's margin via _reward_hook; the producer ε-greedy
    # exploits high-mean-margin datasets while still exploring (breadth).
    _reward: dict = {}
    _explore_prob = float(getattr(settings, "SIM_PIPELINE_EXPLORE_PROB", 0.6) or 0.6)

    def _reward_hook(dataset, margin):
        s, n = _reward.get(dataset, (0.0, 0))
        _reward[dataset] = (s + float(margin), n + 1)

    async def _persist_cursor(pdb):
        # Best-effort cursor persistence on the producer's session so a
        # pause-resume skips already-attempted slots. Never fatal.
        try:
            run_p = await pdb.get(ExperimentRun, run_id)
            if run_p is not None and isinstance(run_p.runtime_state, dict):
                run_p.runtime_state["flat_cursor"] = state["cursor"]
                run_p.runtime_state["flat_iterations"] = state["iterations"]
                flag_modified(run_p, "runtime_state")
                await pdb.commit()
        except Exception as _cur_ex:  # noqa: BLE001
            logger.warning(f"[flat-pipeline] cursor persist failed (non-fatal): {_cur_ex}")

    async def next_round_inputs(pdb):
        # Bounded scan for the next usable dataset; the cursor advances on every
        # probe so an empty-fields dataset is skipped (not retried forever), but
        # only a round that ACTUALLY runs counts toward max_iters (mirrors the
        # legacy loop, which never spends the iteration budget on skips).
        for _ in range(len(datasets) + 1):
            if state["iterations"] >= max_iters:
                _stop_reason["code"] = _stop_reason["code"] or "max_iters_reached"
                return None
            # BRAIN auth circuit OPEN → the consumers' sims would fast-fail, so
            # don't burn LLM generation on candidates that can't simulate. Stop
            # the producer cleanly (cursor preserved → a re-dispatch resumes
            # once ops re-auths). Sub-phase 1: stop-on-open (legacy parked +
            # retried the same dataset; stop-and-resume is the simpler safe
            # equivalent for the decoupled pipeline).
            from backend.adapters.brain_adapter import BRAIN_AUTH_CIRCUIT
            if BRAIN_AUTH_CIRCUIT.is_open():
                logger.warning(
                    f"[flat-pipeline] task={task_id} BRAIN auth circuit OPEN — "
                    f"stopping producer (cursor preserved, resumable)"
                )
                _stop_reason["code"] = "auth_circuit_open"
                return None
            if not _verify_cascade_ownership(
                lock_key, lock_token, where="flat-pipeline producer"
            ):
                _stop_reason["ownership_lost"] = True
                _stop_reason["code"] = "ownership_lost"
                logger.info(f"[flat-pipeline] task={task_id} ownership lost; stopping producer")
                return None
            task_p = await pdb.get(MiningTask, task_id)
            if task_p is None or task_p.status in ("PAUSED", "STOPPED", "EARLY_STOPPED"):
                logger.info(
                    f"[flat-pipeline] task={task_id} status="
                    f"{getattr(task_p, 'status', 'GONE')}; stopping producer"
                )
                _stop_reason["code"] = (
                    f"task_{task_p.status.lower()}" if task_p is not None else "task_gone"
                )
                return None

            # Option C: ε-greedy pick — explore (least-covered, breadth) or
            # exploit (highest mean alpha margin, economic value). bandit
            # (currently OFF) still overrides when enabled.
            dataset_id = _pick_dataset(
                datasets, _coverage, _category_coverage, _category_of,
                _reward, _explore_prob, random.random,
            )
            if bandit_on and ds_weight_map:
                from backend.selection_strategy import weighted_choice
                _picked = weighted_choice(
                    datasets, [ds_weight_map.get(d, 1.0) for d in datasets]
                )
                if _picked is not None:
                    dataset_id = _picked

            fields = await _prepare_round_fields(pdb, task_p, dataset_id)
            state["cursor"] += 1
            # Mark this dataset covered (incl. the empty-fields skip below) so
            # coverage-greedy moves to the next distinct dataset rather than
            # re-picking it.
            _coverage[dataset_id] = _coverage.get(dataset_id, 0) + num_alphas
            _cat = _category_of.get(dataset_id, dataset_id)
            _category_coverage[_cat] = _category_coverage.get(_cat, 0) + num_alphas
            if not fields:
                # Skip empty dataset: persist the cursor advance (so resume
                # skips it too) but DON'T spend an iteration on it.
                await _persist_cursor(pdb)
                continue
            state["iterations"] += 1
            await _persist_cursor(pdb)
            pool = await _build_dataset_pool(pdb, task_p, dataset_id)
            config = {
                "configurable": {
                    "run_id": run_id,
                    "available_dataset_pool": pool,
                }
            }
            return {
                "task": task_p,
                "dataset_id": dataset_id,
                "fields": fields,
                "operators": operators,
                "config": config,
            }
        return None

    num_consumers = BrainAdapter._current_sim_slot_limit()

    # CRITICAL: node_simulate → simulate_alpha ALREADY acquires the role-aware
    # `brain:concurrent_sims` Redis slot internally (brain_adapter.py). If the
    # runner ALSO acquired it per consumer, each candidate would take the slot
    # TWICE — N consumers hold N slots then each block on a 2nd → self-deadlock
    # (30-min acquire timeouts, ~0 throughput). So the runner uses NO-OP slots
    # here and lets simulate_alpha own the single real acquire; concurrency is
    # still bounded to the slot ceiling by that inner acquire (extra consumers
    # block inside simulate_alpha). simulate_alpha is shared with legacy callers
    # so its inner acquire must stay.
    async def _noop_acquire():
        return True

    async def _noop_release():
        return None

    # Drain-and-refresh the shared BRAIN client every N sims (Sub-phase 1, F4) —
    # combats the long-session client-rot sim-hang; runs only with 0 in flight.
    _refresh_every = int(getattr(settings, "SIM_PIPELINE_CLIENT_REFRESH_EVERY", 0) or 0)

    # F2-2/3/4: R1b retry + hypothesis-mutate + G5 crossover through the feedback
    # channel — wired only for whichever legacy flag is on. A composed classifier
    # (persister-side) maps a FAIL → RETRY (implementation) / MUTATE (hypothesis,
    # dominates "both"), and a PASS → PASS_LANDED (G5 trigger); a dispatching
    # handler (producer-side) rewrites+re-pushes (retry), proposes a hypothesis +
    # regenerates (mutate), or crosses the best PASS pair + pushes offspring (G5).
    # All flags OFF → classify/handle stay None → pipeline byte-identical. Bounds:
    # retry R1B_MAX_RETRIES_PER_ALPHA; mutate R1B_MAX_MUTATION_DEPTH (DB-chained);
    # G5 pair-dedupe + G5_PIPELINE_MAX_CROSSOVERS.
    _retry_on = bool(getattr(settings, "ENABLE_R1B_RETRY_LOOP", False))
    _mutate_on = bool(getattr(settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", False))
    _g5_on = bool(getattr(settings, "ENABLE_G5_CROSSOVER", False))
    _fb_classify = None
    _fb_handle = None
    if _retry_on or _mutate_on or _g5_on:
        from backend.agents.pipeline.types import (
            FEEDBACK_MUTATE, FEEDBACK_PASS_LANDED, FEEDBACK_RETRY,
        )
        _fb_config = {"configurable": {"trace_service": None, "run_id": run_id}}
        _classifiers = []
        _r1b_handle = None
        _g5_handle = None
        if _retry_on or _mutate_on:
            from backend.agents.pipeline.feedback_r1b import (
                build_feedback_classifier,
                build_feedback_handler,
            )
            _classifiers.append(build_feedback_classifier(
                retry_on=_retry_on, mutate_on=_mutate_on,
                max_retries=int(getattr(settings, "R1B_MAX_RETRIES_PER_ALPHA", 3)),
            ))
            _r1b_handle = build_feedback_handler(
                config=_fb_config, mutate_num_alphas=num_alphas,
                max_mutations=int(getattr(settings, "R1B_PIPELINE_MAX_MUTATIONS", 20) or 0))
            # retry/mutate key off _r1a_attribution, written by node_evaluate ONLY
            # under ENABLE_R1A_HOOK (or an R5 override). With neither, the
            # classifier never emits → that part of the loop is inert; warn so a
            # half-configured flip isn't a silent no-op.
            if not (getattr(settings, "ENABLE_R1A_HOOK", False)
                    or getattr(settings, "ENABLE_LLM_JUDGE", False)):
                logger.warning(
                    "[flat-pipeline] task=%s R1b feedback (retry=%s mutate=%s) is on "
                    "but neither ENABLE_R1A_HOOK nor ENABLE_LLM_JUDGE is set — no "
                    "_r1a_attribution will be produced, so retry/mutate stay inert.",
                    task_id, _retry_on, _mutate_on,
                )
        if _g5_on:
            from backend.agents.pipeline.feedback_g5 import (
                build_g5_classifier,
                build_g5_handler,
            )
            _classifiers.append(build_g5_classifier())
            _g5_handle = build_g5_handler(
                run_id=run_id, config=_fb_config,
                top_k=int(getattr(settings, "G5_CROSSOVER_TOP_K_OFFSPRING", 2)),
                min_sharpe=float(getattr(settings, "G5_CROSSOVER_MIN_PARENT_SHARPE", 1.25)),
                require_diff_pillar=bool(getattr(
                    settings, "G5_CROSSOVER_REQUIRE_DIFFERENT_PILLAR", True)),
                max_crossovers=int(getattr(settings, "G5_PIPELINE_MAX_CROSSOVERS", 20)),
            )

        def _fb_classify(result, _cs=_classifiers):
            for _c in _cs:
                _ev = _c(result)
                if _ev is not None:
                    return _ev
            return None

        async def _fb_handle(event, push, db, wf):
            if event.kind in (FEEDBACK_RETRY, FEEDBACK_MUTATE):
                if _r1b_handle is not None:
                    await _r1b_handle(event, push, db, wf)
            elif event.kind == FEEDBACK_PASS_LANDED:
                if _g5_handle is not None:
                    await _g5_handle(event, push, db, wf)

    stats = {}
    async with BrainAdapter() as brain:
        refresher = None
        if _refresh_every > 0:
            from backend.agents.pipeline.client_refresh import BrainClientRefresher
            refresher = BrainClientRefresher(
                refresh_every=_refresh_every,
                refresh_fn=_refresh_brain_client,
                brain=brain,
            )
        # consumer_wf.db is never used by node_simulate/node_evaluate (they open
        # their own ephemeral sessions); pass the idle injected db.
        consumer_wf = MiningWorkflow(db, brain)
        from backend.agents.pipeline.runner import PipelineHeartbeatExpired
        from backend.agents.services.llm_service import (
            set_task_function_overrides, clear_task_function_overrides,
        )
        # PR5: bind this task's per-node model overrides to the async context so
        # the producer/consumer coroutines (created inside run_flat_pipeline_session)
        # inherit a copy — enables single-task single-node A/B (Phase C). Empty /
        # missing → set(None) → no routing change (byte-for-byte legacy).
        _llm_ov_token = set_task_function_overrides((task.config or {}).get("llm_overrides"))
        try:
            stats = await run_flat_pipeline_session(
                session_factory=AsyncSessionLocal,
                producer_workflow_factory=lambda pdb: MiningWorkflow(pdb, brain),
                consumer_workflow=consumer_wf,
                next_round_inputs=next_round_inputs,
                run_id=run_id,
                num_alphas=num_alphas,
                num_consumers=num_consumers,
                daily_goal=daily_goal,
                acquire_slot=_noop_acquire,
                release_slot=_noop_release,
                refresher=refresher,
                reward_hook=_reward_hook,
                classify_feedback=_fb_classify,
                handle_feedback=_fb_handle,
                code_producer_count=int(getattr(settings, "SIM_PIPELINE_CODE_PRODUCER_COUNT", 1)),
                op_timeout=_pipeline_op_timeout(),
                heartbeat_timeout_sec=_pipeline_heartbeat_timeout(),
            )
        except PipelineHeartbeatExpired as _hb:
            # The supervisor caught the freeze CLASS (any unwrapped DB op /
            # poisoned asyncpg pool / queue deadlock) and cancelled the session.
            # Finalise as PAUSED so the producer-persisted flat_cursor resumes
            # cleanly on the next dispatch (fresh worker process = fresh pool).
            # Do NOT re-raise — let the standard finalisation below mark the run
            # PAUSED via the task.status refresh path.
            logger.warning(
                f"[flat-pipeline] task={task_id} HEARTBEAT-ABORT: {_hb} — "
                f"marking task PAUSED for clean re-dispatch on a fresh worker."
            )
            _stop_reason["code"] = "heartbeat_abort"
            try:
                from sqlalchemy import update as _sa_update
                await db.execute(
                    _sa_update(MiningTask).where(MiningTask.id == task_id).values(status="PAUSED")
                )
                await db.commit()
            except Exception:  # noqa: BLE001 — best-effort; finalisation will retry
                try:
                    await db.rollback()
                except Exception:
                    pass
            stats = {
                "produced": 0, "simulated": 0, "persisted": 0, "errors": 0,
                "slot_timeouts": 0, "persist_failures": 0,
                "heartbeat_aborted": True,
            }
        finally:
            # PR5: session done — drop the per-task overrides binding. finally so
            # it runs on success, on heartbeat-abort, AND if any other exception
            # escapes run_flat_pipeline_session (belt-and-suspenders on top of the
            # per-celery-task asyncio-context isolation).
            clear_task_function_overrides(_llm_ov_token)

    logger.info(
        f"[flat-pipeline] task={task_id} pipeline finished: "
        f"produced={stats.get('produced')} simulated={stats.get('simulated')} "
        f"persisted={stats.get('persisted')} errors={stats.get('errors')} "
        f"slot_timeouts={stats.get('slot_timeouts')} "
        f"persist_failures={stats.get('persist_failures')}"
    )

    # Finalization (mirror the legacy loop): reflect PAUSED/STOPPED vs COMPLETED
    # on the run + sync task.status. The producer persisted runtime_state on its
    # own session, so refresh run here before touching it on the injected db.
    if run is not None:
        # Stop-reason inference for natural completion paths (set neither in
        # next_round_inputs nor in heartbeat-abort) — distinguishes
        # daily_goal_reached from a generic "completed". Falls back to
        # "completed" if nothing else fits.
        if _stop_reason["code"] is None:
            if state["iterations"] >= max_iters:
                _stop_reason["code"] = "max_iters_reached"
            elif stats.get("persisted", 0) >= daily_goal:
                _stop_reason["code"] = "daily_goal_reached"
            else:
                _stop_reason["code"] = "completed"

        # Persist stop_reason for observability (task.config for task-list
        # speed-read, run.runtime_state for per-run history). Best-effort —
        # never fail finalization on a serialisation issue.
        try:
            await db.refresh(task)
            await db.refresh(run)
        except Exception as _refresh_ex:  # noqa: BLE001
            # Refresh失败 → task.status/run.runtime_state 是 stale 内存状态。
            # 后续 finalize 分支会在 stale 上判断 PAUSED/STOPPED/COMPLETED;若同时
            # 外部 PATCH 把 task PAUSE,会被误覆盖 COMPLETED。低概率 race,但留
            # audit trail 方便事后定位。
            logger.warning(
                f"[flat-pipeline] task={task_id} finalization refresh failed "
                f"(task.status may be stale → external PAUSE race possible): {_refresh_ex}"
            )
        try:
            if isinstance(run.runtime_state, dict):
                run.runtime_state["stop_reason"] = _stop_reason["code"]
                flag_modified(run, "runtime_state")
            if task.config is None:
                task.config = {}
            task.config["last_stop_reason"] = _stop_reason["code"]
            task.config["last_stop_reason_at"] = datetime.utcnow().isoformat()
            flag_modified(task, "config")
        except Exception as _sr_ex:  # noqa: BLE001
            logger.warning(
                f"[flat-pipeline] stop_reason persist failed (non-fatal): {_sr_ex}"
            )

        if _stop_reason["ownership_lost"]:
            # Ownership was taken over mid-session — a replacement worker is
            # meant to continue the TASK from the preserved cursor. Do NOT touch
            # task.status (that would race the new owner / falsely COMPLETE it).
            # But DO close THIS (superseded) run so it doesn't linger as an
            # orphan RUNNING row — otherwise the watchdog's trace-liveness probe
            # / the UI see a dead run as alive (the run-1196 orphan class).
            logger.info(
                f"[flat-pipeline] task={task_id} exited on ownership loss — "
                f"closing this superseded run, leaving task for the new owner"
            )
            try:
                if run.status == "RUNNING":
                    run.status = "STOPPED"
                    run.finished_at = datetime.utcnow()
                    await db.commit()
            except Exception as _own_ex:  # noqa: BLE001
                logger.warning(f"[flat-pipeline] superseded-run close failed: {_own_ex}")
        else:
            if task.status in ("PAUSED", "STOPPED"):
                run.status = task.status
            else:
                run.status = "COMPLETED"
                if task.status == "RUNNING":
                    task.status = "COMPLETED"
            run.finished_at = datetime.utcnow()
            try:
                await db.commit()
            except Exception as _fin_ex:  # noqa: BLE001
                logger.error(f"[flat-pipeline] finalization commit failed: {_fin_ex}")

    # Orchestrator Sub-phase 2 (Q2/Q7 DECIDED 2026-05-29): 事件主路径 — 投递
    # 评估事件,由 orchestrator_evaluate_after_finalize 决策是否 launch 下一个。
    # flag OFF default → orchestrator 自身立即 return,本投递是 cheap no-op
    # (queue 一条 trivial 消息)。投递失败 → cron 1h fallback 兜底,不阻塞 finalize。
    try:
        from backend.tasks.orchestrator import orchestrator_evaluate_after_finalize
        orchestrator_evaluate_after_finalize.delay(task_id)
    except Exception as _orch_ex:  # noqa: BLE001
        logger.warning(
            f"[flat-pipeline] task={task_id} orchestrator event dispatch failed "
            f"(non-fatal,cron fallback 兜底): {_orch_ex}"
        )

    return {"total_alphas": stats.get("persisted", 0), "pipeline_stats": stats}


