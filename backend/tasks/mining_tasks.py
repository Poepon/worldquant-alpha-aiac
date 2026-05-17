"""
Mining Tasks - Background tasks for alpha mining

Contains the main mining task execution logic.
"""

import asyncio
import os
from datetime import datetime
from sqlalchemy import select, update, func
from sqlalchemy.orm.attributes import flag_modified  # Phase 1.5-B JSONB dirty trigger
from loguru import logger

from backend.celery_app import celery_app
from backend.config import settings
from backend.database import AsyncSessionLocal
from backend.agents import MiningAgent
from backend.adapters.brain_adapter import BrainAdapter
from backend.models import MiningTask, DatasetMetadata, Operator, DataField, ExperimentRun
from backend.tasks import run_async


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
            if task.mining_mode == "CONTINUOUS_CASCADE":
                try:
                    return await _run_continuous_cascade(
                        db, task, run, self.request.id,
                        lock_key=cascade_lock_key,
                        lock_token=cascade_lock_token,
                    )
                except Exception as e:
                    logger.error(f"[cascade] Task {task_id} failed: {e}")
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
                        logger.error(f"[cascade] failed to mark task FAILED: {db_err}")
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
                            # input). With T1's expand_t1_strategy producing
                            # daily_goal × 1.5 candidates, daily_goal=4 → 6
                            # candidates/round, daily_goal=20 → 30 candidates.
                            num_per_round = task.daily_goal if task.daily_goal else 4
                            result = await mining_agent.run_evolution_loop(
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
    op_query = select(Operator).where(Operator.is_active == True)
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

    fields_stmt = select(DataField).where(DataField.dataset_id == ds_meta.id)
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
# default disabled (IX-4 — V-16 suspicion mode pre-emptively rejects
# sharpe>3 alphas, T3 PASS rate=0% in spike).
#
# State persistence: cascade_phase + cascade_round_idx written after each
# phase boundary so worker crash + watchdog restart resumes mid-cascade
# (V-19.4 + V-19.7 collaboration).

async def _count_pass_in_task(db, task_id: int, tier: int) -> int:
    """Count PASS alphas of a given tier owned by THIS task.
    Used by IX-1 strict closure: T2 seed = local PASS only when ≥ MIN.
    """
    from backend.models import Alpha
    q = (
        select(func.count(Alpha.id))
        .where(Alpha.task_id == task_id)
        .where(Alpha.factor_tier == tier)
        .where(Alpha.quality_status == "PASS")
    )
    return (await db.execute(q)).scalar() or 0


async def _count_pass_global_region(db, region: str, tier: int) -> int:
    """Count PASS alphas of a given tier across the whole region (any task).
    Used by IX-1 fallback: when local PASS<MIN, try the historical pool.
    """
    from backend.models import Alpha
    q = (
        select(func.count(Alpha.id))
        .where(Alpha.region == region)
        .where(Alpha.factor_tier == tier)
        .where(Alpha.quality_status == "PASS")
    )
    return (await db.execute(q)).scalar() or 0


_TIER_TO_AGENT_MODE = {
    1: "AUTONOMOUS_TIER1",
    2: "AUTONOMOUS_TIER2",
    3: "AUTONOMOUS_TIER3",
}


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


async def _run_one_round_inline(
    db, task, run, brain, mining_agent, operators,
    *, dataset_id: str, tier: int,
) -> dict:
    """Run one round on the foreground session. Returns mining_agent result
    dict (or empty dict on failure)."""
    fields = await _prepare_round_fields(db, task, dataset_id)
    if fields is None:
        return {"all_alphas": [], "iterations_completed": 0, "skipped": True}

    available_dataset_pool = await _build_dataset_pool(db, task, dataset_id)
    active_level = _get_active_level(task)

    try:
        return await mining_agent.run_evolution_loop(
            task=task, dataset_id=dataset_id, fields=fields, operators=operators,
            max_iterations=1, target_alphas=999999,
            num_alphas_per_round=task.daily_goal if task.daily_goal else 4,
            run_id=run.id,
            available_dataset_pool=available_dataset_pool,
            hypothesis_centric_level=active_level,
            experiment_variant=str(
                (task.config or {}).get("hypothesis_centric_variant", active_level)
            ),
            factor_tier_override=tier,
        )
    except Exception as e:
        logger.error(f"[cascade T{tier} {dataset_id}] inline round failed: {e}")
        try:
            await db.rollback()
            await db.refresh(task)
        except Exception:
            pass
        return {"all_alphas": [], "iterations_completed": 0, "error": str(e)}


async def _prefetch_round_isolated(
    task_id: int, run_id: int, dataset_id: str, tier: int,
) -> dict:
    """V-20 (2026-05-10): run one round in an isolated DB session and Brain
    adapter. Used by cascade pipeline — while the foreground round is
    mid-SIMULATE (BRAIN-bound, ~5 min), this prefetched round runs
    LLM/CODE_GEN/VALIDATE concurrently and queues at SIMULATE on the redis
    BRAIN slot semaphore. The slot wait gives near-perfect overlap of LLM-
    CPU and BRAIN-IO, ~30% throughput boost when LLM stage <= SIMULATE.

    Independent session avoids races on the foreground task's db.commit /
    refresh; Brain adapter shares the redis slot counter so global account
    limit (3 sims) is honored across foreground + prefetch.
    """
    from backend.database import AsyncSessionLocal

    async with AsyncSessionLocal() as iso_db:
        task = (
            await iso_db.execute(select(MiningTask).where(MiningTask.id == task_id))
        ).scalar_one_or_none()
        if task is None:
            return {"all_alphas": [], "iterations_completed": 0, "skipped": True}
        # Quick pause check before opening BRAIN connection
        if task.status in ("PAUSED", "STOPPED", "EARLY_STOPPED"):
            return {"all_alphas": [], "iterations_completed": 0, "skipped": True}

        fields = await _prepare_round_fields(iso_db, task, dataset_id)
        if fields is None:
            return {"all_alphas": [], "iterations_completed": 0, "skipped": True}

        available_dataset_pool = await _build_dataset_pool(iso_db, task, dataset_id)
        active_level = _get_active_level(task)

        async with BrainAdapter() as iso_brain:
            iso_agent = MiningAgent(iso_db, iso_brain)
            iso_operators = await _get_operators(iso_db)
            try:
                return await iso_agent.run_evolution_loop(
                    task=task, dataset_id=dataset_id, fields=fields,
                    operators=iso_operators,
                    max_iterations=1, target_alphas=999999,
                    num_alphas_per_round=task.daily_goal if task.daily_goal else 4,
                    run_id=run_id,
                    available_dataset_pool=available_dataset_pool,
                    hypothesis_centric_level=active_level,
                    experiment_variant=str(
                        (task.config or {}).get("hypothesis_centric_variant", active_level)
                    ),
                    factor_tier_override=tier,
                )
            except Exception as e:
                logger.error(f"[prefetch T{tier} {dataset_id}] failed: {e}")
                return {"all_alphas": [], "iterations_completed": 0, "error": str(e)}


def _verify_cascade_ownership(lock_key: str, token: str, *, where: str) -> bool:
    """V-27.1: round-boundary cascade-lock ownership self-check. Returns True
    if the worker should keep running, False if it should exit gracefully.

      OWNED   → True  (we still hold the lock)
      UNKNOWN → True  (Redis blip — a transient error must NEVER make a live
                       worker self-terminate; the RCA safety floor. If every
                       cascade worker self-killed on a Redis hiccup that is
                       strictly worse than the original double-run bug.)
      LOST    → False (watchdog took over; a replacement worker is running)
      MISSING → False (lock vanished — TTL expired / cleared; don't keep
                       running unprotected)

    Returns True unconditionally when CASCADE_LOCK_TAKEOVER_ENABLED is off,
    so the flag is a full kill-switch for the new self-exit path.
    """
    if not getattr(settings, "CASCADE_LOCK_TAKEOVER_ENABLED", True):
        return True
    from backend.tasks.redis_pool import renew_cascade_lock, verify_lock_ownership
    state = verify_lock_ownership(lock_key, token)
    if state in ("OWNED", "UNKNOWN"):
        if state == "OWNED":
            # V-27.1 followup: renew the TTL at every round boundary. The lock
            # TTL (CASCADE_LOCK_TTL_SEC, default 3h) is shorter than a
            # CONTINUOUS_CASCADE worker's lifetime — without renewal a healthy
            # long-running worker lets the lock expire under it, then
            # self-terminates (MISSING) on the next boundary, and the watchdog
            # can take over the freed lock mid-round (a fresh double-run path).
            _ttl = getattr(settings, "CASCADE_LOCK_TTL_SEC", 10800)
            if not renew_cascade_lock(lock_key, token, _ttl):
                logger.warning(
                    f"[cascade-ownership] {where}: lock renew returned 0 "
                    f"(redis blip or token mismatch) — continuing; the next "
                    f"boundary check will catch a genuine loss"
                )
        elif state == "UNKNOWN":
            logger.warning(
                f"[cascade-ownership] {where}: redis UNKNOWN — continuing "
                f"(a transient error must not self-terminate a live worker)"
            )
        return True
    logger.warning(
        f"[cascade-ownership] {where}: lock state={state} — this worker has "
        f"been taken over, exiting gracefully"
    )
    return False


async def _run_cascade_phase(
    db,
    task,
    run,
    brain,
    mining_agent,
    operators,
    *,
    tier: int,
    max_rounds: int,
    lock_key: str,
    lock_token: str,
) -> dict:
    """Run a cascade phase for `tier` for `max_rounds` total rounds.

    V-20 (2026-05-10) round-pipeline: when CASCADE_PIPELINE_ENABLED, round
    N+1's LLM/CODE_GEN/VALIDATE runs in a background task with isolated DB
    session while round N awaits BRAIN simulate. BRAIN slot semaphore (redis)
    naturally serializes the SIMULATE step across foreground + prefetch.
    Disable via CASCADE_PIPELINE_ENABLED=False to fall back to serial.

    V-19.10 C1 fix: passes factor_tier_override into mining_agent instead
    of mutating task.agent_mode (which would persist via auto-flush).

    Returns: {alphas_added, rounds_run, paused}
    """
    from backend.config import settings

    # V-26.30 (2026-05-13): cascade-stuck-T2 RCA file-marker downgraded to
    # env-gated diagnostic. Pre-fix this wrote .cascade_phase_diag.log
    # unconditionally — useful during the RCA but noisy in production and
    # impossible to disable without code edits. Now opt-in via env
    # CASCADE_PHASE_DIAG_FILE=1; default route goes through loguru.
    import os as _os
    _phase_diag_enabled = _os.environ.get("CASCADE_PHASE_DIAG_FILE") == "1"

    def _phase_diag(msg: str) -> None:
        if not _phase_diag_enabled:
            return
        try:
            from datetime import datetime as _dt
            with open(".cascade_phase_diag.log", "a", encoding="utf-8") as _fp:
                _fp.write(f"{_dt.utcnow().isoformat()} task={task.id} {msg}\n")
        except Exception:
            pass

    _phase_diag(f"_run_cascade_phase ENTER tier={tier} max_rounds={max_rounds}")

    # Pick datasets fresh per phase (T1's main pool may differ from T2/T3
    # which need predecessor PASS alphas as seeds — node_tier_seed_load
    # handles seed plumbing internally given factor_tier > 1).
    datasets = await _get_datasets_to_mine(db, task)
    if not datasets:
        logger.warning(f"[cascade T{tier}] no datasets, phase skipped")
        _phase_diag(f"_run_cascade_phase EXIT_NO_DATASETS tier={tier}")
        return {"alphas_added": 0, "rounds_run": 0, "paused": False}
    _phase_diag(f"_run_cascade_phase tier={tier} datasets={len(datasets)} {datasets[:3]}...")

    # Plan round sequence: distribute max_rounds across datasets, 1 round
    # per call. With 10 datasets / 10 rounds: 1 round per dataset.
    rounds_per_ds = max(1, max_rounds // len(datasets))
    round_plan: list = []
    for ds in datasets:
        for _ in range(rounds_per_ds):
            if len(round_plan) >= max_rounds:
                break
            round_plan.append(ds)
        if len(round_plan) >= max_rounds:
            break

    pipeline_enabled = bool(getattr(settings, "CASCADE_PIPELINE_ENABLED", True))
    logger.info(
        f"[cascade T{tier}] phase begin: rounds_planned={len(round_plan)} "
        f"datasets={len(datasets)} pipeline={pipeline_enabled}"
    )

    alphas_added = 0
    rounds_run = 0
    paused = False

    async def _stamp_heartbeat(round_result: dict | None = None) -> None:
        # V-19.10 H1: heartbeat at every round boundary regardless of PASS count.
        # V-26.3 (2026-05-13): also accumulate progress_current on cascade path.
        # Phase 1.5-B (2026-05-17) [V1.2-B2 fix]: switched from bulk SQL
        # `update(MiningTask)...values()` to instance-level mutation so the
        # in-memory `task` and `run` stay coherent post-commit. Previously
        # bulk SQL bypassed the ORM identity map → split-brain reads of
        # `task.last_alpha_persisted_at` returned stale-vs-fresh values.
        # Cascade lock token guarantees exactly 1 writer per task (no
        # concurrent prefetch race) so instance-level is race-safe at
        # this layer. If Phase 2+ adds a concurrent writer to
        # `progress_current` from outside the cascade worker, revisit this.
        # Also dual-writes Phase 1.5-A runtime_state["last_persisted_at"]
        # + ["round_idx"] + ["progress"] to ExperimentRun for Phase 1.5-C
        # cut-over readers.
        try:
            from datetime import timezone as _tz
            success_count = int((round_result or {}).get("total_success", 0) or 0)
            now_utc = datetime.now(_tz.utc)

            # --- Instance-level write to MiningTask (replaces bulk UPDATE) ---
            task.last_alpha_persisted_at = now_utc
            if success_count > 0:
                task.progress_current = (task.progress_current or 0) + success_count

            # --- Phase 1.5-B dual-write to ExperimentRun.runtime_state ---
            if run is not None and isinstance(run.runtime_state, dict):
                new_state = dict(run.runtime_state)
                new_state["last_persisted_at"] = now_utc.isoformat()
                new_state["round_idx"] = (new_state.get("round_idx") or 0) + 1
                if success_count > 0:
                    new_state["progress"] = (new_state.get("progress") or 0) + success_count
                run.runtime_state = new_state
                flag_modified(run, "runtime_state")

            await db.commit()
            # task and run instances reflect committed state — no refresh needed
        except Exception as _e:
            logger.warning(f"[cascade T{tier}] heartbeat update failed: {_e}")
            try:
                await db.rollback()
            except Exception:
                pass

    # V-20.1 (2026-05-10) FIX: V-20 originally scheduled the next-round
    # prefetch AFTER awaiting the current round, which made the main loop
    # block on the prefetch task (= effectively serial — confirmed via
    # trace_steps showing zero overlap between round N's SAVE and round
    # N+1's RAG). The pipeline only delivers throughput when at least
    # 2 rounds are in-flight simultaneously. Scheduling pattern:
    #   1. pre-schedule round 0 task before entering the loop
    #   2. inside loop: schedule round i+1 task BEFORE awaiting round i
    # That keeps `current` and `next` running in parallel — when current
    # finishes (BRAIN sim done), next is already through LLM/CODE_GEN
    # and queued at the BRAIN slot semaphore.
    if not pipeline_enabled or len(round_plan) == 0:
        # Serial fallback — kept for compatibility / debugging
        for dataset_id in round_plan:
            await db.refresh(task)
            if task.status in ("PAUSED", "STOPPED", "EARLY_STOPPED"):
                paused = True
                break
            result = await _run_one_round_inline(
                db, task, run, brain, mining_agent, operators,
                dataset_id=dataset_id, tier=tier,
            )
            rounds_run += int(result.get("iterations_completed") or 0)
            alphas_added += len(result.get("all_alphas", []))
            # V-26.3: pass result so progress_current advances with PASS count.
            await _stamp_heartbeat(result)
            # V-27.1: round-boundary ownership self-check. If the watchdog
            # took over this task's lock, a replacement worker is running —
            # exit gracefully rather than burn another round's BRAIN quota.
            if not _verify_cascade_ownership(
                lock_key, lock_token, where=f"T{tier} serial round boundary"
            ):
                return {"alphas_added": alphas_added, "rounds_run": rounds_run, "paused": True}
        return {"alphas_added": alphas_added, "rounds_run": rounds_run, "paused": paused}

    # V-20.1 pipeline path — two-deep lookahead.
    def _spawn(idx: int) -> "asyncio.Task":
        ds = round_plan[idx]
        return asyncio.create_task(
            _prefetch_round_isolated(task.id, run.id, ds, tier),
            name=f"cascade-T{tier}-R{idx}-{ds}",
        )

    # Pre-schedule round 0
    current: "asyncio.Task | None" = _spawn(0)
    current_label = f"R0-{round_plan[0]}"
    next_task: "asyncio.Task | None" = None
    next_label = ""

    async def _cancel_remaining():
        """V-26.29 (2026-05-13): cancel + await any still-running pipeline
        tasks. The for-loop's normal exit paths handle PAUSED already, but
        an unhandled exception inside the loop body (db.refresh failure,
        OOM, etc.) would leave `current`/`next_task` running in background
        — they'd write rows to the DB after the orchestrator gave up,
        producing orphan alphas / heartbeat noise. Wrapped around the
        for-loop in a try/finally so all exits clean up.
        """
        for t, label in ((current, current_label), (next_task, next_label)):
            if t is None or t.done():
                continue
            logger.warning(
                f"[cascade T{tier}] V-26.29 cancelling leaked task {label}"
            )
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    try:
        for i, dataset_id in enumerate(round_plan):
            # PAUSE check before scheduling next
            await db.refresh(task)
            if task.status in ("PAUSED", "STOPPED", "EARLY_STOPPED"):
                paused = True
                for t, label in ((current, current_label), (next_task, next_label)):
                    if t and not t.done():
                        logger.info(f"[cascade T{tier}] cancelling {label} (paused)")
                        t.cancel()
                        try:
                            await t
                        except (asyncio.CancelledError, Exception):
                            pass
                break

            # SCHEDULE NEXT ROUND BEFORE AWAITING CURRENT — V-20.1 key change.
            # next_task starts running immediately; while we await current's
            # SIMULATE step (BRAIN-IO bound), next_task progresses through
            # LLM stage and queues at the BRAIN slot semaphore.
            if i + 1 < len(round_plan) and next_task is None:
                next_task = _spawn(i + 1)
                next_label = f"R{i+1}-{round_plan[i+1]}"

            # Await current round
            try:
                result = await current
                logger.info(
                    f"[cascade T{tier}] consumed {current_label} "
                    f"alphas={len(result.get('all_alphas', []))} "
                    f"iters={result.get('iterations_completed')}"
                )
            except asyncio.CancelledError:
                result = {"all_alphas": [], "iterations_completed": 0}
            except Exception as e:
                logger.error(f"[cascade T{tier}] {current_label} failed: {e}")
                result = {"all_alphas": [], "iterations_completed": 0}

            rounds_run += int(result.get("iterations_completed") or 0)
            alphas_added += len(result.get("all_alphas", []))
            # V-26.3: pass round result so PASS count flows to progress_current.
            await _stamp_heartbeat(result)

            # V-27.1: round-boundary ownership self-check. If the watchdog
            # took over this task's lock, a replacement worker is running —
            # cancel any in-flight prefetch first (so it doesn't write orphan
            # rows after we give up, cf. V-26.29) then exit gracefully.
            if not _verify_cascade_ownership(
                lock_key, lock_token, where=f"T{tier} pipeline round boundary"
            ):
                await _cancel_remaining()
                return {"alphas_added": alphas_added, "rounds_run": rounds_run, "paused": True}

            # Promote next → current; schedule a fresh next if budget remaining
            current = next_task
            current_label = next_label
            next_task = None
            next_label = ""

            # If pipeline still has work past i+1, immediately schedule i+2 so
            # there are always 2 rounds in flight (until budget exhausts).
            if i + 2 < len(round_plan):
                await db.refresh(task)
                if task.status in ("PAUSED", "STOPPED", "EARLY_STOPPED"):
                    paused = True
                    if current and not current.done():
                        current.cancel()
                        try:
                            await current
                        except (asyncio.CancelledError, Exception):
                            pass
                    break
                next_task = _spawn(i + 2)
                next_label = f"R{i+2}-{round_plan[i+2]}"
    finally:
        # V-26.29: belt-and-braces cleanup. If an unhandled exception
        # propagates out of the for-loop body, kill any background tasks
        # the pipeline scheduled so they don't keep writing rows after the
        # orchestrator gave up.
        await _cancel_remaining()

    return {"alphas_added": alphas_added, "rounds_run": rounds_run, "paused": paused}


async def _run_continuous_cascade(db, task, run, celery_task_id, *, lock_key, lock_token):
    """V-19 main loop — persistent mining service.

    Repeats T1 → T2 → T3 cycles until the user pauses (status='PAUSED' via
    POST /mining-session/stop) or the celery worker crashes (V-19.7 watchdog
    restarts it; we resume from task.cascade_phase).

    Phase skip rules (IX-1 hybrid C + IX-4):
      T2: skip if local PASS T1 < MIN_TIER_SEED_COUNT AND global region T1 < MIN
      T3: skip if CASCADE_ENABLE_T3 = False (default) OR same seed-shortage as T2
    """
    from backend.config import settings

    logger.info(
        f"[cascade] task={task.id} region={task.region} starting at "
        f"phase={task.cascade_phase or 'T1'} round_idx={task.cascade_round_idx}"
    )

    # V-26.30: env-gated diagnostic; default OFF in production.
    import os as _os
    _outer_diag_enabled = _os.environ.get("CASCADE_PHASE_DIAG_FILE") == "1"

    def _outer_diag(msg: str) -> None:
        if not _outer_diag_enabled:
            return
        try:
            from datetime import datetime as _dt
            with open(".cascade_phase_diag.log", "a", encoding="utf-8") as _fp:
                _fp.write(f"{_dt.utcnow().isoformat()} task={task.id} OUTER {msg}\n")
        except Exception:
            pass

    total_alphas = 0
    async with BrainAdapter() as brain:
        mining_agent = MiningAgent(db, brain)
        operators = await _get_operators(db)

        while True:
            # Refresh + check status before starting a new phase / round
            await db.refresh(task)
            if task.status in ("PAUSED", "STOPPED", "EARLY_STOPPED"):
                logger.info(
                    f"[cascade] task={task.id} status={task.status}, exiting main loop"
                )
                _outer_diag(f"exit_status={task.status}")
                break

            # V-27.1: ownership self-check at the outer loop top. Catches the
            # case where a phase is skipped (T2/T3 seed shortage) so the
            # per-round checks inside _run_cascade_phase never run — without
            # this a taken-over worker could spin the outer loop indefinitely.
            if not _verify_cascade_ownership(
                lock_key, lock_token, where="cascade outer loop top"
            ):
                _outer_diag("exit_taken_over")
                break

            # 2026-05-11: proactively refresh BRAIN session at every phase
            # boundary. Token expires ~4h; a single cascade phase can run
            # ~1h, so we'd hit token expiry mid-T1 or mid-T2 if cascade
            # cycles 4+ times.
            #
            # V-26.2 (2026-05-13): the original ensure_session() short-
            # circuited on Redis cache hit without checking actual /authentication
            # expiry. That meant Redis-cached tokens 30min from expiry would
            # be reused at the boundary and then expire mid-phase. Pass
            # force_refresh=True so the Redis short-circuit is bypassed and
            # _is_session_valid actually probes the BRAIN token expiry field.
            try:
                await brain.ensure_session(force_refresh=True)
            except Exception as _es:
                logger.warning(f"[cascade] ensure_session at phase boundary failed: {_es}")

            # Resume: if cascade_phase is None or invalid, start at T1.
            # V-26.28 (2026-05-13): if the column holds an unrecognized value
            # (manual DB edit, future enum extension that this worker doesn't
            # know about) all three phase branches below silently no-op and
            # the `while True` becomes a busy loop. Normalize to T1 with a
            # WARNING so the situation is observable.
            current_phase = task.cascade_phase or "T1"
            if current_phase not in {"T1", "T2", "T3"}:
                logger.warning(
                    f"[cascade] task={task.id} V-26.28 unrecognized cascade_phase="
                    f"{task.cascade_phase!r}; resetting to T1"
                )
                current_phase = "T1"
                task.cascade_phase = "T1"
                await db.commit()
            _outer_diag(f"loop_top current_phase={current_phase} round_idx={task.cascade_round_idx}")

            # ============================================================
            # T1 phase
            # ============================================================
            if current_phase == "T1":
                logger.info(
                    f"[cascade] task={task.id} T1 phase begin "
                    f"(round_idx={task.cascade_round_idx} budget={settings.CASCADE_T1_ROUNDS})"
                )
                _outer_diag(f"T1_phase_begin round_idx={task.cascade_round_idx}")
                phase_result = await _run_cascade_phase(
                    db, task, run, brain, mining_agent, operators,
                    tier=1, max_rounds=settings.CASCADE_T1_ROUNDS,
                    lock_key=lock_key, lock_token=lock_token,
                )
                _outer_diag(f"T1_phase_end result={phase_result}")
                total_alphas += phase_result["alphas_added"]
                if phase_result["paused"]:
                    break
                # Move to T2
                task.cascade_phase = "T2"
                # Phase 1.5-B dual-write current_tier on ExperimentRun.runtime_state
                if run is not None and isinstance(run.runtime_state, dict):
                    run.runtime_state = {**run.runtime_state, "current_tier": 2}
                    flag_modified(run, "runtime_state")
                await db.commit()
                current_phase = "T2"

            await db.refresh(task)
            if task.status in ("PAUSED", "STOPPED", "EARLY_STOPPED"):
                break

            # ============================================================
            # T2 phase — IX-1 fallback hybrid C (local first, global fallback)
            # ============================================================
            if current_phase == "T2":
                local_t1 = await _count_pass_in_task(db, task.id, tier=1)
                global_t1 = await _count_pass_global_region(db, task.region, tier=1)
                if local_t1 >= settings.MIN_TIER_SEED_COUNT or global_t1 >= settings.MIN_TIER_SEED_COUNT:
                    logger.info(
                        f"[cascade] task={task.id} T2 phase begin "
                        f"(local_T1_PASS={local_t1} global_T1_PASS={global_t1})"
                    )
                    phase_result = await _run_cascade_phase(
                        db, task, run, brain, mining_agent, operators,
                        tier=2, max_rounds=settings.CASCADE_T2_ROUNDS,
                        lock_key=lock_key, lock_token=lock_token,
                    )
                    total_alphas += phase_result["alphas_added"]
                    if phase_result["paused"]:
                        break
                else:
                    logger.info(
                        f"[cascade] task={task.id} T2 phase SKIP: "
                        f"local_T1_PASS={local_t1} global_T1_PASS={global_t1} both<{settings.MIN_TIER_SEED_COUNT}"
                    )
                # Move to T3
                task.cascade_phase = "T3"
                # Phase 1.5-B dual-write current_tier on ExperimentRun.runtime_state
                if run is not None and isinstance(run.runtime_state, dict):
                    run.runtime_state = {**run.runtime_state, "current_tier": 3}
                    flag_modified(run, "runtime_state")
                await db.commit()
                current_phase = "T3"

            await db.refresh(task)
            if task.status in ("PAUSED", "STOPPED", "EARLY_STOPPED"):
                break

            # ============================================================
            # T3 phase — IX-4 default disabled
            # ============================================================
            if current_phase == "T3":
                if not settings.CASCADE_ENABLE_T3:
                    logger.info(
                        f"[cascade] task={task.id} T3 phase SKIP: CASCADE_ENABLE_T3=False"
                    )
                else:
                    local_t2 = await _count_pass_in_task(db, task.id, tier=2)
                    global_t2 = await _count_pass_global_region(db, task.region, tier=2)
                    if local_t2 >= settings.MIN_TIER_SEED_COUNT or global_t2 >= settings.MIN_TIER_SEED_COUNT:
                        logger.info(
                            f"[cascade] task={task.id} T3 phase begin "
                            f"(local_T2_PASS={local_t2} global_T2_PASS={global_t2})"
                        )
                        phase_result = await _run_cascade_phase(
                            db, task, run, brain, mining_agent, operators,
                            tier=3, max_rounds=settings.CASCADE_T3_ROUNDS,
                            lock_key=lock_key, lock_token=lock_token,
                        )
                        total_alphas += phase_result["alphas_added"]
                        if phase_result["paused"]:
                            break
                    else:
                        logger.info(
                            f"[cascade] task={task.id} T3 phase SKIP: seed shortage"
                        )

                # V-27.1: ownership self-check before committing the round
                # boundary. If taken over, exit before incrementing — the
                # replacement worker owns round-index advancement now.
                if not _verify_cascade_ownership(
                    lock_key, lock_token, where="cascade round-complete boundary"
                ):
                    _outer_diag("exit_taken_over_at_round_complete")
                    break

                # Cascade round complete — increment + reset to T1
                task.cascade_round_idx += 1
                task.cascade_phase = "T1"
                # Phase 1.5-B dual-write current_tier on ExperimentRun.runtime_state
                # round_idx is already bumped by _stamp_heartbeat per-round;
                # here we just reset the tier marker.
                if run is not None and isinstance(run.runtime_state, dict):
                    run.runtime_state = {**run.runtime_state, "current_tier": 1}
                    flag_modified(run, "runtime_state")
                await db.commit()
                _outer_diag(f"T3_phase_end round_idx_new={task.cascade_round_idx} reset_to=T1")
                logger.info(
                    f"[cascade] task={task.id} round {task.cascade_round_idx} complete; "
                    f"total alphas this session: {total_alphas}"
                )

    # Loop exited: PAUSED / STOPPED. Run is wrapped up but task stays in
    # PAUSED state (V-19.4 resume_session can re-dispatch a new worker).
    # V-19.10 H2 fix: don't mark the run COMPLETED if the worker exited
    # because the task was paused/stopped — that misrepresents the run as
    # finished work. Mirror task.status onto the run for accurate history.
    if run is not None:
        await db.refresh(task)
        if task.status in ("PAUSED", "STOPPED"):
            run.status = task.status
        else:
            run.status = "COMPLETED"
        run.finished_at = datetime.utcnow()
    await db.commit()

    logger.info(
        f"[cascade] task={task.id} worker exiting; final phase={task.cascade_phase} "
        f"round_idx={task.cascade_round_idx} total_alphas={total_alphas}"
    )
    return {
        "success": True,
        "mode": "CONTINUOUS_CASCADE",
        "alphas_mined": total_alphas,
        "final_phase": task.cascade_phase,
        "final_round_idx": task.cascade_round_idx,
    }
