"""V-19.7 BRAIN quota guard.

`quota_guard_pause_at_threshold` (every 10 min):
   Counts today's alpha rows (UTC date). When >= 90% of BRAIN_DAILY_SIMULATE_LIMIT,
   pauses every active (RUNNING) session as a defensive measure.
   Logs a clear reason; user can resume next day or after raising the limit.

NOTE (Phase 1c-delete): `watchdog_revive_dead_sessions` was retired here —
lease-recycle (``backend/pool/queue.py`` + the run_pool_lease_recycle beat) is
the pool's sole recovery path, and the FLAT/ONESHOT tasks it used to revive no
longer exist. _quota_guard_async survives and is also imported lazily by the
soft-robustness gate in node_evaluate (evaluation.py).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select, update, func

from backend.celery_app import celery_app
from backend.config import settings
from backend.database import AsyncSessionLocal
from backend.models import Alpha, AlphaFailure, MiningTask
from backend.models.task import TraceStep
from backend.tasks import run_async




# ---------------------------------------------------------------------------
# 2. BRAIN quota guard — pause sessions at 90% daily simulate count
# ---------------------------------------------------------------------------

@celery_app.task(name="backend.tasks.quota_guard_pause_at_threshold")
def quota_guard_pause_at_threshold():
    """Beat-scheduled. Pause active (RUNNING) sessions if today's alpha count
    approaches BRAIN_DAILY_SIMULATE_LIMIT.
    """
    return run_async(_quota_guard_async())


async def _quota_guard_async() -> dict:
    limit = getattr(settings, "BRAIN_DAILY_SIMULATE_LIMIT", 1000)
    threshold_pct = getattr(settings, "BRAIN_QUOTA_PAUSE_PCT", 0.9)
    threshold = int(limit * threshold_pct)

    now = datetime.now(timezone.utc)
    # alphas.created_at is TIMESTAMP WITHOUT TIME ZONE; the comparison value
    # must be a naive datetime to avoid asyncpg's tz subtraction error.
    today_start = datetime(now.year, now.month, now.day)

    paused: list[dict] = []
    async with AsyncSessionLocal() as db:
        # Count today's BRAIN simulate calls. Every alpha row corresponds
        # to ~1 simulate; every alpha_failure that wasn't a pre-sim
        # rejection ALSO corresponds to ~1 simulate (timeouts, sim
        # errors, server-side validation rejections all hit the API
        # before we record the failure).
        #
        # V-26.31 (2026-05-13): the original counter only looked at the
        # alphas table, missing failure rows that came from successful-but-
        # rejected sims. On a worker burning 30% of its quota on
        # sim-fail expressions, the guard would not trigger until 130%
        # actual usage — long past the threshold. Adding alpha_failures
        # closes the gap.
        # Bug A follow-up (2026-05-20): only count mining-direct alphas
        # (task_id NOT NULL). sync_user_alphas imports HISTORICAL BRAIN alphas
        # with created_at = insert-time (today), but they consumed BRAIN quota
        # historically, not today — counting them spikes the daily denominator
        # (e.g. a sync of 1040 historical rows looked like 1040 sims "today")
        # and would falsely pause live mining. Sync-imported rows have
        # task_id=NULL; mining-direct (the ones that actually burned today's
        # quota) always carry task_id.
        alpha_cnt = (
            await db.execute(
                select(func.count(Alpha.id))
                .where(
                    Alpha.created_at >= today_start,
                    Alpha.task_id.isnot(None),
                )
            )
        ).scalar() or 0
        # Bug A fix (2026-05-20): exclude pre-BRAIN skip rows. These never
        # consumed a BRAIN simulate slot, so counting them inflated this
        # denominator and paused sessions early. Two kinds:
        #   PRESIM_SKIP — pre-simulate skeleton classifier + Q10 hard reject
        #   DEDUP_SKIP  — local-DB dedup (expression already simulated; the
        #                 prior simulation consumed the slot, not this skip)
        # Honours the long-standing comment intent above ("every alpha_failure
        # that wasn't a pre-sim rejection ALSO corresponds to ~1 simulate").
        # coalesce keeps NULL-error_type rows counted.
        fail_cnt = (
            await db.execute(
                select(func.count(AlphaFailure.id))
                .where(
                    AlphaFailure.created_at >= today_start,
                    ~func.coalesce(AlphaFailure.error_type, "").in_(
                        ["PRESIM_SKIP", "DEDUP_SKIP"]
                    ),
                )
            )
        ).scalar() or 0
        cnt = alpha_cnt + fail_cnt

        if cnt < threshold:
            return {
                "today_alpha_count": alpha_cnt,
                "today_failure_count": fail_cnt,
                "today_total_count": cnt,
                "threshold": threshold,
                "limit": limit,
                "paused_count": 0,
                "paused": [],
            }

        # Over threshold — pause all active (RUNNING) sessions.
        # V-26.32 (2026-05-13): in-flight BRAIN sims can't be cancelled
        # server-side, so the PAUSE here only stops the NEXT round from
        # being scheduled. The in-flight sims (up to 3 per the global
        # slot counter) keep burning quota until they reach terminal status
        # — usually 5-10 minutes. Log the current slot count so the
        # operator sees how many sims will still complete after the
        # PAUSE before quota actually stops growing.
        try:
            from backend.tasks.redis_pool import get_redis_client
            cli = get_redis_client()
            inflight_raw = cli.get("brain:concurrent_sims")
            inflight = int(inflight_raw) if inflight_raw is not None else 0
        except Exception:
            inflight = -1

        # Post tier-system removal (2026-05-18): cascade retired; quota guard
        # now PAUSEs all RUNNING tasks (flat sessions included).
        active = (
            await db.execute(
                select(MiningTask).where(MiningTask.status == "RUNNING")
            )
        ).scalars().all()
        # V-27.7: log moved below the `active` query so the count is real —
        # the f-string previously had a literal `{?}` placeholder that was
        # never filled (the log ran before `active` was fetched).
        logger.warning(
            f"[quota_guard] V-26.32 PAUSING {len(active)} sessions over quota "
            f"(today_total={cnt} alphas={alpha_cnt} failures={fail_cnt}); "
            f"BRAIN inflight_sims={inflight} will continue to terminal status "
            f"before quota growth halts"
        )
        for task in active:
            try:
                await db.execute(
                    update(MiningTask)
                    .where(MiningTask.id == task.id)
                    .values(status="PAUSED")
                )
                paused.append({
                    "task_id": task.id,
                    "region": task.region,
                    # phase15-D PR3b dropped cascade_phase col; quota guard
                    # pause log no longer carries a phase field.
                })
                logger.warning(
                    f"[quota_guard] PAUSED task={task.id} region={task.region} "
                    f"(today_total={cnt} alphas={alpha_cnt} failures={fail_cnt} >= {threshold})"
                )
            except Exception as e:
                logger.error(f"[quota_guard] failed to pause task={task.id}: {e}")
        if paused:
            await db.commit()

    return {
        "today_alpha_count": alpha_cnt,
        "today_failure_count": fail_cnt,
        "today_total_count": cnt,
        "threshold": threshold,
        "limit": limit,
        "paused_count": len(paused),
        "paused": paused,
    }
