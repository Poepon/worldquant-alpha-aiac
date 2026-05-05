"""
Mining Tasks - Background tasks for alpha mining

Contains the main mining task execution logic.
"""

from datetime import datetime
from sqlalchemy import select, update, func
from loguru import logger

from backend.celery_app import celery_app
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

            # Update status to RUNNING
            await db.execute(
                update(MiningTask)
                .where(MiningTask.id == task_id)
                .values(status="RUNNING")
            )
            await db.commit()

            # Create or attach ExperimentRun
            run = await _get_or_create_run(db, task, run_id, self.request.id)

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
