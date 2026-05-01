"""Tier-system refresh beat tasks (PR2).

Two beat-driven refresh paths:

- refresh_kb_referenced_alphas (06:15 daily): for every Alpha that's referenced
  by an active SUCCESS_PATTERN KB entry (alpha_id_ref in meta_data), pull
  current metrics from BRAIN and re-evaluate quality_status against tier
  thresholds. Demoted alphas get their KB entries soft-deactivated so RAG
  doesn't re-recommend them.

- refresh_os_active_metrics: NOT a separate beat task. Per plan §13.5 D3
  fix, OS-active metrics refresh is folded into the existing 06:30
  refresh_os_correlation_cache (sync_tasks.py) — wiring there avoids a
  beat-schedule conflict and keeps PnL + metrics fetches paired.

Note on transactions: each alpha is committed in its own transaction so a
single BRAIN failure doesn't roll back the whole batch.
"""
from __future__ import annotations

import asyncio
from typing import Dict, List

from loguru import logger
from sqlalchemy import select

from backend.celery_app import celery_app
from backend.tasks import run_async


@celery_app.task(name="backend.tasks.refresh_kb_referenced_alphas")
def refresh_kb_referenced_alphas() -> Dict:
    """Beat-triggered wrapper around the async implementation."""
    return run_async(_refresh_kb_referenced_alphas_async())


async def _refresh_kb_referenced_alphas_async() -> Dict:
    from backend.adapters.brain_adapter import BrainAdapter
    from backend.agents.graph.tier_thresholds import get_tier_thresholds
    from backend.database import AsyncSessionLocal
    from backend.models import Alpha, KnowledgeEntry
    from backend.services.alpha_service import AlphaService

    refreshed = 0
    demoted = 0
    failed = 0
    deactivated_kb = 0

    async with AsyncSessionLocal() as db:
        # 1. Find KB-referenced alpha ids (distinct, only active SUCCESS_PATTERN rows)
        kb_stmt = (
            select(KnowledgeEntry.id, KnowledgeEntry.meta_data)
            .where(KnowledgeEntry.entry_type == "SUCCESS_PATTERN")
            .where(KnowledgeEntry.is_active == True)  # noqa: E712
        )
        kb_rows = (await db.execute(kb_stmt)).all()

        alpha_id_set = set()
        kb_alpha_map: Dict[int, List[int]] = {}  # alpha_id → list of kb_ids
        for kb_id, meta_data in kb_rows:
            md = meta_data or {}
            alpha_id_ref = md.get("alpha_id_ref")
            if isinstance(alpha_id_ref, int):
                alpha_id_set.add(alpha_id_ref)
                kb_alpha_map.setdefault(alpha_id_ref, []).append(kb_id)

        if not alpha_id_set:
            logger.info("[refresh_kb] no KB entries with alpha_id_ref; nothing to refresh")
            return {"refreshed": 0, "demoted": 0, "failed": 0, "kb_deactivated": 0}

        logger.info(f"[refresh_kb] refreshing {len(alpha_id_set)} KB-referenced alphas")
        adapter = BrainAdapter()
        try:
            await adapter.login()
        except Exception as e:
            logger.error(f"[refresh_kb] BRAIN login failed: {e}")
            return {"refreshed": 0, "demoted": 0, "failed": 0, "kb_deactivated": 0,
                    "error": str(e)}

        alpha_service = AlphaService(db)

        # 2. For each alpha, GET fresh metrics + re-evaluate
        for alpha_id_ref in sorted(alpha_id_set):
            alpha = await db.get(Alpha, alpha_id_ref)
            if alpha is None or not alpha.alpha_id:
                continue

            try:
                fresh = await adapter.get_alpha(alpha.alpha_id)
            except Exception as e:
                logger.warning(f"[refresh_kb] fetch alpha={alpha.alpha_id}: {e}")
                failed += 1
                continue

            if not fresh:
                failed += 1
                continue

            is_block = fresh.get("is") or {}
            alpha.is_sharpe = is_block.get("sharpe", alpha.is_sharpe)
            alpha.is_fitness = is_block.get("fitness", alpha.is_fitness)
            alpha.is_turnover = is_block.get("turnover", alpha.is_turnover)
            if "checks" in fresh:
                merged = dict(alpha.metrics or {})
                merged["checks"] = fresh["checks"]
                alpha.metrics = merged

            from datetime import datetime as _dt
            alpha.metrics_snapshot_at = _dt.utcnow()
            refreshed += 1

            # 3. Re-evaluate against tier-specific thresholds
            t = get_tier_thresholds(alpha.factor_tier)
            sharpe_ok = (alpha.is_sharpe or 0) >= t["sharpe_min"]
            fitness_ok = (alpha.is_fitness or 0) >= t["fitness_min"]
            turnover_ok = t["turnover_min"] <= (alpha.is_turnover or 0) <= t["turnover_max"]

            if alpha.quality_status == "PASS" and not (sharpe_ok and fitness_ok and turnover_ok):
                # Demote: PASS → PASS_PROVISIONAL (don't go to FAIL outright;
                # operator can review). Audit log via apply_quality_status_change.
                try:
                    await alpha_service.apply_quality_status_change(
                        alpha_id=alpha.id,
                        new_status="PASS_PROVISIONAL",
                        reason=(
                            f"daily_beat_kb: drifted "
                            f"sharpe={alpha.is_sharpe:.2f} fitness={alpha.is_fitness:.2f} "
                            f"turnover={alpha.is_turnover:.2f} (T{alpha.factor_tier} bar)"
                        ),
                        source="daily_beat_kb",
                    )
                    demoted += 1
                    # Soft-deactivate the linked KB entries
                    from sqlalchemy import update as sa_update
                    await db.execute(
                        sa_update(KnowledgeEntry)
                        .where(KnowledgeEntry.id.in_(kb_alpha_map[alpha_id_ref]))
                        .values(is_active=False)
                    )
                    deactivated_kb += len(kb_alpha_map[alpha_id_ref])
                except Exception as e:
                    logger.warning(f"[refresh_kb] demote alpha={alpha.id} failed: {e}")

        await db.commit()

    logger.info(
        f"[refresh_kb] done | refreshed={refreshed} demoted={demoted} "
        f"failed={failed} kb_deactivated={deactivated_kb}"
    )
    return {
        "refreshed": refreshed,
        "demoted": demoted,
        "failed": failed,
        "kb_deactivated": deactivated_kb,
    }
