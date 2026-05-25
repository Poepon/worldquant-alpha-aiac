"""
Mining Tasks - Background tasks for alpha mining

Contains the main mining task execution logic.
"""

import asyncio
import os
from datetime import datetime
from typing import Optional

from sqlalchemy import select, update, func, case
from sqlalchemy.orm.attributes import flag_modified  # Phase 1.5-B JSONB dirty trigger
from loguru import logger

from backend.celery_app import celery_app
from backend.config import settings
from backend.database import AsyncSessionLocal
from backend.agents import MiningAgent
from backend.adapters.brain_adapter import BrainAdapter
from backend.models import MiningTask, DatasetMetadata, Operator, DataField, ExperimentRun
from backend.tasks import run_async


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

            # Idempotency check (2026-05-03): skip if already in terminal state.
            # Required after the seed-loop bug fix because Celery may redeliver
            # unack'd tasks after worker restart, and we don't want a previously
            # COMPLETED / EARLY_STOPPED task to be re-run.
            if task.status in ("COMPLETED", "FAILED"):
                logger.info(f"Task {task_id} already in terminal state {task.status}, skipping")
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
                        fields = await _get_dataset_fields(db, dataset_id, task.region, task.universe)

                        if not fields:
                            logger.warning(f"No fields found for dataset {dataset_id}, skipping")
                            continue

                        if dataset_id != "pv1":
                            pv_supplement = await _get_universal_pv_fields(db, task.region, task.universe)
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
                            # input).
                            num_per_round = task.daily_goal if task.daily_goal else 4
                            # 2026-05-21: per-round hard deadline (see _run_one_round_inline).
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
    ds_query = (
        select(DatasetMetadata)
        .where(
            DatasetMetadata.region == task.region,
            DatasetMetadata.universe == task.universe
        )
        .order_by(DatasetMetadata.mining_weight.desc(), func.random())
        .limit(10)
    )
    ds_result = await db.execute(ds_query)
    datasets_objs = ds_result.scalars().all()
    return [d.dataset_id for d in datasets_objs]


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
    ds_query = (
        select(DatasetMetadata)
        .where(
            DatasetMetadata.region == task.region,
            DatasetMetadata.universe == task.universe,
            DatasetMetadata.dataset_id != anchor_dataset_id,
        )
        .order_by(DatasetMetadata.mining_weight.desc(), func.random())
        .limit(k)
    )
    rows = (await db.execute(ds_query)).scalars().all()
    return [d.dataset_id for d in rows]


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


async def _get_dataset_fields(db, dataset_id, region, universe):
    """Get fields for a dataset."""
    ds_meta_stmt = select(DatasetMetadata).where(
        DatasetMetadata.dataset_id == dataset_id,
        DatasetMetadata.region == region,
        DatasetMetadata.universe == universe
    )
    ds_meta_res = await db.execute(ds_meta_stmt)
    ds_meta = ds_meta_res.scalar_one_or_none()

    if not ds_meta:
        return []

    # is_active == True (2026-05-22): exclude fields BRAIN rejects as
    # "Invalid data field" — auto-deactivated by prune_invalid_datafields.
    # Without this filter is_active was a dead flag and a stale catalog field
    # (e.g. pv96_eq_dvd_cash_cg_amt, 107 sim failures/wk once the dataset
    # bandit steered onto long-dormant pv96) kept being offered to the LLM.
    # Value fields first (2026-05-23): GROUP-heavy datasets (pv13: 135 GROUP /
    # 30 MATRIX) would otherwise crowd MATRIX/VECTOR value fields out of the
    # downstream [:60]/[:30] caps, leaving the LLM only group fields to (mis)use
    # as value inputs. Order non-GROUP (value) fields ahead of GROUP.
    fields_stmt = (
        select(DataField)
        .where(
            DataField.dataset_id == ds_meta.id,
            DataField.is_active.is_(True),
        )
        .order_by(
            case((DataField.field_type == "GROUP", 1), else_=0),
            DataField.field_id,
        )
    )
    fields_res = await db.execute(fields_stmt)
    fields_objs = fields_res.scalars().all()

    return [
        {
            "id": f.field_id,
            "name": f.field_name,
            "description": f.description,
            "type": f.field_type
        }
        for f in fields_objs
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


async def _get_universal_pv_fields(db, region, universe):
    """Pull the canonical PV fields (price-volume) regardless of which
    dataset is being mined. Returns at most |_UNIVERSAL_PV_FIELDS| entries
    that exist in the datafields table for this region/universe.
    """
    pv_meta_stmt = select(DatasetMetadata).where(
        DatasetMetadata.dataset_id == "pv1",
        DatasetMetadata.region == region,
        DatasetMetadata.universe == universe,
    )
    pv_meta = (await db.execute(pv_meta_stmt)).scalar_one_or_none()
    if not pv_meta:
        return []
    fields_stmt = (
        select(DataField)
        .where(DataField.dataset_id == pv_meta.id)
        .where(DataField.field_id.in_(_UNIVERSAL_PV_FIELDS))
        .where(DataField.is_active.is_(True))
    )
    rows = (await db.execute(fields_stmt)).scalars().all()
    return [
        {
            "id": f.field_id,
            "name": f.field_name,
            "description": f.description,
            "type": f.field_type,
        }
        for f in rows
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
    fields = await _get_dataset_fields(db, dataset_id, task.region, task.universe)
    if not fields:
        return None
    if dataset_id != "pv1":
        pv_supplement = await _get_universal_pv_fields(db, task.region, task.universe)
        fields = _merge_field_pools(fields, pv_supplement)
    return fields


async def _maybe_run_typed_pipeline_round(
    db, task, brain, operators, *, dataset_id: str,
) -> Optional[dict]:
    """R1b.4b (2026-05-18): typed AlphaMiningPipeline dispatch wrapper.

    Plan ~/.claude/plans/phase3-r1b-costeer-loop-2026-05-18.md v1.3 §8.2.
    Returns a result dict shaped like ``_run_one_round_inline`` output
    (``all_alphas`` + ``iterations_completed``) when the typed path is
    active for this task, else ``None`` so the caller falls through to
    the legacy LangGraph round.

    Active conditions per ``is_typed_pipeline_active``:
        - ``ENABLE_R1B_TYPED_PIPELINE=True`` (global flag)
        - ``task.config['hypothesis_centric_variant']==3`` (per-task opt-in)

    Soft-fail: any pipeline exception → ``None`` → legacy path takes over
    (NEVER raises). Mirrors plan §8.6 test_typed_pipeline_falls_back...
    contract.
    """
    try:
        from backend.agents.graph.nodes.r1b_typed_pipeline import (
            is_typed_pipeline_active, run_typed_round,
        )
    except Exception as ex:
        logger.debug(f"[r1b_typed wire] dispatch imports unavailable: {ex}")
        return None
    if not is_typed_pipeline_active(task):
        return None
    fields = await _prepare_round_fields(db, task, dataset_id)
    if fields is None:
        return {"all_alphas": [], "iterations_completed": 0, "skipped": True}
    try:
        typed_result = await run_typed_round(
            task=task, brain=brain, db=db,
            region=task.region, universe=task.universe,
            dataset_id=dataset_id,
            fields=fields, operators=operators,
        )
    except Exception as ex:
        # Per plan §8.6 — typed exception falls through to legacy
        logger.warning(
            f"[r1b_typed wire] typed round raised, falling back to legacy: {ex}"
        )
        return None
    if typed_result.get("skipped_disabled"):
        return None
    # Map typed result shape → legacy round result shape callers expect.
    # Keep typed-specific telemetry under '_r1b_typed_*' keys so
    # downstream telemetry / DAG update code can inspect without colliding
    # with mining_agent's result schema.
    return {
        "all_alphas": typed_result.get("all_alphas", []),
        "iterations_completed": typed_result.get("num_iter_executed", 0),
        "skipped": False,
        "_r1b_typed_cost_usd": typed_result.get("cost_usd", 0.0),
        "_r1b_typed_trace_size": typed_result.get("trace_size", 0),
        "_r1b_typed_abandoned": typed_result.get("abandoned", False),
    }


async def _run_one_round_inline(
    db, task, run, brain, mining_agent, operators,
    *, dataset_id: str, iteration_offset: int = 0,
) -> dict:
    """Run one round on the foreground session. Returns mining_agent result
    dict (or empty dict on failure)."""
    # A+ circuit breaker (2026-05-19): if BRAIN auth is in a known-bad state,
    # skip the entire LangGraph workflow — no hypothesis / code_gen / validate
    # / simulate / evaluate / R5 / R1a LLM cost burnt running a round whose
    # sim will fail. Caller (_run_flat_iteration outer loop) sees
    # skipped=True with reason brain_auth_circuit_open and sleeps before the
    # next iteration so we don't busy-wait.
    try:
        from backend.adapters.brain_adapter import BRAIN_AUTH_CIRCUIT
        if BRAIN_AUTH_CIRCUIT.is_open():
            status = BRAIN_AUTH_CIRCUIT.status()
            logger.warning(
                f"[_run_one_round_inline] BRAIN auth circuit OPEN — "
                f"skipping round dataset={dataset_id} reason="
                f"{status.last_failure_reason!r} reopens_in="
                f"{status.to_dict()['seconds_until_half_open']}s. "
                f"NO LLM cost burnt this round."
            )
            return {
                "all_alphas": [],
                "iterations_completed": 0,
                "skipped": True,
                "skipped_reason": "brain_auth_circuit_open",
                "circuit_reopens_in_sec": status.to_dict()["seconds_until_half_open"],
            }
    except Exception as _circ_e:
        # Soft-fail: Redis blip etc. — DO NOT skip the round, let traffic
        # through (the circuit's own is_open already defaults to False on
        # error, but defend against import-time crashes too).
        logger.debug(f"[_run_one_round_inline] circuit check skipped: {_circ_e}")

    # R1b.2c+v2 wire (2026-05-18): drain any cross-round pending hypothesis
    # from the prior round's hypothesis_mutate, then INJECT it into MiningState
    # via the workflow's configurable so node_hypothesis can use it directly
    # (R1b.2-v2). Flag-gated; soft-fail.
    #
    # R1b.2-v2 plumbing: stash the consumed dict on task.config under a private
    # "__r1b_consumed_pending_hypothesis" slot. workflow.run reads it at
    # initial_state init time and clears the slot (so it's a one-shot per
    # round). This avoids extending 3 layers of kwargs (_run_one_round_inline
    # → run_evolution_loop → run_mining_iteration → workflow.run) while still
    # keeping the consume → inject path round-scoped.
    try:
        from backend.config import settings as _r1b_settings
        if bool(getattr(_r1b_settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", False)):
            from backend.agents.graph.nodes.r1b_persistence import (
                consume_pending_hypothesis,
            )
            _consumed = await consume_pending_hypothesis(task, db)
            if _consumed:
                logger.info(
                    f"[r1b_wire] task={task.id} round-entry consumed pending hypothesis "
                    f"(R1b.2-v2 inject ON): {_consumed.get('statement', '')[:80]}"
                )
                # Stash for workflow.run to pick up + clear (one-shot).
                try:
                    _cfg = dict(getattr(task, "config", None) or {})
                    _cfg["__r1b_consumed_pending_hypothesis"] = _consumed
                    task.config = _cfg
                    try:
                        from sqlalchemy.orm.attributes import flag_modified
                        flag_modified(task, "config")
                    except Exception:
                        pass
                    await db.commit()
                except Exception as _stash_ex:
                    logger.warning(
                        f"[r1b_wire] task={task.id} stash consumed failed (will skip inject): {_stash_ex}"
                    )
    except Exception as _r1b_ex:
        logger.warning(
            f"[r1b_wire] task={task.id} consume_pending_hypothesis failed (round unaffected): {_r1b_ex}"
        )

    # G5 Phase A (2026-05-19): consume offspring stashed by prior round's
    # mining_agent._maybe_run_g5_crossover. Mirror R1b.2-v2 plumbing — stash
    # consumed list on task.config["__g5_consumed_offspring"], workflow.run
    # pops + clears + injects into MiningState.g5_offspring_candidates.
    # Flag-gated; soft-fail全链.
    try:
        from backend.config import settings as _g5_settings
        if bool(getattr(_g5_settings, "ENABLE_G5_CROSSOVER", False)):
            from backend.agents.graph.nodes.g5_persistence import (
                consume_pending_offspring,
            )
            _g5_consumed = await consume_pending_offspring(task, db)
            if _g5_consumed:
                logger.info(
                    f"[g5_wire] task={task.id} round-entry consumed "
                    f"{len(_g5_consumed)} pending offspring"
                )
                try:
                    _cfg = dict(getattr(task, "config", None) or {})
                    _cfg["__g5_consumed_offspring"] = _g5_consumed
                    task.config = _cfg
                    try:
                        from sqlalchemy.orm.attributes import flag_modified
                        flag_modified(task, "config")
                    except Exception:
                        pass
                    await db.commit()
                except Exception as _stash_ex:
                    logger.warning(
                        f"[g5_wire] task={task.id} stash consumed failed: {_stash_ex}"
                    )
    except Exception as _g5_ex:
        logger.warning(
            f"[g5_wire] task={task.id} consume_pending_offspring failed: {_g5_ex}"
        )

    # R1b.4b: route to typed AlphaMiningPipeline when the task opts in via
    # hypothesis_centric_variant=3 + ENABLE_R1B_TYPED_PIPELINE flag.
    typed_result = await _maybe_run_typed_pipeline_round(
        db, task, brain, operators, dataset_id=dataset_id,
    )
    if typed_result is not None:
        return typed_result

    fields = await _prepare_round_fields(db, task, dataset_id)
    if fields is None:
        return {"all_alphas": [], "iterations_completed": 0, "skipped": True}

    available_dataset_pool = await _build_dataset_pool(db, task, dataset_id)
    active_level = _get_active_level(task)

    try:
        # 2026-05-21: per-round hard deadline. Backstop for any unbounded await
        # (Redis/asyncpg/httpx) that the LLM-level wait_for doesn't cover — a hung
        # round must fail cleanly, never freeze the worker. asyncio.TimeoutError
        # is an Exception subclass → caught by the except below → soft-fail dict.
        return await asyncio.wait_for(
            mining_agent.run_evolution_loop(
                task=task, dataset_id=dataset_id, fields=fields, operators=operators,
                max_iterations=1, target_alphas=999999,
                num_alphas_per_round=task.daily_goal if task.daily_goal else 4,
                run_id=run.id,
                available_dataset_pool=available_dataset_pool,
                hypothesis_centric_level=active_level,
                experiment_variant=str(
                    (task.config or {}).get("hypothesis_centric_variant", active_level)
                ),
                iteration_offset=iteration_offset,
            ),
            timeout=settings.MINING_ROUND_TIMEOUT_SEC,
        )
    except Exception as e:
        # 2026-05-25: include the exception TYPE. asyncio.TimeoutError (the
        # per-round wait_for backstop firing on a slow round, e.g. news12) has
        # an EMPTY str(), so `str(e)` logged a blank message AND produced
        # error="" below — which the caller's truthy `if result.get("error")`
        # treated as "no error", skipping the rebuild path and falling through
        # to the success-path cursor commit on a cancel-poisoned session →
        # greenlet_spawn killed the whole task (3504 + 3516, both news12).
        logger.error(
            f"[flat round {dataset_id}] inline round failed: {type(e).__name__}: {e}"
        )
        try:
            await db.rollback()
            await db.refresh(task)
        except Exception:
            pass
        return {
            "all_alphas": [], "iterations_completed": 0,
            "error": (str(e) or type(e).__name__),
        }


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


async def _run_flat_iteration(db, task, run, celery_task_id, *, lock_key, lock_token):
    """Flat session main loop (post tier-system removal, 2026-05-18).

    Hypothesis-driven flat session: iterate over (dataset × hypothesis) tuples.
    Persists ``flat_cursor`` in ``run.runtime_state`` so pause-resume picks up
    where it left off (cursor is inherited into the new ExperimentRun when
    ``TaskService._dispatch_session_worker(inherit_runtime_state=True)`` —
    see Q1 V2 in plan v1.5 §3.6).

    Bounds: ``settings.FLAT_CONTINUOUS_MAX_ITERATIONS`` iterations or
    ``settings.FLAT_CONTINUOUS_DAILY_GOAL`` alphas, whichever fires first.
    Exits cleanly on task.status in (PAUSED, STOPPED, EARLY_STOPPED) at any
    iteration boundary, preserving cursor in runtime_state.
    """
    # 2026-05-25: track the originally-injected session. On round failure we
    # may rebuild a clean replacement (see _rebuild_flat_db_session); the
    # original is owned by run_mining_task._run's `async with`, so only a
    # self-built replacement gets closed by us at the end.
    _orig_db = db
    logger.info(
        f"[flat] task={task.id} region={task.region} starting "
        f"flat session (target_datasets={len(task.target_datasets or [])})"
    )

    max_iters = int(getattr(settings, "FLAT_CONTINUOUS_MAX_ITERATIONS", 100) or 100)
    daily_goal = int(getattr(settings, "FLAT_CONTINUOUS_DAILY_GOAL", 20) or 20)

    # Resolve dataset list — explicit target_datasets or AUTO-pick same as cascade
    datasets = list(task.target_datasets or [])
    if not datasets:
        datasets = await _get_datasets_to_mine(db, task)
    if not datasets:
        logger.warning(f"[flat] task={task.id} no datasets to mine; exiting")
        if run is not None:
            run.status = "COMPLETED"
            run.finished_at = datetime.utcnow()
            await db.commit()
        return {"warning": "no_datasets", "total_alphas": 0}

    # Q1 V2: cursor comes from runtime_state, inherited across pause-resume.
    # HIGH-#5 fix (2026-05-18): cursor is monotonically incremented and we
    # always select via `flat_cursor % len(datasets)` — never compared to
    # `len(datasets)` for termination (single-dataset MANUAL sessions would
    # otherwise COMPLETE after iteration 1 with daily_goal unmet). Resume
    # reads the integer back as-is and keeps incrementing — modulo wrap is
    # applied at access time only.
    flat_cursor = 0
    if isinstance(run.runtime_state, dict):
        flat_cursor = int(run.runtime_state.get("flat_cursor", 0) or 0)

    total_alphas = 0
    # Seed iterations from runtime_state.flat_iterations (inherited on
    # resume via TaskService._dispatch_session_worker(inherit_runtime_state=
    # True)) so trace_steps.iteration advances monotonically across pause-
    # resume boundaries — see 2026-05-19 fix-A in agents/mining_agent.py.
    iterations = 0
    if isinstance(run.runtime_state, dict):
        iterations = int(run.runtime_state.get("flat_iterations", 0) or 0)

    async with BrainAdapter() as brain:
        mining_agent = MiningAgent(db, brain)
        operators = await _get_operators(db)

        # Breadth dataset-steering bandit (2026-05-22, ENABLE_DATASET_VALUE_BANDIT):
        # fetch per-dataset mining_weight once so the iteration loop can
        # weight-sample the next dataset (∝ the bandit's discounted Beta
        # posterior) instead of equal-probability round-robin. OFF →
        # _ds_weight_map stays empty → round-robin (byte-for-byte legacy).
        _bandit_on = bool(getattr(settings, "ENABLE_DATASET_VALUE_BANDIT", False))
        _ds_weight_map: dict = {}
        if _bandit_on:
            try:
                wrows = await db.execute(
                    select(DatasetMetadata.dataset_id, DatasetMetadata.mining_weight).where(
                        DatasetMetadata.region == task.region,
                        DatasetMetadata.universe == task.universe,
                        DatasetMetadata.dataset_id.in_(datasets),
                    )
                )
                _ds_weight_map = {
                    did: float(w if w is not None else 1.0) for did, w in wrows.all()
                }
            except Exception as _wm_err:  # noqa: BLE001 — degrade to round-robin
                logger.warning(f"[flat] dataset-bandit weight fetch failed: {_wm_err}")
                _ds_weight_map = {}

        # 2026-05-25: a single poisoned/timed-out round must not kill the whole
        # FLAT session — count consecutive failures and bail out gracefully
        # (cursor preserved, resumable) only after a small threshold.
        consecutive_round_failures = 0
        _max_round_failures = int(
            getattr(settings, "FLAT_MAX_CONSECUTIVE_ROUND_FAILURES", 3) or 3
        )

        while iterations < max_iters and total_alphas < daily_goal:
            try:
                await db.refresh(task)
            except Exception as _refresh_ex:  # noqa: BLE001
                # Shared session poisoned by a prior round's wait_for-cancel
                # greenlet_spawn — rebuild a clean one before reading status.
                logger.error(
                    f"[flat] task={task.id} session refresh failed "
                    f"(rebuilding clean session): {_refresh_ex}"
                )
                db, task, run, mining_agent = await _rebuild_flat_db_session(
                    db, task.id, run.id, brain
                )
                await db.refresh(task)
            if task.status in ("PAUSED", "STOPPED", "EARLY_STOPPED"):
                logger.info(
                    f"[flat] task={task.id} status={task.status} at cursor={flat_cursor}, "
                    f"exiting (cursor preserved in runtime_state)"
                )
                break

            # HIGH-#4 fix (2026-05-18): V-27.1 ownership self-check at the
            # iteration boundary, mirroring the cascade pattern (cf. line
            # 1267-1273). Without this, a watchdog re-assignment mid-FLAT
            # session lets the old worker keep advancing flat_cursor and
            # writing alphas concurrently with the replacement — the same
            # double-write bug cascade had before V-27.1.
            if not _verify_cascade_ownership(
                lock_key, lock_token, where="flat iteration boundary"
            ):
                logger.info(
                    f"[flat] task={task.id} ownership lost at cursor={flat_cursor}; "
                    f"exiting (cursor preserved in runtime_state)"
                )
                break

            # Dataset choice: round-robin baseline, optionally overridden by
            # the breadth bandit (weight-sample ∝ mining_weight, flag-gated).
            # Steers frequency off the mined-out pv1 toward high-marginal-value
            # + under-mined sources. flat_cursor still advances (drives
            # iteration_offset + counters + pause-resume cursor); only the
            # dataset *choice* changes.
            dataset_id = datasets[flat_cursor % len(datasets)]
            if _bandit_on and _ds_weight_map:
                from backend.selection_strategy import weighted_choice
                _picked = weighted_choice(
                    datasets, [_ds_weight_map.get(d, 1.0) for d in datasets]
                )
                if _picked is not None:
                    dataset_id = _picked

            try:
                result = await _run_one_round_inline(
                    db, task, run, brain, mining_agent, operators,
                    dataset_id=dataset_id,
                    # Pass cumulative round count as offset so the inner
                    # workflow advances trace_steps.iteration past flat-mode
                    # dataset cycles (and pause-resume boundaries).
                    iteration_offset=iterations,
                )
            except Exception as _round_ex:  # noqa: BLE001
                # 2026-05-25: _run_one_round_inline normally soft-fails to an
                # error dict, but a wait_for-cancel greenlet_spawn can defeat
                # even its own rollback and propagate. Treat as a failed round.
                logger.error(
                    f"[flat] task={task.id} round raised out of inline "
                    f"(dataset={dataset_id}): {_round_ex}"
                )
                result = {"all_alphas": [], "error": str(_round_ex)}

            # 2026-05-25: a failed round may have poisoned the shared session
            # (timeout cancel mid asyncpg IO → greenlet_spawn). Rebuild a clean
            # session, advance past this dataset, and continue so one slow /
            # timed-out round can't kill the entire FLAT session and strand the
            # cursor. Bail out only after _max_round_failures in a row.
            # 2026-05-25: `is not None` NOT truthy — a timeout round returns
            # error="" (empty str, falsy), which truthy skipped → fell through
            # to the poisoned success-path cursor commit (3504/3516 root cause).
            if result.get("error") is not None:
                consecutive_round_failures += 1
                logger.error(
                    f"[flat] task={task.id} round failed (dataset={dataset_id}, "
                    f"consecutive={consecutive_round_failures}/"
                    f"{_max_round_failures}): {str(result.get('error'))[:200]}"
                )
                try:
                    db, task, run, mining_agent = await _rebuild_flat_db_session(
                        db, task.id, run.id, brain
                    )
                except Exception as _rebuild_ex:  # noqa: BLE001
                    # 2026-05-25: if even the rebuild can't get a clean session in
                    # this coroutine, don't let it propagate (that is what killed
                    # the task) — bail out; a fresh dispatch (new coroutine +
                    # session) resumes from the preserved cursor.
                    logger.error(
                        f"[flat] task={task.id} session rebuild FAILED post-round "
                        f"— exiting loop for fresh re-dispatch: {_rebuild_ex}"
                    )
                    break
                flat_cursor += 1
                iterations += 1
                # Persist the advanced cursor on the fresh session so a resume
                # skips the bad slot (best-effort; never fatal).
                try:
                    if isinstance(run.runtime_state, dict):
                        run.runtime_state["flat_cursor"] = flat_cursor
                        run.runtime_state["flat_iterations"] = iterations
                        flag_modified(run, "runtime_state")
                        await db.commit()
                except Exception as _cur_ex:  # noqa: BLE001
                    logger.warning(
                        f"[flat] task={task.id} cursor persist after rebuild "
                        f"failed (non-fatal): {_cur_ex}"
                    )
                if consecutive_round_failures >= _max_round_failures:
                    logger.error(
                        f"[flat] task={task.id} {consecutive_round_failures} "
                        f"consecutive round failures — exiting flat loop "
                        f"(cursor={flat_cursor} preserved; resumable)"
                    )
                    break
                continue

            consecutive_round_failures = 0

            # A+ circuit breaker: if BRAIN auth circuit is OPEN the round
            # was skipped — don't busy-loop hitting the same circuit-open
            # check on every dataset. Sleep enough for the circuit's TTL to
            # naturally probe (capped at 60s — long enough to avoid hot
            # loop, short enough to recover quickly when ops re-auths).
            if result.get("skipped") and result.get("skipped_reason") == "brain_auth_circuit_open":
                _wait_sec = min(60, max(5, int(result.get("circuit_reopens_in_sec") or 30)))
                logger.info(
                    f"[flat] task={task.id} BRAIN auth circuit open — "
                    f"sleeping {_wait_sec}s before next iteration (cursor "
                    f"unchanged, iterations unchanged)"
                )
                await asyncio.sleep(_wait_sec)
                # Do NOT advance flat_cursor / iterations / total_alphas —
                # the round didn't run, so the same dataset gets retried.
                continue

            round_alphas = len(result.get("all_alphas") or [])
            total_alphas += round_alphas
            iterations += 1
            flat_cursor += 1

            # Persist cursor + counters into run.runtime_state — flag_modified
            # required because SQLAlchemy doesn't detect JSONB in-place edits
            # (Phase 1.5-B pattern).
            if isinstance(run.runtime_state, dict):
                run.runtime_state["flat_cursor"] = flat_cursor
                run.runtime_state["flat_total_alphas"] = total_alphas
                run.runtime_state["flat_iterations"] = iterations
                flag_modified(run, "runtime_state")
                try:
                    await db.commit()
                except Exception as _succ_commit_ex:  # noqa: BLE001
                    # 2026-05-25 defence: even a round that returned success can
                    # leave the shared session poisoned (e.g. a persistence-layer
                    # savepoint swallowed an error mid-round) — the success-path
                    # cursor commit then hits greenlet_spawn. Guard it so it
                    # rebuilds + continues instead of killing the whole task.
                    # (Fix A/B already routes timeout rounds to the rebuild path;
                    # this covers the round-returned-success-but-poisoned case.)
                    logger.error(
                        f"[flat] task={task.id} success-path cursor commit failed "
                        f"(session poisoned; rebuilding): {_succ_commit_ex}"
                    )
                    try:
                        db, task, run, mining_agent = await _rebuild_flat_db_session(
                            db, task.id, run.id, brain
                        )
                    except Exception as _rb_ex:  # noqa: BLE001
                        logger.error(
                            f"[flat] task={task.id} rebuild after success-commit "
                            f"failed — exiting loop for fresh re-dispatch: {_rb_ex}"
                        )
                        break
                    continue

            logger.info(
                f"[flat] task={task.id} iter={iterations} dataset={dataset_id} "
                f"round_alphas={round_alphas} total={total_alphas}/{daily_goal}"
            )

            # Phase 4 Sprint 1 A2 (2026-05-19): R14 task_stop_loss check.
            # Counts PASS alpha in this round; updates EMA + consecutive_zero
            # state on task.config[stop_loss_state]; pauses task on trigger.
            # Race fix: CB-skipped rounds reach `continue` above and never
            # touch this code — auto-excluded from counters.
            # Soft-fail: any exception in service → log + continue mining;
            # never block round (task fault tolerance > stop_loss precision).
            try:
                if bool(getattr(settings, "ENABLE_TASK_STOP_LOSS", False)):
                    from backend.services.task_stop_loss_service import (
                        check_should_pause as _stop_loss_check,
                        apply_stop_loss_decision as _stop_loss_apply,
                    )

                    def _pass_count(alphas_list):
                        n = 0
                        for _a in alphas_list:
                            _q = (
                                getattr(_a, "quality_status", None)
                                or (isinstance(_a, dict) and _a.get("quality_status"))
                                or ""
                            )
                            _q_str = getattr(_q, "value", _q) if _q is not None else ""
                            if _q_str == "PASS":
                                n += 1
                        return n

                    _round_pass = _pass_count(result.get("all_alphas") or [])
                    _r14_decision = _stop_loss_check(
                        task,
                        round_pass_count=_round_pass,
                        round_alpha_count=round_alphas,
                    )
                    # Persist EMA/counter state mutation (service set
                    # task.config + flag_modified; commit here).
                    await db.commit()
                    if _r14_decision.should_pause:
                        await _stop_loss_apply(
                            db, task, _r14_decision,
                            extra_meta={
                                "iteration": iterations,
                                "dataset_id": dataset_id,
                                "total_alphas": total_alphas,
                            },
                        )
                        logger.warning(
                            f"[flat] task={task.id} R14 stop_loss TRIGGERED "
                            f"reason={_r14_decision.reason} — exiting flat loop"
                        )
                        break
            except Exception as _r14_ex:  # noqa: BLE001
                logger.warning(
                    f"[flat] task={task.id} R14 stop_loss check failed "
                    f"(non-fatal): {_r14_ex}"
                )

    # HIGH-#5 fix (2026-05-18): mirror cascade's V-19.10 H2 finalization
    # (cf. line 1821-1833). Only mark the run COMPLETED when the loop
    # exited under its own completion criterion (daily_goal hit or
    # max_iters exhausted). If the worker exited because of PAUSED /
    # STOPPED or ownership takeover, reflect that on the run so history
    # isn't misrepresented as finished work.
    if run is not None:
        # 2026-05-25: this runs OUTSIDE the BrainAdapter context, so a rebuild
        # here passes brain=None (no MiningAgent needed for finalization).
        try:
            await db.refresh(task)
        except Exception as _fin_ex:  # noqa: BLE001
            logger.error(
                f"[flat] task={task.id} finalization refresh failed "
                f"(rebuilding clean session): {_fin_ex}"
            )
            db, task, run, _ = await _rebuild_flat_db_session(
                db, task.id, run.id, None
            )
            await db.refresh(task)
        if task.status in ("PAUSED", "STOPPED"):
            run.status = task.status
        else:
            run.status = "COMPLETED"
            # 2026-05-25: also sync task.status — finalization previously only
            # updated run.status, so a FLAT session that finished naturally
            # (daily_goal / max_iters) left mining_tasks.status stuck at RUNNING
            # → watch false-RUNNING/STALE + quota miscount (observed task 3515).
            # The PAUSED/STOPPED branch leaves task untouched (already terminal).
            task.status = "COMPLETED"
        run.finished_at = datetime.utcnow()
        await db.commit()

    # 2026-05-25: if we rebuilt the session mid-loop, run_mining_task._run's
    # `async with` only closes the ORIGINAL db — close the replacement we own.
    if db is not _orig_db:
        try:
            await db.close()
        except Exception:  # noqa: BLE001
            pass

    return {
        "mode": "FLAT_CONTINUOUS",
        "total_alphas": total_alphas,
        "iterations": iterations,
        "final_cursor": flat_cursor,
    }


