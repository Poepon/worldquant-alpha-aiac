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
    from backend.config import settings
    from backend.database import AsyncSessionLocal
    from backend.models import Alpha, KnowledgeEntry
    from backend.services.alpha_service import AlphaService

    # PR4 — P0 experiment found BRAIN returns frozen IS metrics snapshots, so
    # this beat is a no-op when re-fetching cached PASS alphas. Default-off.
    if not getattr(settings, "REFRESH_KB_VIA_BRAIN", False):
        logger.info(
            "[refresh_kb] skipped (REFRESH_KB_VIA_BRAIN=False; "
            "BRAIN returns frozen IS snapshots — refresh is no-op for cached alphas)"
        )
        return {"skipped": True, "refreshed": 0, "demoted": 0, "failed": 0, "kb_deactivated": 0}

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


def enqueue_can_submit_refresh(alpha_pk: int, brain_alpha_id: str | None = None, countdown: int = 30) -> None:
    """Fire-and-forget helper: schedule can_submit refresh `countdown` seconds
    after now. Skips when alpha lacks a BRAIN id. Wrapped in try/except so a
    Celery broker outage cannot break the mining pipeline.
    """
    if not brain_alpha_id:
        return
    try:
        refresh_can_submit_for_alpha.apply_async(args=[alpha_pk], countdown=countdown)
    except Exception as e:
        logger.warning(f"[refresh_can_submit] enqueue failed for alpha_pk={alpha_pk}: {e}")


@celery_app.task(name="backend.tasks.refresh_can_submit_for_alpha")
def refresh_can_submit_for_alpha(alpha_pk: int) -> Dict:
    """Auto-triggered post-simulation refresh of can_submit.

    Called via .apply_async(args=[alpha_pk], countdown=30) from the evaluation
    node — the 30s buffer lets BRAIN finish computing CONCENTRATED_WEIGHT and
    LOW_SUB_UNIVERSE_SHARPE checks (which are async on BRAIN's side and
    typically arrive ~10-20s after the simulate response).

    Idempotent: safe to retry. Returns {can_submit, alpha_pk, error?}.
    """
    return run_async(_refresh_can_submit_async(alpha_pk))


async def _refresh_can_submit_async(alpha_pk: int) -> Dict:
    from backend.database import AsyncSessionLocal
    from backend.models import Alpha
    from backend.services.alpha_service import AlphaService

    try:
        async with AsyncSessionLocal() as db:
            svc = AlphaService(db)
            result = await svc.refresh_can_submit(alpha_pk)
            if result is None:
                return {"alpha_pk": alpha_pk, "can_submit": None, "skipped": True}

            # P1: can_submit=False → 把每个 BRAIN FAIL 写回 RAG pitfall 池，
            # 让下一轮 LLM 能学到「避坑」。同条 alpha 多个 FAIL 各算一个 pitfall。
            #
            # V-22 (2026-05-10): also write the BRAIN verdict back into the
            # SUCCESS_PATTERN entry that owns this alpha's skeleton — closes
            # the LLM feedback loop so the next round's RAG retrieval can
            # surface "this skeleton was IS-PASS but BRAIN rejected on
            # FITNESS/CW/SELF_CORR". Runs for both can_submit=True and
            # =False (True clears any stale failed_checks; False stamps them).
            kb_recorded = 0
            from backend.agents.services.rag_service import RAGService

            alpha = await svc.alpha_repo.get_by_id(alpha_pk)
            if alpha and alpha.expression:
                rag = RAGService(db)

                # V-22: brain_status update on the SUCCESS_PATTERN entry
                try:
                    await rag.update_pattern_brain_status(
                        expression=alpha.expression,
                        can_submit=result["can_submit"],
                        failed_checks=result.get("failed_checks") or [],
                    )
                except Exception as e:
                    logger.warning(
                        f"[refresh_can_submit] V-22 brain_status update failed for "
                        f"alpha_pk={alpha_pk}: {e}"
                    )

                # P1: pitfall write (only when can_submit=False)
                if result["can_submit"] is False and result["failed_checks"]:
                    for chk in result["failed_checks"]:
                        check_name = chk.get("name")
                        if not check_name:
                            continue
                        try:
                            await rag.record_failure_pattern(
                                expression=alpha.expression,
                                error_type=check_name,
                                metrics=alpha.metrics or {},
                                region=alpha.region,
                                dataset_id=alpha.dataset_id,
                            )
                            kb_recorded += 1
                        except Exception as e:
                            logger.warning(
                                f"[refresh_can_submit] RAG record failed for "
                                f"alpha_pk={alpha_pk} check={check_name}: {e}"
                            )
                    if kb_recorded > 0:
                        await db.commit()

        # V-22.12 (2026-05-13): when refresh flips can_submit=True, enqueue
        # an IQC marginal-contribution audit. Stores Δscore / Δsharpe into
        # alpha.metrics._iqc_marginal so frontend can surface "actually adds
        # value to team portfolio" filter. Fire-and-forget — failure here
        # never blocks the refresh result.
        if result.get("can_submit") is True:
            try:
                competition = getattr(
                    __import__("backend.config", fromlist=["settings"]).settings,
                    "IQC_AUTO_AUDIT_COMPETITION",
                    "IQC2026S1",
                )
                if competition:
                    audit_iqc_marginal_for_alpha.apply_async(
                        args=[alpha_pk, competition], countdown=5,
                    )
            except Exception as e:
                logger.warning(
                    f"[refresh_can_submit] V-22.12 IQC audit enqueue failed for "
                    f"alpha_pk={alpha_pk}: {e}"
                )

        return {
            "alpha_pk": alpha_pk,
            "can_submit": result["can_submit"],
            "fail_count": len(result["failed_checks"]),
            "pending_count": len(result["pending_checks"]),
            "kb_pitfalls_recorded": kb_recorded,
        }
    except Exception as e:
        logger.warning(f"[refresh_can_submit] alpha_pk={alpha_pk} failed: {e}")
        return {"alpha_pk": alpha_pk, "can_submit": None, "error": str(e)}


# ---------------------------------------------------------------------------
# V-22.12 (2026-05-13) — IQC marginal-contribution auto-audit
# ---------------------------------------------------------------------------

@celery_app.task(name="backend.tasks.audit_iqc_marginal_for_alpha")
def audit_iqc_marginal_for_alpha(alpha_pk: int, competition: str = "IQC2026S1") -> Dict:
    """Auto-triggered after can_submit refresh flips True.

    Calls BRAIN /competitions/{competition}/alphas/{alpha_id}/before-and-after
    -performance and stores the resulting Δscore / Δsharpe / Δturnover into
    alpha.metrics._iqc_marginal. Doesn't change submission state — the user
    still decides; this just surfaces the team-impact signal.

    Idempotent: re-runs overwrite the metric. Failure is silent (logged at
    warning level) — IQC audit is a nice-to-have, not blocking.
    """
    return run_async(_audit_iqc_marginal_async(alpha_pk, competition))


async def _audit_iqc_marginal_async(alpha_pk: int, competition: str) -> Dict:
    from datetime import datetime, timezone
    from sqlalchemy import update
    from backend.adapters.brain_adapter import BrainAdapter
    from backend.database import AsyncSessionLocal
    from backend.models import Alpha
    from backend.services.alpha_service import AlphaService

    try:
        async with AsyncSessionLocal() as db:
            svc = AlphaService(db)
            async with BrainAdapter() as brain:
                result = await svc.get_marginal_contribution(
                    alpha_pk=alpha_pk,
                    competition=competition,
                    brain_adapter=brain,
                )
                if result is None:
                    return {"alpha_pk": alpha_pk, "skipped": True}

            # Merge audit info into alpha.metrics._iqc_marginal
            alpha = await svc.alpha_repo.get_by_id(alpha_pk)
            if alpha is None:
                return {"alpha_pk": alpha_pk, "skipped": True, "reason": "alpha_not_found"}

            deltas = result.get("deltas") or {}
            stats = (result.get("raw") or {}).get("stats") or {}
            new_metrics = dict(alpha.metrics or {})
            new_metrics["_iqc_marginal"] = {
                "competition": competition,
                "audited_at": datetime.now(timezone.utc).isoformat(),
                "delta_score": deltas.get("score"),
                "delta_sharpe": deltas.get("sharpe"),
                "delta_fitness": deltas.get("fitness"),
                "delta_turnover": deltas.get("turnover"),
                "delta_returns": deltas.get("returns"),
                "delta_pnl": deltas.get("pnl"),
                "merged_sharpe": (stats.get("after") or {}).get("sharpe"),
                "merged_fitness": (stats.get("after") or {}).get("fitness"),
                # V-23.E: explicit fresh-after-audit flag (default false).
                # sync_user_alphas flips this to true on submission flip;
                # sweep prioritises stale=true to refresh first.
                "stale": False,
            }
            await db.execute(
                update(Alpha).where(Alpha.id == alpha_pk).values(metrics=new_metrics)
            )
            await db.commit()
            return {
                "alpha_pk": alpha_pk,
                "competition": competition,
                "delta_score": deltas.get("score"),
                "delta_sharpe": deltas.get("sharpe"),
            }
    except Exception as e:
        logger.warning(f"[audit_iqc_marginal] alpha_pk={alpha_pk} failed: {e}")
        return {"alpha_pk": alpha_pk, "error": str(e)}


# ---------------------------------------------------------------------------
# V-22.12.1 (2026-05-13) — IQC audit beat fallback sweep
# ---------------------------------------------------------------------------
# The V-22.12 enqueue lives inside refresh_can_submit_for_alpha. Anything that
# flipped can_submit=True without going through that path (sync_user_alphas
# back-fill, pre-V-22.12 workers, broker outage etc.) never enqueues an audit.
# Sweep periodically picks up the stragglers so the dataset trends toward
# fully-audited regardless of how an alpha became submittable.

# Cap per sweep so we don't blast BRAIN with a thousand requests if the
# backlog is huge — the beat re-runs and chews through the queue.
# V-26.83 (2026-05-13): module-level constant kept as alias for legacy callers
# (tests / scripts that import the name). Live value resolves from settings.
from backend.config import settings as _iqc_settings
IQC_AUDIT_BACKFILL_LIMIT = _iqc_settings.IQC_AUDIT_BACKFILL_LIMIT


@celery_app.task(name="backend.tasks.iqc_audit_backfill_sweep")
def iqc_audit_backfill_sweep() -> Dict:
    """Beat-driven sweep enqueuing IQC marginal audits for can_submit=true
    alphas missing the _iqc_marginal metric. Idempotent: alphas already
    audited are skipped by the WHERE clause.
    """
    return run_async(_iqc_audit_backfill_sweep_async())


async def _iqc_audit_backfill_sweep_async() -> Dict:
    from sqlalchemy import text as _text
    from backend.config import settings
    from backend.database import AsyncSessionLocal

    competition = getattr(settings, "IQC_AUTO_AUDIT_COMPETITION", "IQC2026S1")
    if not competition:
        return {"skipped": True, "reason": "IQC_AUTO_AUDIT_COMPETITION empty"}

    enqueued = 0
    async with AsyncSessionLocal() as db:
        # date_submitted filter: BRAIN's before-and-after-performance endpoint
        # is for *submission candidates* only — already-submitted alphas return
        # 400. Filtering them out keeps the sweep idempotent + avoids burning
        # BRAIN call budget on alphas we'd just skip anyway.
        #
        # V-23.E (2026-05-13): IQC marginal Δscore is a dynamic snapshot — after
        # any submission the portfolio state changes and prior audits go stale.
        # sync_user_alphas marks affected alphas with _iqc_marginal.stale=true.
        # Sweep picks up stale alphas in addition to never-audited ones, with
        # priority order: stale > never-audited (re-audit current can_submit
        # candidates before back-filling old ones). Stale candidates already
        # have a row in metrics._iqc_marginal so the predicate differs from
        # the "missing key" predicate — both are unioned below.
        rows = await db.execute(
            _text(
                """
                SELECT id, audit_priority FROM (
                    SELECT id, 0 AS audit_priority, updated_at
                    FROM alphas
                    WHERE can_submit = true
                      AND alpha_id IS NOT NULL
                      AND date_submitted IS NULL
                      AND metrics ? '_iqc_marginal'
                      AND (metrics->'_iqc_marginal'->>'stale')::boolean = true
                    UNION ALL
                    SELECT id, 1 AS audit_priority, updated_at
                    FROM alphas
                    WHERE can_submit = true
                      AND alpha_id IS NOT NULL
                      AND date_submitted IS NULL
                      AND (metrics IS NULL OR NOT (metrics ? '_iqc_marginal'))
                ) AS pri
                ORDER BY audit_priority ASC, updated_at DESC NULLS LAST
                LIMIT :lim
                """
            ),
            # V-26.83: cap sourced from settings so beat-level tuning doesn't
            # need a code edit. Aliased module constant keeps imports stable.
            {"lim": _iqc_settings.IQC_AUDIT_BACKFILL_LIMIT},
        )
        pks = [r[0] for r in rows.all()]

    countdown_sec = _iqc_settings.IQC_AUDIT_BACKFILL_COUNTDOWN_SEC
    for pk in pks:
        try:
            audit_iqc_marginal_for_alpha.apply_async(
                args=[pk, competition], countdown=countdown_sec,
            )
            enqueued += 1
        except Exception as e:
            logger.warning(
                f"[iqc_audit_backfill_sweep] enqueue failed for alpha_pk={pk}: {e}"
            )
    logger.info(
        f"[iqc_audit_backfill_sweep] enqueued={enqueued} "
        f"competition={competition}"
    )
    return {"enqueued": enqueued, "competition": competition}
