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
from backend.models import (
    DatasetMetadata, DatasetCellStats, DataField, DataFieldCellStats, Operator, Alpha,
)
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


async def _upsert_dataset_def_and_cell(db, ds, *, region, universe, delay, category, subcategory):
    """Cell-stats normalization upsert: a dataset DEFINITION (by dataset_id,
    region) + its per-(universe, delay) ``DatasetCellStats`` cell.

    Returns ``(def_created, cell_created)``. mining_weight / alpha_{success,fail}_count
    are left at their column defaults (the dataset bandit owns mining_weight; sync
    never wrote those) — mirrors the pre-refactor sync which never touched them.
    Caller commits.
    """
    dsid = ds.get("id")
    ddef = (await db.execute(
        select(DatasetMetadata).where(
            DatasetMetadata.dataset_id == dsid,
            DatasetMetadata.region == region,
        )
    )).scalar_one_or_none()
    def_created = False
    if ddef is None:
        ddef = DatasetMetadata(
            dataset_id=dsid, region=region,
            description=ds.get("description"), category=category, subcategory=subcategory,
        )
        db.add(ddef)
        await db.flush()  # assign datasets.id for the cell FK
        def_created = True
    else:
        ddef.description = ds.get("description")
        ddef.category = category
        ddef.subcategory = subcategory

    cell = (await db.execute(
        select(DatasetCellStats).where(
            DatasetCellStats.dataset_ref == ddef.id,
            DatasetCellStats.universe == universe,
            DatasetCellStats.delay == delay,
        )
    )).scalar_one_or_none()
    cell_created = False
    if cell is None:
        cell = DatasetCellStats(dataset_ref=ddef.id, universe=universe, delay=delay)
        db.add(cell)
        cell_created = True
    cell.field_count = ds.get("fieldCount", 0)
    cell.last_synced_at = func.now()
    cell.date_coverage = ds.get("dateCoverage")
    cell.themes = ds.get("themes")
    cell.resources = ds.get("researchPapers")
    cell.value_score = ds.get("valueScore")
    cell.alpha_count = ds.get("alphaCount")
    cell.pyramid_multiplier = ds.get("pyramidMultiplier")
    cell.coverage = ds.get("coverage")
    return def_created, cell_created


async def _upsert_datafield_cell(db, datafield_ref, *, universe, delay, f_data):
    """Upsert the per-(universe, delay) cell stats for a datafield def. BRAIN
    returned the field → is_active=True (undoes any prior prune). region is via
    the parent dataset (not stored on the cell). Caller commits."""
    cell = (await db.execute(
        select(DataFieldCellStats).where(
            DataFieldCellStats.datafield_ref == datafield_ref,
            DataFieldCellStats.universe == universe,
            DataFieldCellStats.delay == delay,
        )
    )).scalar_one_or_none()
    if cell is None:
        cell = DataFieldCellStats(datafield_ref=datafield_ref, universe=universe, delay=delay)
        db.add(cell)
    cell.date_coverage = f_data.get("dateCoverage")
    cell.coverage = f_data.get("coverage")
    cell.pyramid_multiplier = f_data.get("pyramidMultiplier")
    cell.user_count = f_data.get("userCount")
    cell.alpha_count = f_data.get("alphaCount", 0)
    cell.themes = f_data.get("themes", [])
    cell.is_active = True


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
                        category = ds.get("category")
                        if isinstance(category, dict):
                            category = category.get("id")
                        subcategory = ds.get("subcategory")
                        if isinstance(subcategory, dict):
                            subcategory = subcategory.get("id")

                        def_created, cell_created = await _upsert_dataset_def_and_cell(
                            db, ds, region=region, universe=universe, delay=1,
                            category=category, subcategory=subcategory,
                        )
                        if def_created:
                            new_count += 1
                        else:
                            updated_count += 1
                        # Trigger field sync whenever the (universe) cell is new —
                        # covers both a brand-new dataset and a new universe of an
                        # existing one (its datafield cells don't exist yet).
                        # P3-Brain: capture the (dataset, region, universe) tuple —
                        # multi-region sync uses different universes (HKG=TOP500 etc.),
                        # can't rely on closure `universe` (= last region's by enqueue).
                        if cell_created:
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
                    category = ds.get("category")
                    if isinstance(category, dict):
                        category = category.get("id")

                    subcategory = ds.get("subcategory")
                    if isinstance(subcategory, dict):
                        subcategory = subcategory.get("id")

                    def_created, _cell_created = await _upsert_dataset_def_and_cell(
                        db, ds, region=region, universe=universe, delay=1,
                        category=category, subcategory=subcategory,
                    )
                    if def_created:
                        count += 1
                    else:
                        updated += 1

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


async def _reconcile_dataset_fields(db, dataset, fields, *, region, universe, delay):
    """Upsert BRAIN-returned fields + (re-)activate them (user 2026-05-22):
    a field BRAIN currently returns → is_active=True, undoing any earlier prune.

    Fields BRAIN no longer returns KEEP their current is_active — deactivation
    is delegated to the mining-driven prune (prune_invalid_datafields), which
    deactivates ONLY fields BRAIN actually rejects at SIMULATE time ("Invalid
    data field"). So sync never wipes a dataset on a transient/sliced metadata
    response; the loop self-identifies dead fields as they fail. Caller commits.
    Pure DB-side → unit-testable on the sqlite fixture.

    Cell-stats normalization (2026-05-26): a field DEFINITION (datafields, keyed
    by dataset_id+field_id) is upserted once + its per-(universe, delay) cell in
    datafield_cell_stats (coverage/counts/themes/is_active). field_count lands on
    the parent dataset's (universe, delay) cell, counting ACTIVE field cells.

    Returns {"new", "updated", "returned"}.
    """
    from sqlalchemy import func as sqla_func

    count = 0
    updated = 0
    for f_data in fields:
        fid = f_data.get("id")
        if not fid:
            continue
        category_obj = f_data.get("category") or {}
        subcategory_obj = f_data.get("subcategory") or {}
        category_id = category_obj.get("id") if isinstance(category_obj, dict) else category_obj
        category_name = category_obj.get("name") if isinstance(category_obj, dict) else None
        subcategory_id = subcategory_obj.get("id") if isinstance(subcategory_obj, dict) else subcategory_obj
        subcategory_name = subcategory_obj.get("name") if isinstance(subcategory_obj, dict) else None

        existing = (await db.execute(
            select(DataField).where(
                DataField.dataset_id == dataset.id,
                DataField.field_id == fid,
            )
        )).scalar_one_or_none()

        if existing:
            existing.description = f_data.get("description")
            existing.field_name = f_data.get("name", fid)
            existing.field_type = f_data.get("type")
            existing.category = category_id
            existing.category_name = category_name
            existing.subcategory = subcategory_id
            existing.subcategory_name = subcategory_name
            df_ref = existing.id
            updated += 1
        else:
            df = DataField(
                dataset_id=dataset.id,
                field_id=fid, field_name=f_data.get("name", fid),
                description=f_data.get("description"), field_type=f_data.get("type"),
                category=category_id, category_name=category_name,
                subcategory=subcategory_id, subcategory_name=subcategory_name,
            )
            db.add(df)
            await db.flush()  # assign datafields.id for the cell FK
            df_ref = df.id
            count += 1

        await _upsert_datafield_cell(db, df_ref, universe=universe, delay=delay, f_data=f_data)

    returned_ids = {f.get("id") for f in fields if f.get("id")}
    # Fields BRAIN no longer returns are intentionally left as-is — the mining
    # prune deactivates the truly-invalid ones when BRAIN rejects them at sim.
    await db.flush()  # ensure new cells are visible to the count below

    # field_count reflects ACTIVE field cells for this (universe, delay) — what
    # mining actually sees — and lands on the parent dataset's cell.
    active_count = (await db.execute(
        select(sqla_func.count(DataFieldCellStats.id))
        .join(DataField, DataField.id == DataFieldCellStats.datafield_ref)
        .where(
            DataField.dataset_id == dataset.id,
            DataFieldCellStats.universe == universe,
            DataFieldCellStats.delay == delay,
            DataFieldCellStats.is_active.is_(True),
        )
    )).scalar() or 0

    ds_cell = (await db.execute(
        select(DatasetCellStats).where(
            DatasetCellStats.dataset_ref == dataset.id,
            DatasetCellStats.universe == universe,
            DatasetCellStats.delay == delay,
        )
    )).scalar_one_or_none()
    if ds_cell is None:
        ds_cell = DatasetCellStats(dataset_ref=dataset.id, universe=universe, delay=delay)
        db.add(ds_cell)
    ds_cell.field_count = active_count
    ds_cell.last_synced_at = func.now()
    return {"new": count, "updated": updated, "returned": len(returned_ids)}


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
            # Resolve dataset def (universe/delay-invariant → keyed by dataset_id+region)
            stmt_ds = select(DatasetMetadata).where(
                DatasetMetadata.dataset_id == dataset_id,
                DatasetMetadata.region == region,
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
                
                stats = await _reconcile_dataset_fields(
                    db, dataset, fields,
                    region=region, universe=universe, delay=delay,
                )
                await db.commit()
                logger.info(
                    f"Field sync for {dataset_id}: {stats['new']} new, "
                    f"{stats['updated']} updated/(re)activated "
                    f"(BRAIN returned {stats['returned']})"
                )
                return {k: stats[k] for k in ("new", "updated")}
    
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

                # 2026-05-20: BRAIN's 'dateCreated>' filter requires ISO-8601
                # WITH a timezone. date_created is stored Beijing-naive
                # (_parse_to_beijing: BRAIN-UTC + 8h, tz stripped — see
                # [[reference_alpha_dual_timezone]]), so we re-attach +08:00 to
                # represent the correct instant. Prev code passed a naive
                # 'YYYY-MM-DD' to the (silently-ignored) 'startDate' param, so
                # every "incremental" sync was actually a full re-fetch +
                # update of all ~9700 alphas.
                def _iso_bj(dt):
                    return dt.strftime("%Y-%m-%dT%H:%M:%S+08:00")

                if latest_date:
                    safe_start = latest_date - timedelta(days=3)
                    if safe_start < MIN_START_DATE:
                        safe_start = MIN_START_DATE
                    start_date_iso = _iso_bj(safe_start)
                    logger.info(f"Incremental Sync: Fetching alphas since {start_date_iso}")
                else:
                    start_date_iso = _iso_bj(MIN_START_DATE)
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
                        else _iso_bj(MIN_START_DATE)
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

                        # 2026-05-24: collect processed alphas → incremental PnL
                        # backfill enqueued after the batch commit (ids assigned).
                        _pnl_targets = []
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
                                _pnl_targets.append(existing)
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
                                _pnl_targets.append(new_alpha)

                        await db.commit()
                        logger.info(f"Committed {len(results)} updates/inserts.")

                        # 2026-05-24: incremental PnL backfill — enqueue store for
                        # processed alphas (the task skips any that already have
                        # PnL, so sync does NOT re-fetch the whole pool each cycle).
                        try:
                            from backend.tasks.refresh_tasks import enqueue_alpha_pnl_store
                            for _a in _pnl_targets:
                                if getattr(_a, "id", None) and getattr(_a, "alpha_id", None):
                                    enqueue_alpha_pnl_store(_a.id, _a.alpha_id)
                        except Exception as _pnl_e:
                            logger.warning(f"[sync] PnL enqueue batch failed: {_pnl_e}")

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


def _derive_verdict_from_brain(a_data, is_metrics, os_metrics, expression, can_sub):
    """Feature 1 (2026-05-24): derive a synced alpha's quality_status through the
    SAME verdict logic the runtime evaluator uses (compute_verdict_from_signals),
    replacing the old sharpe/fitness/turnover-only band function.

    Returns a VerdictResult, or None when a core metric is missing (caller keeps
    PENDING). score=0 / should_opt=False → sync never emits OPTIMIZE. BRAIN-gate
    signals come from evaluate_with_brain_checks (mirrors mining); self_corr is
    read from BRAIN's SELF_CORRELATION check (no PnL fetch). A sync-side guardrail
    demotes a full PASS to PASS_PROVISIONAL when compute_can_submit reports the
    alpha unsubmittable, so sync's PASS never exceeds the BRAIN submission gate.

    See plan synchronous-rolling-lagoon.md (v4). Lazy cross-layer imports match
    the established pattern (sync_tasks already lazy-imports _eval_thresholds);
    evaluation.py has no top-level backend.tasks import so there is no cycle.
    """
    from backend.agents.graph.nodes.evaluation import (
        compute_verdict_from_signals,
        _unpack_eval_thresholds,
        _eval_thresholds,
        _safe_metric,
    )
    from backend.alpha_scoring import evaluate_with_brain_checks
    from backend.services.correlation_service import CorrSource

    # B#5: raw-None guard FIRST. A missing core metric → PENDING (unevaluable),
    # exactly as the old function did. _safe_metric coerces None→0.0, which would
    # mis-route these to FAIL, so the raw check must precede normalization.
    if (is_metrics.get("sharpe") is None
            or is_metrics.get("fitness") is None
            or is_metrics.get("turnover") is None):
        return None

    # T5: a single checks reference feeds the verdict (sub_universe/concentrated),
    # evaluate_with_brain_checks, AND compute_can_submit — all read a_data's
    # is.checks. is_metrics IS a_data["is"], so is_metrics["checks"] is that array.
    _checks = is_metrics.get("checks", [])

    # B#3: fresh dict carrying is_metrics' checks + the OS leg for V-12. Does NOT
    # mutate is_metrics (persisted as-is).
    verdict_metrics = {**is_metrics, "os_sharpe": os_metrics.get("sharpe")}

    # T6: one throwaway fallback list; sync does NOT persist _metrics_fallback_flags.
    _fb = []
    sharpe = _safe_metric(verdict_metrics, "sharpe", 0.0, _fb)
    fitness = _safe_metric(verdict_metrics, "fitness", 0.0, _fb)
    turnover = _safe_metric(verdict_metrics, "turnover", 0.0, _fb)

    # Minimal sim_result mirror (evaluation.py:413-434) for evaluate_with_brain_checks.
    # T5: checks at is.checks + top level are the same reference as verdict_metrics'.
    sim_result = {
        "is": {"checks": _checks},
        "checks": _checks,
        "can_submit": is_metrics.get("can_submit", False),
    }
    brain_eval = evaluate_with_brain_checks(sim_result)
    brain_can_submit = brain_eval.get("can_submit", False)
    brain_failed_checks = brain_eval.get("failed_checks", [])
    brain_check_details_present = bool(brain_eval.get("check_details"))

    th = _unpack_eval_thresholds(_eval_thresholds())

    # meets_thresholds — mirror evaluation.py:455-464.
    if brain_check_details_present:
        meets_thresholds = brain_can_submit or (not brain_failed_checks)
    else:
        meets_thresholds = (
            sharpe >= th["sharpe_min"]
            and turnover <= th["turnover_max"]
            and fitness >= th["fitness_min"]
        )

    # self_corr from BRAIN's SELF_CORRELATION check. On synced alphas this is
    # ~always PENDING-no-value → UNKNOWN (hard_gate then fails on
    # self_corr_verified; near_pass still reachable — matches mining unverified).
    self_corr = 0.0
    self_corr_source = CorrSource.UNKNOWN
    _sc = next(
        (c for c in _checks
         if isinstance(c, dict) and c.get("name") == "SELF_CORRELATION"),
        None,
    )
    if (_sc is not None
            and _sc.get("result") in ("PASS", "FAIL")
            and _sc.get("value") is not None):
        self_corr = float(_sc.get("value") or 0.0)
        self_corr_source = CorrSource.BRAIN

    vr = compute_verdict_from_signals(
        metrics=verdict_metrics,
        sharpe=sharpe,
        fitness=fitness,
        turnover=turnover,
        self_corr=self_corr,
        self_corr_source=self_corr_source,
        meets_thresholds=meets_thresholds,
        brain_check_details_present=brain_check_details_present,
        brain_failed_checks=brain_failed_checks,
        brain_can_submit=brain_can_submit,
        score=0.0,
        should_opt=False,
        expression=expression or "",
        th=th,
        check_self_corr=True,
        check_concentrated=True,
    )

    # S1 guardrail (T4): sync's full PASS must never exceed the BRAIN submission
    # gate. compute_can_submit treats ERROR as FAIL while evaluate_with_brain_checks
    # files ERROR under pending, so a verdict PASS can outrun can_submit — demote
    # it. `is False` (not `not can_sub`): None = "no BRAIN signal", leave PASS be.
    if vr.decision.status == "PASS" and can_sub is False:
        vr.decision.status = "PASS_PROVISIONAL"
        vr.decision.reason = "brain_unsubmittable"
        logger.info(
            f"[sync] verdict PASS→PASS_PROVISIONAL (brain_unsubmittable) "
            f"| {a_data.get('id')}"
        )
    return vr


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

    # P2.C follow-up (2026-05-24): MERGE not REPLACE. BRAIN's settings.datasetId
    # is empty for FLAT cross-dataset alphas, so blindly overwriting wipes the
    # AIAC field-derived dataset_id every 6h sync — defeating the dataset bandit's
    # per-dataset reward attribution. Same class as the metrics MERGE fix below
    # (950-955): keep BRAIN's value only when it actually provides one, else
    # preserve the AIAC-derived stamp.
    _brain_ds = settings.get("datasetId")
    if _brain_ds:
        existing.dataset_id = _brain_ds

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

    # P2.C [V1.1-S2] (2026-05-20) + Feature 1 (2026-05-24): derive quality_status
    # via the shared verdict logic, but ONLY when the row is still PENDING — never
    # overwrite a mining-direct PASS/PASS_PROVISIONAL/OPTIMIZE/FAIL verdict (sync
    # is reconciliation, the mining write is authoritative). Runs AFTER the metrics
    # MERGE above so the _routing_reason stamp survives. T3: stamp _routing_reason
    # only for PASS/PASS_PROVISIONAL (mirrors evaluation.py:737-738).
    if existing.quality_status == "PENDING":
        _vr = _derive_verdict_from_brain(
            a_data, is_metrics, os_metrics, existing.expression, can_sub
        )
        if _vr is not None:
            existing.quality_status = _vr.decision.status
            if _vr.decision.status in ("PASS", "PASS_PROVISIONAL"):
                _m = dict(existing.metrics or {})
                _m["_routing_reason"] = _vr.decision.reason
                if _vr.decision.reason == "brain_actionable_fails":
                    _m["_brain_pass_downgrade"] = _vr.brain_actionable_fails
                existing.metrics = _m

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

    # Feature 1 (2026-05-24, B#4): classify a brand-new synced alpha via the
    # shared verdict logic. Compute it BEFORE the constructor (status + metrics
    # can't both be built inside one Alpha(...) call). vr is None → no core
    # metrics → PENDING (a new row has no prior status to "keep").
    _vr = _derive_verdict_from_brain(a_data, is_metrics, os_metrics, expr_code, can_sub)
    _quality_status = _vr.decision.status if _vr is not None else "PENDING"
    _metrics = {
        **(is_metrics or {}),
        "_brain_can_submit": can_sub,
        "_brain_failed_checks": failed,
        "_brain_pending_checks": pending,
    }
    # T3: stamp _routing_reason only for PASS/PASS_PROVISIONAL (mirror evaluation.py:737-738).
    if _vr is not None and _vr.decision.status in ("PASS", "PASS_PROVISIONAL"):
        _metrics["_routing_reason"] = _vr.decision.reason
        if _vr.decision.reason == "brain_actionable_fails":
            _metrics["_brain_pass_downgrade"] = _vr.brain_actionable_fails

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
        quality_status=_quality_status,
        metrics_snapshot_at=datetime.now(timezone.utc),
        metrics=_metrics,
    )
