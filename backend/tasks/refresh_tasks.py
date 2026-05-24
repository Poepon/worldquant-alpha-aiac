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
from backend.tasks._role_helpers import read_role_snapshot


def _should_promote_provisional(
    *,
    quality_status,
    can_submit,
    routing_reason,
    is_sharpe,
    is_fitness,
    is_turnover,
    thresholds: Dict,
) -> bool:
    """Pure predicate: should a PASS_PROVISIONAL alpha be promoted to PASS?

    2026-05-24: closes the one-way ratchet (refresh_kb only DEMOTES). An alpha
    held PASS_PROVISIONAL purely because BRAIN's ``is.checks`` were still empty at
    sim-time (routing reason ``brain_checks_unverified``) was otherwise frozen
    forever even after BRAIN confirmed it. Promote ONLY when:

      - it is currently PASS_PROVISIONAL, AND
      - the hold reason was the empty-checks timing artifact, AND
      - BRAIN now confirms ``can_submit`` is True (so BRAIN doesn't reject it —
        a still-PENDING BRAIN SELF_CORRELATION is fine because the verified local
        self_corr already satisfied that gate at eval time; this is the
        local-OR-BRAIN self_corr the gate intends), AND
      - the full hard band is met (not just the provisional band).

    Deliberately does NOT promote ``near_pass`` / ``v16_hard_flags`` /
    originality holds — those carry a different routing reason.
    """
    if quality_status != "PASS_PROVISIONAL":
        return False
    if can_submit is not True:
        return False
    if routing_reason != "brain_checks_unverified":
        return False
    return (
        (is_sharpe or 0) >= thresholds["sharpe_min"]
        and (is_fitness or 0) >= thresholds["fitness_min"]
        and thresholds["turnover_min"] <= (is_turnover or 0) <= thresholds["turnover_max"]
    )


@celery_app.task(name="backend.tasks.refresh_kb_referenced_alphas")
def refresh_kb_referenced_alphas() -> Dict:
    """Beat-triggered wrapper around the async implementation."""
    return run_async(_refresh_kb_referenced_alphas_async())


async def _refresh_kb_referenced_alphas_async() -> Dict:
    from backend.adapters.brain_adapter import BrainAdapter
    from backend.agents.graph.nodes.evaluation import _eval_thresholds
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

            # 3. Re-evaluate against the flat thresholds.
            # BRAIN role-switch (P3-Brain): read task-snapshot sharpe override
            # so running tasks don't get re-judged by Consultant 1.58 mid-run.
            _role_snapshot = await read_role_snapshot(alpha.task_id, db)
            t = _eval_thresholds(
                sharpe_submit_min_override=_role_snapshot.get("effective_sharpe_submit_min"),
            )
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
                            f"turnover={alpha.is_turnover:.2f}"
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


# ---------------------------------------------------------------------------
# alpha_pnl persistence (2026-05-24) — the table was empty; PnL was fetched for
# self_corr at eval time then discarded. Mining stores it inline at the post-sim
# refresh; sync enqueues an incremental backfill (skip-if-exists) so it does NOT
# re-fetch the whole pool every 6h cycle.
# ---------------------------------------------------------------------------

async def _fetch_and_store_pnl(db, svc, alpha_pk: int, brain_alpha_id: str) -> int:
    """Fetch an alpha's PnL from BRAIN and persist it to alpha_pnl.

    Own BrainAdapter; soft-fail so a PnL hiccup never affects the caller. Empty
    fetch → no-op (upsert_alpha_pnl never wipes on empty). Commits on a non-empty
    write. Returns rows written (0 on empty / failure)."""
    if not brain_alpha_id:
        return 0
    try:
        from backend.adapters.brain_adapter import BrainAdapter
        from backend.services.correlation_service import CorrelationService
        async with BrainAdapter() as _ba:
            series = await CorrelationService(_ba)._fetch_pnl_series(brain_alpha_id)
        n = await svc.upsert_alpha_pnl(alpha_pk, series)
        if n:
            await db.commit()
            logger.info(f"[alpha_pnl] stored {n} rows for alpha_pk={alpha_pk}")
        return n
    except Exception as e:
        logger.warning(f"[alpha_pnl] persist failed for alpha_pk={alpha_pk}: {e}")
        return 0


@celery_app.task(name="backend.tasks.store_alpha_pnl_for_alpha")
def store_alpha_pnl_for_alpha(alpha_pk: int) -> Dict:
    """Fetch + persist one alpha's daily PnL. Enqueued by sync for alphas that
    lack stored PnL — INCREMENTAL: skips if PnL already exists, so sync does not
    re-fetch the whole pool every cycle. Idempotent."""
    return run_async(_store_alpha_pnl_async(alpha_pk))


async def _store_alpha_pnl_async(alpha_pk: int) -> Dict:
    from sqlalchemy import func as _f

    from backend.database import AsyncSessionLocal
    from backend.models import Alpha, AlphaPnl
    from backend.services.alpha_service import AlphaService

    async with AsyncSessionLocal() as db:
        existing_cnt = (await db.execute(
            select(_f.count(AlphaPnl.id)).where(AlphaPnl.alpha_id == alpha_pk)
        )).scalar() or 0
        if existing_cnt > 0:
            return {"alpha_pk": alpha_pk, "stored": 0, "skipped": "already_has_pnl"}
        alpha = await db.get(Alpha, alpha_pk)
        if alpha is None or not alpha.alpha_id:
            return {"alpha_pk": alpha_pk, "stored": 0, "skipped": True}
        svc = AlphaService(db)
        n = await _fetch_and_store_pnl(db, svc, alpha_pk, alpha.alpha_id)
        return {"alpha_pk": alpha_pk, "stored": n}


def enqueue_alpha_pnl_store(
    alpha_pk: int, brain_alpha_id: str | None = None, countdown: int = 10
) -> None:
    """Fire-and-forget: schedule an incremental PnL store. Skips when no BRAIN id.
    Wrapped so a broker outage cannot break the sync pipeline."""
    if not brain_alpha_id:
        return
    try:
        store_alpha_pnl_for_alpha.apply_async(args=[alpha_pk], countdown=countdown)
    except Exception as e:
        logger.warning(f"[alpha_pnl] enqueue failed for alpha_pk={alpha_pk}: {e}")


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

        # 2026-05-24: promote PASS_PROVISIONAL → PASS when the provisional hold was
        # purely the empty-BRAIN-checks timing artifact (routing reason
        # `brain_checks_unverified`) and BRAIN now confirms submission-grade. This
        # 30s refresh is exactly when the previously-empty `is.checks` resolve.
        # Closes the one-way ratchet: refresh_kb only DEMOTES, so an alpha held
        # provisional at sim-time (checks not yet computed) was otherwise frozen
        # forever even after BRAIN confirmed it. Local self_corr already satisfied
        # the self-corr gate at eval time, so a still-PENDING BRAIN SELF_CORRELATION
        # does not block — can_submit=True means BRAIN doesn't reject it (the
        # local-OR-BRAIN self_corr the gate intends). Tightly scoped: only this
        # routing reason, only when the full hard band is met; no-op (audited) else.
        if (
            alpha is not None
            and alpha.quality_status == "PASS_PROVISIONAL"
            and result.get("can_submit") is True
            and (alpha.metrics or {}).get("_routing_reason") == "brain_checks_unverified"
        ):
            try:
                from backend.agents.graph.nodes.evaluation import _eval_thresholds
                _snap = await read_role_snapshot(alpha.task_id, db)
                _t = _eval_thresholds(
                    sharpe_submit_min_override=_snap.get("effective_sharpe_submit_min")
                )
                if _should_promote_provisional(
                    quality_status=alpha.quality_status,
                    can_submit=result.get("can_submit"),
                    routing_reason=(alpha.metrics or {}).get("_routing_reason"),
                    is_sharpe=alpha.is_sharpe,
                    is_fitness=alpha.is_fitness,
                    is_turnover=alpha.is_turnover,
                    thresholds=_t,
                ):
                    promoted = await svc.apply_quality_status_change(
                        alpha_id=alpha_pk,
                        new_status="PASS",
                        reason=(
                            f"can_submit_refresh: BRAIN checks verified post-sim "
                            f"(can_submit=True, sharpe={alpha.is_sharpe:.2f} "
                            f"fitness={alpha.is_fitness:.2f} turnover={alpha.is_turnover:.2f}); "
                            f"was held provisional on brain_checks_unverified"
                        ),
                        source="can_submit_refresh",
                    )
                    if promoted:
                        await db.commit()
                        logger.info(
                            f"[refresh_can_submit] promoted alpha_pk={alpha_pk} "
                            f"PASS_PROVISIONAL→PASS (BRAIN checks now verified)"
                        )
            except Exception as e:
                logger.warning(
                    f"[refresh_can_submit] promote alpha_pk={alpha_pk} failed: {e}"
                )

        # 2026-05-24: persist the alpha's daily PnL into alpha_pnl (the table sat
        # empty — PnL was fetched for self_corr at eval time then discarded).
        if alpha is not None and alpha.alpha_id:
            await _fetch_and_store_pnl(db, svc, alpha_pk, alpha.alpha_id)

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
    -performance and stores the resulting Δsharpe / Δfitness / Δturnover into
    alpha.metrics._iqc_marginal. Doesn't change submission state — the user
    still decides; this just surfaces the team-impact signal.

    2026-05-24: BRAIN removed the competition `score` from this endpoint, so
    delta_score is no longer stored (it was always advisory and the dataset
    bandit already moved off it to binary can_submit). The standalone-vs-merged
    stats deltas + merged sharpe/fitness remain the team-impact signal.

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
            analysis = result.get("analysis") or {}
            new_metrics = dict(alpha.metrics or {})
            new_metrics["_iqc_marginal"] = {
                "competition": competition,
                "audited_at": datetime.now(timezone.utc).isoformat(),
                # delta_score dropped 2026-05-24 (BRAIN removed `score` from the
                # before-and-after endpoint). partition_name is the new label.
                "partition_name": result.get("partition_name"),
                "delta_sharpe": deltas.get("sharpe"),
                "delta_fitness": deltas.get("fitness"),
                "delta_turnover": deltas.get("turnover"),
                "delta_returns": deltas.get("returns"),
                "delta_pnl": deltas.get("pnl"),
                "merged_sharpe": (stats.get("after") or {}).get("sharpe"),
                "merged_fitness": (stats.get("after") or {}).get("fitness"),
                # Multi-dimensional verdict (3rd review): persist so the "可提交"
                # list + bandit can sort/flag by it without re-hitting BRAIN.
                "recommendation": analysis.get("recommendation"),
                "composite_score": analysis.get("composite_score"),
                "guardrails": analysis.get("guardrails"),
                # V-23.E: explicit fresh-after-audit flag (default false).
                # sync_user_alphas flips this to true on submission flip;
                # sweep prioritises stale=true to refresh first.
                "stale": False,
            }
            await db.execute(
                update(Alpha).where(Alpha.id == alpha_pk).values(metrics=new_metrics)
            )
            await db.commit()
            # V-26.84: release sweep-side inflight lock on successful audit.
            try:
                from backend.tasks.redis_pool import release_iqc_audit_lock
                release_iqc_audit_lock(alpha_pk)
            except Exception:
                pass
            return {
                "alpha_pk": alpha_pk,
                "competition": competition,
                "delta_sharpe": deltas.get("sharpe"),
                "delta_fitness": deltas.get("fitness"),
            }
    except Exception as e:
        logger.warning(f"[audit_iqc_marginal] alpha_pk={alpha_pk} failed: {e}")
        # V-26.86 (2026-05-13): increment a per-alpha failure counter so the
        # sweep can stop re-queuing alphas that BRAIN consistently rejects
        # (HTTP 400 on weird alpha_id, 500 server side, etc.). Filter is
        # applied in _iqc_audit_backfill_sweep_async via the
        # `_iqc_marginal.audit_failures` JSONB path.
        try:
            from sqlalchemy import select, update as _u
            from backend.database import AsyncSessionLocal
            from backend.models import Alpha
            async with AsyncSessionLocal() as fail_db:
                cur_metrics = (
                    await fail_db.execute(
                        select(Alpha.metrics).where(Alpha.id == alpha_pk)
                    )
                ).scalar_one_or_none() or {}
                m = dict(cur_metrics) if isinstance(cur_metrics, dict) else {}
                iqc = dict(m.get("_iqc_marginal") or {})
                iqc["audit_failures"] = int(iqc.get("audit_failures") or 0) + 1
                iqc["last_error"] = str(e)[:200]
                m["_iqc_marginal"] = iqc
                await fail_db.execute(
                    _u(Alpha).where(Alpha.id == alpha_pk).values(metrics=m)
                )
                await fail_db.commit()
        except Exception as _failed_to_bump:
            logger.warning(
                f"[audit_iqc_marginal] V-26.86 failure-counter bump failed: {_failed_to_bump}"
            )
        finally:
            # V-26.84: release sweep-side inflight lock so next sweep tick
            # can re-enqueue (subject to retry-count filter applied there).
            try:
                from backend.tasks.redis_pool import release_iqc_audit_lock
                release_iqc_audit_lock(alpha_pk)
            except Exception:
                pass
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
        # V-26.86 (2026-05-13): exclude alphas with audit_failures >= 3 so
        # the sweep stops re-burning BRAIN budget on consistently-failing
        # IDs. The counter is stamped in _audit_iqc_marginal_async's
        # except block.
        max_failures = 3
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
                      AND COALESCE((metrics->'_iqc_marginal'->>'audit_failures')::int, 0) < :max_fail
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
            {"lim": _iqc_settings.IQC_AUDIT_BACKFILL_LIMIT, "max_fail": max_failures},
        )
        pks = [r[0] for r in rows.all()]

    # V-26.84 (2026-05-13): claim a Redis in-flight lock per alpha_pk
    # before enqueueing. Prevents this sweep tick from piling another
    # celery task on top of an audit that's still running from the
    # previous tick. Lock TTL is 10min (longer than worst-case audit) so
    # if the worker crashes mid-audit the sweep will eventually retry.
    from backend.tasks.redis_pool import claim_iqc_audit_lock
    countdown_sec = _iqc_settings.IQC_AUDIT_BACKFILL_COUNTDOWN_SEC
    skipped_inflight = 0
    for pk in pks:
        if not claim_iqc_audit_lock(pk):
            skipped_inflight += 1
            continue
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
        f"skipped_inflight={skipped_inflight} competition={competition}"
    )
    return {
        "enqueued": enqueued,
        "skipped_inflight": skipped_inflight,
        "competition": competition,
    }
