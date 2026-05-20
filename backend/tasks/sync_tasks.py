"""
Sync Tasks - Background tasks for data synchronization

Contains tasks for syncing data from BRAIN platform:
- Datasets
- Datafields
- Operators
- User alphas
"""

from datetime import datetime, timezone, timedelta
from typing import Dict, Optional
from sqlalchemy import select, func, text
from loguru import logger

from backend.celery_app import celery_app
from backend.database import AsyncSessionLocal
from backend.adapters.brain_adapter import BrainAdapter
from backend.models import DatasetMetadata, DataField, Operator, Alpha
from backend.services.correlation_service import CorrelationService
from backend.tasks import run_async
from backend.tasks._role_helpers import read_role_snapshot


@celery_app.task(name="backend.tasks.refresh_portfolio_skeletons_all")
def refresh_portfolio_skeletons_all():
    """V-27.147 beat fallback: refresh the portfolio-skeleton cache for every
    region with submitted alphas.

    AlphaService.submit_alpha refreshes this cache inline on each successful
    submit, but that refresh is best-effort — if it raised (DB blip, etc.)
    the cache could go stale indefinitely and the T1 strategy prompt would
    keep nudging the LLM toward an already-submitted shape. This beat sweep
    is the safety net. Per-region try/except so one failure doesn't skip
    the rest.
    """
    logger.info("[refresh_portfolio_skeletons] V-27.147 beat sweep starting")

    async def _run():
        from backend.agents.seed_pool.portfolio_skeletons import (
            refresh_portfolio_from_db,
        )
        results: Dict[str, object] = {}
        async with AsyncSessionLocal() as db:
            regions = (
                await db.execute(
                    select(Alpha.region)
                    .where(Alpha.date_submitted.isnot(None))
                    .where(Alpha.region.isnot(None))
                    .distinct()
                )
            ).scalars().all()
        for region in regions:
            try:
                n = await refresh_portfolio_from_db(region=region)
                results[region] = n
                logger.info(
                    f"[refresh_portfolio_skeletons] {region}: {n} skeleton rows"
                )
            except Exception as e:
                logger.warning(
                    f"[refresh_portfolio_skeletons] {region} failed: {e}"
                )
                results[region] = f"error: {e}"
        return {"refreshed": results}

    return run_async(_run())


@celery_app.task(name="backend.tasks.refresh_os_correlation_cache")
def refresh_os_correlation_cache():
    """Refresh OS-alpha PnL cache + metrics for all major regions (scheduled).

    Runs daily at 06:30 (after sync-datasets). Two responsibilities:
      1. PnL refresh — feeds fast local self-correlation in evaluation.py,
         avoiding BRAIN /correlations/SELF rate-limit risk.
      2. PR2 — Metrics refresh: pulls fresh is.sharpe/fitness/turnover for
         every OS-active alpha, writes back to alphas table, triggers
         quality_status re-eval via tier thresholds. Demoted alphas write a
         transition audit row (source='daily_beat_os'). Without this, OS-active
         alpha metrics drift silently and the FactorLibrary KPI gets stale.
    """
    logger.info("Refreshing OS correlation cache + metrics...")

    async def _run():
        async with BrainAdapter() as brain:
            svc = CorrelationService(brain)
            results = {}
            for region in ["USA", "CHN", "EUR", "HKG", "JPN"]:
                try:
                    new_count, total = await svc.refresh_os_alpha_cache(
                        region=region, incremental=True
                    )
                    results[region] = {"pnl_new": new_count, "pnl_total": total}
                except Exception as e:
                    logger.warning(f"[refresh_os_corr] {region} PnL failed: {e}")
                    results[region] = {"pnl_error": str(e)}

            # PR2 — Metrics refresh leg. Separate try/except per region so a
            # network blip on one doesn't kill the others.
            for region in ["USA", "CHN", "EUR", "HKG", "JPN"]:
                try:
                    metrics_stats = await _refresh_os_alpha_metrics(brain, region)
                    results.setdefault(region, {}).update(metrics_stats)
                except Exception as e:
                    logger.warning(f"[refresh_os_metrics] {region} failed: {e}")
                    results.setdefault(region, {})["metrics_error"] = str(e)

            # Crisis-window stress test snapshot. Reuses the PnL cache we
            # just refreshed — no extra BRAIN calls. Persisted JSON powers
            # /correlation/crisis-summary without recomputing the full
            # N×N matrix per request.
            for region in ["USA", "CHN", "EUR", "HKG", "JPN"]:
                try:
                    payload = svc.crisis_stress_test(region=region)
                    if payload.get("status") == "ok":
                        svc.save_crisis_snapshot(region, payload)
                        n_alphas = payload.get("baseline", {}).get("n_alphas", 0)
                        results.setdefault(region, {})["crisis_snapshot_n"] = n_alphas
                    else:
                        results.setdefault(region, {})["crisis_snapshot_status"] = payload.get("status")
                except Exception as e:
                    logger.warning(f"[refresh_crisis_snapshot] {region} failed: {e}")
                    results.setdefault(region, {})["crisis_snapshot_error"] = str(e)

            return results

    results = run_async(_run())
    logger.info(f"OS correlation cache refresh: {results}")
    return results


async def _refresh_os_alpha_metrics(brain: "BrainAdapter", region: str) -> Dict:
    """Pull fresh BRAIN metrics for every OS-active alpha in this region,
    write back to alphas table, re-evaluate quality_status against tier
    thresholds. Returns counters for the celery beat result.

    Single failures are logged but don't abort the loop. Goes through
    AlphaService.apply_quality_status_change so demotions are audit-logged.
    """
    from datetime import datetime as _dt

    from sqlalchemy import select

    from backend.agents.graph.nodes.evaluation import _eval_thresholds
    from backend.database import AsyncSessionLocal
    from backend.models import Alpha
    from backend.services.alpha_service import AlphaService
    from backend.services.decay_service import maybe_append_decay_snapshot

    refreshed = 0
    demoted = 0
    failed = 0
    decay_snapshots_added = 0

    async with AsyncSessionLocal() as db:
        # OS-active = stage='OS' AND quality_status in (PASS, PASS_PROVISIONAL).
        # FAIL/PENDING alphas don't need refresh — they're not in the active pool.
        stmt = (
            select(Alpha)
            .where(Alpha.region == region)
            .where(Alpha.stage == "OS")
            .where(Alpha.quality_status.in_(["PASS", "PASS_PROVISIONAL"]))
        )
        rows = (await db.execute(stmt)).scalars().all()
        if not rows:
            return {"metrics_refreshed": 0, "metrics_demoted": 0, "metrics_failed": 0}

        alpha_service = AlphaService(db)
        for alpha in rows:
            if not alpha.alpha_id:
                continue
            try:
                fresh = await brain.get_alpha(alpha.alpha_id)
            except Exception as e:
                logger.warning(f"[refresh_os_metrics] {alpha.alpha_id}: {e}")
                failed += 1
                continue

            if not fresh:
                failed += 1
                continue

            is_block = fresh.get("is") or {}
            old_sharpe = alpha.is_sharpe
            alpha.is_sharpe = is_block.get("sharpe", alpha.is_sharpe)
            alpha.is_fitness = is_block.get("fitness", alpha.is_fitness)
            alpha.is_turnover = is_block.get("turnover", alpha.is_turnover)
            if "checks" in fresh:
                merged = dict(alpha.metrics or {})
                merged["checks"] = fresh["checks"]
                alpha.metrics = merged
            alpha.metrics_snapshot_at = _dt.utcnow()
            refreshed += 1

            # TODO #1: append a weekly decay-curve snapshot. The helper
            # gates on a 6-day dedup window, so calling daily is fine — only
            # the first call each week mutates the row. Failures here are
            # advisory and must not abort the metrics refresh loop.
            try:
                if maybe_append_decay_snapshot(alpha, _dt.utcnow()):
                    decay_snapshots_added += 1
            except Exception as e:
                logger.warning(
                    f"[refresh_os_metrics] decay snapshot append failed for "
                    f"alpha={alpha.id}: {e}"
                )

            # Re-evaluate against the flat thresholds; demote PASS rows
            # whose metrics drifted below the bar so KB stays clean.
            # BRAIN role-switch (P3-Brain): read task-snapshot sharpe override
            # so running tasks don't get re-judged by Consultant 1.58 mid-run.
            _role_snapshot = await read_role_snapshot(alpha.task_id, db)
            t = _eval_thresholds(
                sharpe_submit_min_override=_role_snapshot.get("effective_sharpe_submit_min"),
            )
            sharpe_ok = (alpha.is_sharpe or 0) >= t["sharpe_min"]
            fitness_ok = (alpha.is_fitness or 0) >= t["fitness_min"]
            turnover_ok = (
                t["turnover_min"] <= (alpha.is_turnover or 0) <= t["turnover_max"]
            )

            # User decision (2026-05-02): CONCENTRATED_WEIGHT and
            # LOW_SUB_UNIVERSE_SHARPE FAIL must NOT keep PASS — alpha must
            # enter optimization iteration. evaluation.py:hard_gate already
            # enforces this at creation time, but BRAIN checks often arrive
            # PENDING when a fresh alpha is evaluated, then flip to FAIL on
            # the next BRAIN sync. This block catches the post-sync FAIL
            # and demotes PASS → OPTIMIZE so the alpha re-enters mining
            # candidate pool (mining_agent.py:622 picks up OPTIMIZE rows).
            checks = (alpha.metrics or {}).get("checks") or []
            HARD_DEMOTE_CHECKS = ("CONCENTRATED_WEIGHT", "LOW_SUB_UNIVERSE_SHARPE")
            brain_check_fails = [
                c.get("name")
                for c in checks
                if c.get("result") == "FAIL" and c.get("name") in HARD_DEMOTE_CHECKS
            ]

            if alpha.quality_status in ("PASS", "PASS_PROVISIONAL") and brain_check_fails:
                try:
                    await alpha_service.apply_quality_status_change(
                        alpha_id=alpha.id,
                        new_status="OPTIMIZE",
                        reason=(
                            f"daily_beat_os: BRAIN check FAIL after sync — "
                            f"{','.join(brain_check_fails)}"
                        ),
                        source="daily_beat_os",
                    )
                    demoted += 1
                except Exception as e:
                    logger.warning(
                        f"[refresh_os_metrics] checks-fail demote alpha={alpha.id} failed: {e}"
                    )
            elif alpha.quality_status == "PASS" and not (sharpe_ok and fitness_ok and turnover_ok):
                try:
                    await alpha_service.apply_quality_status_change(
                        alpha_id=alpha.id,
                        new_status="PASS_PROVISIONAL",
                        reason=(
                            f"daily_beat_os: drifted from sharpe={old_sharpe:.2f} → "
                            f"{alpha.is_sharpe:.2f}"
                        ),
                        source="daily_beat_os",
                    )
                    demoted += 1
                except Exception as e:
                    logger.warning(
                        f"[refresh_os_metrics] demote alpha={alpha.id} failed: {e}"
                    )

        await db.commit()

    return {
        "metrics_refreshed": refreshed,
        "metrics_demoted": demoted,
        "metrics_failed": failed,
        "decay_snapshots_added": decay_snapshots_added,
    }


@celery_app.task(name="backend.tasks.sync_datasets")
def sync_datasets(regions: Optional[list] = None, **_extra_kwargs):
    """
    Sync dataset metadata from BRAIN (scheduled or manual).

    Args:
        regions: list of region codes to sync. If None, walks
                 settings.effective_region_universes (User=USA only,
                 Consultant=phase-1 global 5 regions).
        **_extra_kwargs: rolling-upgrade tolerance — old beat schedule may
                         pass no args, new caller may pass regions=[...] AND
                         legacy worker shouldn't TypeError on unknown kwargs.

    BRAIN role-switch (P3-Brain plan §7): each region uses its own universe
    (USA=TOP3000, HKG=TOP500, JPN=TOP1600, etc.) — multi-region sync requires
    per-region universe, not a single DEFAULT_UNIVERSE. Per-region try/except
    so a 4xx on one region doesn't kill the rest.
    """
    logger.info(f"Syncing datasets from BRAIN... regions={regions}")

    async def _run():
        # V-27.3: the beat sync was INSERT-only — it skipped already-existing
        # rows (never refreshing field_count / value_score / pyramid_multiplier
        # / coverage) AND new rows landed with universe=NULL. Since
        # _get_datasets_to_mine / _get_dataset_fields filter on
        # `universe == task.universe`, every beat-synced dataset was invisible
        # to mining — the daily beat was effectively a no-op. Now mirrors the
        # manual sync_datasets_from_brain: per (region, universe), UPDATE
        # existing rows + INSERT new ones with the full field set.
        #
        # P3-Brain plan §7: regions kwarg优先 (FastAPI 进程 resolve 后传给
        # worker,绕开 worker 进程 60s flag cache 滞后);kwarg=None 时从
        # settings.effective_region_universes 读(beat schedule 路径)。
        from backend.config import settings as _settings
        if regions:
            # Caller specified regions explicitly — look up universe per region.
            _region_universes = {
                r: _settings.effective_region_universes.get(r, "TOP3000")
                for r in regions
            }
        else:
            _region_universes = _settings.effective_region_universes
        async with AsyncSessionLocal() as db:
            async with BrainAdapter() as brain:
                new_count = 0
                updated_count = 0
                # V-27.3: collect newly-inserted (dataset, region) pairs so
                # field sync is triggered for them — mirrors the manual
                # sync_datasets_from_brain. Without this a beat-synced new
                # dataset has a field_count number but no DataField rows, so
                # _get_dataset_fields still can't see its fields → mining
                # can't use it ("visible but empty shell").
                field_sync_targets: list = []

                for region, universe in _region_universes.items():
                    try:
                        datasets = await brain.get_datasets(region=region, universe=universe)
                    except Exception as ex:
                        logger.warning(
                            f"[sync_datasets] region={region}/{universe} failed: {ex} — continue"
                        )
                        continue

                    for ds in datasets:
                        result = await db.execute(
                            select(DatasetMetadata).where(
                                DatasetMetadata.dataset_id == ds.get("id"),
                                DatasetMetadata.region == region,
                                DatasetMetadata.universe == universe,
                            )
                        )
                        existing = result.scalar_one_or_none()

                        category = ds.get("category")
                        if isinstance(category, dict):
                            category = category.get("id")
                        subcategory = ds.get("subcategory")
                        if isinstance(subcategory, dict):
                            subcategory = subcategory.get("id")

                        if existing:
                            existing.description = ds.get("description")
                            existing.category = category
                            existing.subcategory = subcategory
                            existing.field_count = ds.get("fieldCount", 0)
                            existing.last_synced_at = func.now()
                            existing.date_coverage = ds.get("dateCoverage")
                            existing.themes = ds.get("themes")
                            existing.resources = ds.get("researchPapers")
                            existing.value_score = ds.get("valueScore")
                            existing.alpha_count = ds.get("alphaCount")
                            existing.pyramid_multiplier = ds.get("pyramidMultiplier")
                            existing.coverage = ds.get("coverage")
                            updated_count += 1
                        else:
                            db.add(DatasetMetadata(
                                dataset_id=ds.get("id"),
                                region=region,
                                universe=universe,
                                category=category,
                                subcategory=subcategory,
                                description=ds.get("description"),
                                field_count=ds.get("fieldCount", 0),
                                date_coverage=ds.get("dateCoverage"),
                                themes=ds.get("themes"),
                                resources=ds.get("researchPapers"),
                                value_score=ds.get("valueScore"),
                                alpha_count=ds.get("alphaCount"),
                                pyramid_multiplier=ds.get("pyramidMultiplier"),
                                coverage=ds.get("coverage"),
                            ))
                            new_count += 1
                            # P3-Brain: capture per-(dataset, region, universe) tuple
                            # — multi-region sync uses different universes (HKG=TOP500
                            # etc.), can't rely on closure `universe` (would be the
                            # last region's universe by enqueue time).
                            field_sync_targets.append((ds.get("id"), region, universe))

                await db.commit()

                # V-27.3: trigger field sync for newly-inserted datasets only
                # (existing ones already have DataField rows from a prior
                # sync). Bounded by new_count so the beat doesn't fan out a
                # field-sync task per dataset every day.
                for _dsid, _reg, _uni in field_sync_targets:
                    sync_fields_from_brain.delay(
                        dataset_id=_dsid,
                        region=_reg,
                        universe=_uni,
                        delay=1,
                    )

                logger.info(
                    f"Synced datasets ({len(_region_universes)} regions): "
                    f"{new_count} new, {updated_count} updated; "
                    f"{len(field_sync_targets)} field syncs queued"
                )
                return {
                    "new_datasets": new_count,
                    "updated_datasets": updated_count,
                    "field_syncs_queued": len(field_sync_targets),
                }

    return run_async(_run())


@celery_app.task(name="backend.tasks.sync_datasets_from_brain")
def sync_datasets_from_brain(region: str = "USA", universe: str = "TOP3000"):
    """
    Sync datasets for a specific region (Manual Trigger).
    
    Args:
        region: Market region
        universe: Stock universe
    """
    logger.info(f"Syncing datasets for region={region} universe={universe}...")
    
    async def _run():
        async with AsyncSessionLocal() as db:
            async with BrainAdapter() as brain:
                datasets = await brain.get_datasets(region=region, universe=universe)
                count = 0
                updated = 0
                
                for ds in datasets:
                    stmt = select(DatasetMetadata).where(
                        DatasetMetadata.dataset_id == ds.get("id"),
                        DatasetMetadata.region == region,
                        DatasetMetadata.universe == universe
                    )
                    result = await db.execute(stmt)
                    existing = result.scalar_one_or_none()
                    
                    category = ds.get("category")
                    if isinstance(category, dict):
                        category = category.get("id")
                        
                    subcategory = ds.get("subcategory")
                    if isinstance(subcategory, dict):
                        subcategory = subcategory.get("id")
                        
                    if existing:
                        existing.description = ds.get("description")
                        existing.category = category
                        existing.subcategory = subcategory
                        existing.field_count = ds.get("fieldCount", 0)
                        existing.last_synced_at = func.now()
                        existing.date_coverage = ds.get("dateCoverage")
                        existing.themes = ds.get("themes")
                        existing.resources = ds.get("researchPapers")
                        existing.value_score = ds.get("valueScore")
                        existing.alpha_count = ds.get("alphaCount")
                        existing.pyramid_multiplier = ds.get("pyramidMultiplier")
                        existing.coverage = ds.get("coverage")
                        updated += 1
                    else:
                        metadata = DatasetMetadata(
                            dataset_id=ds.get("id"),
                            region=region,
                            universe=universe,
                            category=category,
                            subcategory=subcategory,
                            description=ds.get("description"),
                            field_count=ds.get("fieldCount", 0),
                            date_coverage=ds.get("dateCoverage"),
                            themes=ds.get("themes"),
                            resources=ds.get("researchPapers"),
                            value_score=ds.get("valueScore"),
                            alpha_count=ds.get("alphaCount"),
                            pyramid_multiplier=ds.get("pyramidMultiplier"),
                            coverage=ds.get("coverage")
                        )
                        db.add(metadata)
                        count += 1
                
                await db.commit()
                
                # Auto-trigger field sync
                logger.info(f"Auto-triggering field sync for {len(datasets)} datasets...")
                for ds in datasets:
                    sync_fields_from_brain.delay(
                        dataset_id=ds.get("id"),
                        region=region,
                        universe=universe,
                        delay=1
                    )
                
                logger.info(f"Sync complete: {count} new, {updated} updated. Field syncs queued.")
                return {"new": count, "updated": updated, "field_syncs_queued": len(datasets)}
    
    return run_async(_run())


@celery_app.task(name="backend.tasks.sync_operators_from_brain")
def sync_operators_from_brain():
    """Sync operators from BRAIN platform."""
    logger.info("Syncing operators from BRAIN...")
    
    async def _run():
        async with AsyncSessionLocal() as db:
            async with BrainAdapter() as brain:
                ops_data = await brain.get_operators(detailed=True)
                
                if ops_data and isinstance(ops_data[0], str):
                    logger.warning("Operator sync got simple list, skipping detailed update")
                    return {"updated": 0}
                
                count = 0
                updated = 0
                
                for op_data in ops_data:
                    name = op_data.get("name")
                    if not name:
                        continue
                        
                    stmt = select(Operator).where(Operator.name == name)
                    result = await db.execute(stmt)
                    existing = result.scalar_one_or_none()
                    
                    if existing:
                        existing.description = op_data.get("description")
                        existing.category = op_data.get("category")
                        existing.definition = op_data.get("definition")
                        existing.level = op_data.get("level")
                        existing.scope = op_data.get("scope")
                        existing.documentation = op_data.get("documentation")
                        updated += 1
                    else:
                        new_op = Operator(
                            name=name,
                            description=op_data.get("description"),
                            category=op_data.get("category"),
                            definition=op_data.get("definition"),
                            level=op_data.get("level"),
                            scope=op_data.get("scope"),
                            documentation=op_data.get("documentation"),
                        )
                        db.add(new_op)
                        count += 1
                
                await db.commit()
                logger.info(f"Operator sync complete: {count} new, {updated} updated")
                return {"new": count, "updated": updated}
    
    return run_async(_run())


@celery_app.task(name="backend.tasks.sync_fields_from_brain")
def sync_fields_from_brain(dataset_id: str, region: str = "USA", universe: str = "TOP3000", delay: int = 1):
    """
    Sync fields for a specific dataset from BRAIN.
    
    Args:
        dataset_id: The dataset ID
        region: Market region
        universe: Stock universe
        delay: Signal delay
    """
    logger.info(f"Syncing fields for {dataset_id}...")
    
    async def _run():
        async with AsyncSessionLocal() as db:
            # Resolve dataset PK
            stmt_ds = select(DatasetMetadata).where(
                DatasetMetadata.dataset_id == dataset_id,
                DatasetMetadata.region == region,
                DatasetMetadata.universe == universe
            )
            res_ds = await db.execute(stmt_ds)
            dataset = res_ds.scalar_one_or_none()
            
            if not dataset:
                logger.error(f"Dataset {dataset_id} not found for region {region}")
                return {"error": "Dataset not found"}
            
            async with BrainAdapter() as brain:
                fields = await brain.get_datafields(
                    dataset_id=dataset_id,
                    region=region,
                    universe=universe,
                    delay=delay
                )
                
                count = 0
                updated = 0
                
                for f_data in fields:
                    fid = f_data.get("id")
                    if not fid:
                        continue
                    
                    # Extract nested category/subcategory objects
                    category_obj = f_data.get("category") or {}
                    subcategory_obj = f_data.get("subcategory") or {}
                    
                    category_id = category_obj.get("id") if isinstance(category_obj, dict) else category_obj
                    category_name = category_obj.get("name") if isinstance(category_obj, dict) else None
                    subcategory_id = subcategory_obj.get("id") if isinstance(subcategory_obj, dict) else subcategory_obj
                    subcategory_name = subcategory_obj.get("name") if isinstance(subcategory_obj, dict) else None
                        
                    stmt = select(DataField).where(
                        DataField.dataset_id == dataset.id,
                        DataField.field_id == fid
                    )
                    result = await db.execute(stmt)
                    existing = result.scalar_one_or_none()
                    
                    if existing:
                        existing.description = f_data.get("description")
                        existing.field_name = f_data.get("name", fid)
                        existing.field_type = f_data.get("type")
                        existing.date_coverage = f_data.get("dateCoverage")
                        existing.coverage = f_data.get("coverage")
                        existing.pyramid_multiplier = f_data.get("pyramidMultiplier")
                        existing.user_count = f_data.get("userCount")
                        existing.alpha_count = f_data.get("alphaCount", 0)
                        existing.category = category_id
                        existing.category_name = category_name
                        existing.subcategory = subcategory_id
                        existing.subcategory_name = subcategory_name
                        existing.themes = f_data.get("themes", [])
                        updated += 1
                    else:
                        new_field = DataField(
                            dataset_id=dataset.id,
                            region=region,
                            universe=universe,
                            delay=delay,
                            field_id=fid,
                            field_name=f_data.get("name", fid),
                            description=f_data.get("description"),
                            field_type=f_data.get("type"),
                            date_coverage=f_data.get("dateCoverage"),
                            coverage=f_data.get("coverage"),
                            pyramid_multiplier=f_data.get("pyramidMultiplier"),
                            user_count=f_data.get("userCount"),
                            alpha_count=f_data.get("alphaCount", 0),
                            category=category_id,
                            category_name=category_name,
                            subcategory=subcategory_id,
                            subcategory_name=subcategory_name,
                            themes=f_data.get("themes", [])
                        )
                        db.add(new_field)
                        count += 1
                
                # Update dataset field count
                from sqlalchemy import func as sqla_func
                total_fields_query = await db.execute(
                    select(sqla_func.count(DataField.id)).where(DataField.dataset_id == dataset.id)
                )
                actual_field_count = total_fields_query.scalar() or 0
                dataset.field_count = actual_field_count
                dataset.last_synced_at = func.now()
                
                await db.commit()
                logger.info(f"Field sync for {dataset_id}: {count} new, {updated} updated")
                return {"new": count, "updated": updated}
    
    return run_async(_run())


@celery_app.task(name="backend.tasks.sync_user_alphas")
def sync_user_alphas():
    """Sync all user alphas (IS and OS) from Brain."""
    logger.info("Syncing user alphas from Brain...")
    
    async def _run():
        # P2.C [V1.2-R1] (2026-05-20): skip if BRAIN auth circuit is open.
        # The new 6h beat schedule fires regardless of BRAIN health; without
        # this guard an auth outage makes every tick burn 5-10 sequential
        # authenticate() retries (only simulate_alpha consulted the circuit
        # before). Mirrors brain_adapter.simulate_alpha's fast-fail.
        from backend.adapters.brain_adapter import BRAIN_AUTH_CIRCUIT
        if BRAIN_AUTH_CIRCUIT.is_open():
            logger.warning(
                f"[sync_user_alphas] BRAIN_AUTH_CIRCUIT open "
                f"({BRAIN_AUTH_CIRCUIT.status()}); skipping this tick"
            )
            return {"status": "skipped_circuit_open"}

        async with AsyncSessionLocal() as db:
            async with BrainAdapter() as brain:
                count = 0
                updated = 0

                stages = ["IS", "OS"]
                
                # Check for latest created timestamp (Incremental Sync)
                stmt_latest = select(func.max(Alpha.date_created))
                result_latest = await db.execute(stmt_latest)
                latest_date = result_latest.scalar_one_or_none()
                
                MIN_START_DATE = datetime(2025, 7, 5)
                start_date_iso = None

                if latest_date:
                    safe_start = latest_date - timedelta(days=3)
                    if safe_start < MIN_START_DATE:
                        safe_start = MIN_START_DATE
                    start_date_iso = safe_start.strftime("%Y-%m-%d")
                    logger.info(f"Incremental Sync: Fetching alphas since {start_date_iso}")
                else:
                    start_date_iso = MIN_START_DATE.strftime("%Y-%m-%d")
                    logger.info(f"Full Sync: Fetching all alphas since {start_date_iso}")

                # V-23.E (2026-05-13): track regions where submissions were
                # detected this run. After commit, mark all remaining
                # unsubmitted can_submit alphas in those regions as stale —
                # their _iqc_marginal Δscore was computed against an older
                # portfolio state and is no longer accurate.
                submission_flip_regions: set = set()

                for stage in stages:
                    # V-27.14: the incremental anchor (start_date_iso) is on
                    # date_created, but V-23.E needs to catch date_submitted
                    # flips (NULL→set). An alpha created 30 days ago but
                    # submitted yesterday falls outside the created-window →
                    # BRAIN drops it from the listing → submission_flip_regions
                    # misses it → IQC Δscore staleness never fires. OS-stage
                    # alphas are exactly the ones that flip (submit moves an
                    # alpha to OS) and there are only tens-to-hundreds of them
                    # — pull the OS stage in FULL while IS (the high-volume
                    # mining output) stays incremental.
                    effective_start = (
                        start_date_iso if stage == "IS"
                        else MIN_START_DATE.strftime("%Y-%m-%d")
                    )
                    offset = 0
                    limit = 100
                    while True:
                        alphas_data = await brain.get_user_alphas(
                            limit=limit,
                            offset=offset,
                            stage=stage,
                            start_date=effective_start
                        )
                        results = alphas_data.get("results", [])
                        if not results:
                            break

                        logger.info(f"Syncing {len(results)} alphas from {stage} (offset {offset})...")

                        for a_data in results:
                            alpha_id = a_data.get("id")
                            if not alpha_id:
                                continue

                            stmt = select(Alpha).where(Alpha.alpha_id == alpha_id)
                            result = await db.execute(stmt)
                            existing = result.scalar_one_or_none()

                            # Parse dates
                            date_created = _parse_to_beijing(a_data.get("dateCreated"))
                            date_submitted = _parse_to_beijing(a_data.get("dateSubmitted"))

                            settings = a_data.get("settings", {})
                            is_metrics = a_data.get("is", {})
                            os_metrics = a_data.get("os", {}) or {}

                            if existing:
                                # V-23.E: detect submission flip BEFORE update
                                # mutates existing.date_submitted
                                if (
                                    existing.date_submitted is None
                                    and date_submitted is not None
                                ):
                                    submission_flip_regions.add(existing.region)
                                _update_existing_alpha(existing, a_data, stage, settings, is_metrics, os_metrics, date_submitted)
                                updated += 1
                            else:
                                # V-23.E: if BRAIN reports a newly-created
                                # alpha already submitted (rare but possible
                                # for off-platform tools), still treat as
                                # portfolio-state change for that region.
                                if date_submitted is not None:
                                    submission_flip_regions.add(settings.get("region"))
                                new_alpha = _create_new_alpha(a_data, stage, settings, is_metrics, os_metrics, date_created, date_submitted)
                                db.add(new_alpha)
                                count += 1

                        await db.commit()
                        logger.info(f"Committed {len(results)} updates/inserts.")

                        offset += limit
                        if offset >= alphas_data.get("count", 0):
                            break

                # V-23.E post-sync stale marking. JSONB jsonb_set semantics:
                # when _iqc_marginal already exists, set 'stale'=true; when
                # it doesn't exist, skip (no past audit to invalidate).
                stale_marked = 0
                for region in submission_flip_regions:
                    if not region:
                        continue
                    res = await db.execute(
                        text(
                            """
                            UPDATE alphas
                            SET metrics = jsonb_set(
                                metrics, '{_iqc_marginal,stale}', 'true'::jsonb
                            )
                            WHERE can_submit = true
                              AND date_submitted IS NULL
                              AND region = :region
                              AND metrics ? '_iqc_marginal'
                              AND COALESCE(
                                metrics->'_iqc_marginal'->>'stale', 'false'
                              ) != 'true'
                            """
                        ),
                        {"region": region},
                    )
                    stale_marked += res.rowcount or 0
                if submission_flip_regions:
                    await db.commit()
                    logger.info(
                        f"[V-23.E] stale-marked {stale_marked} unsubmitted "
                        f"can_submit alphas across regions "
                        f"{submission_flip_regions} (submission flip detected)"
                    )

                logger.info(f"Alpha sync complete: {count} new, {updated} updated")
                return {"new": count, "updated": updated, "iqc_stale_marked": stale_marked}

    return run_async(_run())


def _parse_to_beijing(iso_str):
    """Parse ISO date string to Beijing time."""
    if not iso_str:
        return None
    try:
        BEIJING_TZ = timezone(timedelta(hours=8))
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        dt_bj = dt.astimezone(BEIJING_TZ)
        return dt_bj.replace(tzinfo=None)
    except:
        return None


def _derive_quality_status_from_metrics(sharpe, fitness, turnover):
    """P2.C [V1.1-S1/S2] (2026-05-20): derive a quality_status from BRAIN
    metrics so sync-imported alphas land classified instead of dumped as
    PENDING (which loses the gate signal already present in the metrics).

    Mirrors the EVAL_* / EVAL_PROVISIONAL_* bands used by the runtime
    evaluator (_eval_thresholds). Conservative: never returns full PASS
    (composite_score isn't recomputed here) — only PASS_PROVISIONAL or FAIL.
    Missing metrics → PENDING (can't evaluate). Provisional band has no
    turnover lower bound (matches config: only EVAL_PROVISIONAL_TURNOVER_MAX
    exists, no _MIN).
    """
    from backend.config import settings as _cfg
    if sharpe is None or fitness is None or turnover is None:
        return "PENDING"
    if (sharpe >= _cfg.EVAL_SHARPE_MIN
            and fitness >= _cfg.EVAL_FITNESS_MIN
            and _cfg.EVAL_TURNOVER_MIN <= turnover <= _cfg.EVAL_TURNOVER_MAX):
        return "PASS_PROVISIONAL"
    if (sharpe >= _cfg.EVAL_PROVISIONAL_SHARPE_MIN
            and fitness >= _cfg.EVAL_PROVISIONAL_FITNESS_MIN
            and turnover <= _cfg.EVAL_PROVISIONAL_TURNOVER_MAX):
        return "PASS_PROVISIONAL"
    return "FAIL"


def _update_existing_alpha(existing, a_data, stage, settings, is_metrics, os_metrics, date_submitted):
    """Update an existing alpha with new data.

    Auto-fills BRAIN-derived can_submit + metrics_snapshot_at so /alphas/sync
    produces ready-to-use rows without an extra backfill step.
    """
    from backend.can_submit import compute_can_submit

    existing.status = a_data.get("status")
    existing.stage = stage
    existing.settings = settings
    existing.tags = a_data.get("tags")
    existing.checks = a_data.get("is", {}).get("checks", [])

    existing.is_metrics = is_metrics
    existing.os_metrics = os_metrics

    existing.is_sharpe = is_metrics.get("sharpe")
    existing.is_fitness = is_metrics.get("fitness")
    existing.is_returns = is_metrics.get("returns")
    existing.is_turnover = is_metrics.get("turnover")
    existing.is_drawdown = is_metrics.get("drawdown")

    existing.date_modified = datetime.now()
    if date_submitted:
        existing.date_submitted = date_submitted

    existing.dataset_id = settings.get("datasetId")

    can_sub, failed, pending = compute_can_submit(a_data)
    if can_sub is not None:
        existing.can_submit = can_sub

    existing.metrics_snapshot_at = datetime.now(timezone.utc)
    # P2.C [V1.1-M3] (2026-05-20): MERGE, don't REPLACE. The old code
    # `existing.metrics = {**is_metrics, ...}` wiped every AIAC-stamped
    # `_`-prefixed key on each 6h sync — _direction_bandit_recommended_arm,
    # _g8_forest_referenced_ids, _pre_brain_skip, _reslot_thresholds, etc.
    # Layer existing first so AIAC keys survive; BRAIN-fresh metrics + the
    # _brain_* keys override on top.
    existing.metrics = {
        **(existing.metrics or {}),
        **(is_metrics or {}),
        "_brain_can_submit": can_sub,
        "_brain_failed_checks": failed,
        "_brain_pending_checks": pending,
    }

    # P2.C [V1.1-S2] (2026-05-20): derive quality_status from metrics, but
    # ONLY when the row is still PENDING — never overwrite a mining-direct
    # PASS/PASS_PROVISIONAL/OPTIMIZE/FAIL verdict (anti-pattern guard: sync
    # is reconciliation, the mining write is authoritative).
    if existing.quality_status == "PENDING":
        _derived = _derive_quality_status_from_metrics(
            is_metrics.get("sharpe"), is_metrics.get("fitness"), is_metrics.get("turnover")
        )
        if _derived != "PENDING":
            existing.quality_status = _derived

    existing.is_margin = is_metrics.get("margin")
    existing.is_long_count = is_metrics.get("longCount")
    existing.is_short_count = is_metrics.get("shortCount")


def _create_new_alpha(a_data, stage, settings, is_metrics, os_metrics, date_created, date_submitted):
    """Create a new alpha from BRAIN data.

    Auto-fills BRAIN-derived can_submit + metrics_snapshot_at so /alphas/sync
    produces ready-to-use rows without an extra backfill step.
    """
    from backend.alpha_semantic_validator import compute_expression_hash
    from backend.can_submit import compute_can_submit

    expr_code = (
        a_data.get("regular", {}).get("code") or
        a_data.get("combo", {}).get("code") or
        a_data.get("selection", {}).get("code") or
        "N/A"
    )
    expr_hash = compute_expression_hash(expr_code) if expr_code != "N/A" else None

    can_sub, failed, pending = compute_can_submit(a_data)

    return Alpha(
        alpha_id=a_data.get("id"),
        type=a_data.get("type"),
        expression=expr_code,
        expression_hash=expr_hash,
        name=a_data.get("name"),
        region=settings.get("region"),
        universe=settings.get("universe"),
        dataset_id=settings.get("datasetId"),
        status=a_data.get("status"),
        stage=stage,
        settings=settings,
        tags=a_data.get("tags"),
        checks=a_data.get("is", {}).get("checks", []),
        is_metrics=is_metrics,
        os_metrics=os_metrics,
        is_sharpe=is_metrics.get("sharpe"),
        is_fitness=is_metrics.get("fitness"),
        is_returns=is_metrics.get("returns"),
        is_turnover=is_metrics.get("turnover"),
        is_drawdown=is_metrics.get("drawdown"),
        is_margin=is_metrics.get("margin"),
        is_long_count=is_metrics.get("longCount"),
        is_short_count=is_metrics.get("shortCount"),
        date_created=date_created,
        date_submitted=date_submitted,
        can_submit=can_sub,
        # P2.C [V1.1-S1] (2026-05-20): classify on import instead of dumping
        # as PENDING — the BRAIN metrics already carry the gate signal.
        quality_status=_derive_quality_status_from_metrics(
            is_metrics.get("sharpe"), is_metrics.get("fitness"), is_metrics.get("turnover")
        ),
        metrics_snapshot_at=datetime.now(timezone.utc),
        metrics={
            **(is_metrics or {}),
            "_brain_can_submit": can_sub,
            "_brain_failed_checks": failed,
            "_brain_pending_checks": pending,
        },
    )
