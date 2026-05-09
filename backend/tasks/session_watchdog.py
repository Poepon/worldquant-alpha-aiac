"""V-19.7 Persistent Mining Service watchdog + BRAIN quota guard.

Two beat-driven Celery tasks:

1. `watchdog_revive_dead_sessions` (every 5 min)
   Detects CONTINUOUS_CASCADE sessions whose worker stopped emitting heartbeat
   (alpha persistence). Re-dispatches the celery worker so the session keeps
   making progress without user intervention.

   Heuristic: session is "dead" when status='RUNNING' AND
              last_alpha_persisted_at < NOW() - DEAD_THRESHOLD_MIN
   Grace: skip sessions whose task.created_at > NOW() - DEAD_THRESHOLD_MIN
          (a fresh session may not have produced its first alpha yet — IX-6
          mitigation against false-positive at start-up).

2. `quota_guard_pause_at_threshold` (every 10 min)
   Counts today's alpha rows (UTC date). When >= 90% of BRAIN_DAILY_SIMULATE_LIMIT,
   pauses every active CONTINUOUS_CASCADE session as a defensive measure.
   Logs a clear reason; user can resume next day or after raising the limit.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select, update, func

from backend.celery_app import celery_app
from backend.config import settings
from backend.database import AsyncSessionLocal
from backend.models import Alpha, ExperimentRun, MiningTask
from backend.tasks import run_async


# ---------------------------------------------------------------------------
# 1. Watchdog — revive dead sessions
# ---------------------------------------------------------------------------

@celery_app.task(name="backend.tasks.watchdog_revive_dead_sessions")
def watchdog_revive_dead_sessions():
    """Beat-scheduled. Re-dispatch celery worker for dead-but-RUNNING sessions."""
    return run_async(_watchdog_revive_async())


async def _watchdog_revive_async() -> dict:
    dead_threshold_min = getattr(settings, "CASCADE_WATCHDOG_DEAD_MIN", 15)
    grace_min = getattr(settings, "CASCADE_WATCHDOG_GRACE_MIN", 15)
    now = datetime.now(timezone.utc)
    dead_cutoff = now - timedelta(minutes=dead_threshold_min)
    grace_cutoff = now - timedelta(minutes=grace_min)

    revived = []
    async with AsyncSessionLocal() as db:
        stmt = (
            select(MiningTask)
            .where(MiningTask.mining_mode == "CONTINUOUS_CASCADE")
            .where(MiningTask.status == "RUNNING")
        )
        rows = (await db.execute(stmt)).scalars().all()
        for task in rows:
            # Skip fresh sessions (grace period) — they may not have persisted
            # their first alpha yet.
            if task.created_at and task.created_at > grace_cutoff:
                continue
            # Heartbeat fresh enough?
            if task.last_alpha_persisted_at and task.last_alpha_persisted_at > dead_cutoff:
                continue
            # Dead — re-dispatch
            try:
                run = ExperimentRun(
                    task_id=task.id,
                    status="RUNNING",
                    trigger_source="WATCHDOG_REVIVE",
                    celery_task_id=None,
                    config_snapshot={
                        "watchdog_revive": {
                            "at": now.isoformat(),
                            "last_alpha_persisted_at": (
                                task.last_alpha_persisted_at.isoformat()
                                if task.last_alpha_persisted_at else None
                            ),
                            "cascade_phase": task.cascade_phase,
                            "cascade_round_idx": task.cascade_round_idx,
                        }
                    },
                    strategy_snapshot={},
                )
                db.add(run)
                await db.commit()
                await db.refresh(run)

                from backend.tasks import run_mining_task
                celery_task = run_mining_task.delay(task.id, run.id)
                run.celery_task_id = celery_task.id
                await db.commit()

                revived.append({
                    "task_id": task.id,
                    "region": task.region,
                    "celery_task_id": celery_task.id,
                })
                logger.warning(
                    f"[watchdog] revived dead session task={task.id} region={task.region} "
                    f"phase={task.cascade_phase} round={task.cascade_round_idx} "
                    f"(last_alpha_persisted_at={task.last_alpha_persisted_at})"
                )
            except Exception as e:
                logger.error(f"[watchdog] revive failed for task={task.id}: {e}")
                try:
                    await db.rollback()
                except Exception:
                    pass

    if revived:
        logger.info(f"[watchdog] revived {len(revived)} dead session(s): {revived}")
    return {"revived_count": len(revived), "revived": revived}


# ---------------------------------------------------------------------------
# 2. BRAIN quota guard — pause sessions at 90% daily simulate count
# ---------------------------------------------------------------------------

@celery_app.task(name="backend.tasks.quota_guard_pause_at_threshold")
def quota_guard_pause_at_threshold():
    """Beat-scheduled. Pause CONTINUOUS_CASCADE sessions if today's alpha count
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
        # Count today's alphas (proxy for BRAIN simulate calls — every alpha
        # row corresponds to ~1 simulate. We don't track simulate calls
        # directly in DB; alpha INSERT is the closest signal.)
        cnt = (
            await db.execute(
                select(func.count(Alpha.id))
                .where(Alpha.created_at >= today_start)
            )
        ).scalar() or 0

        if cnt < threshold:
            return {
                "today_alpha_count": cnt,
                "threshold": threshold,
                "limit": limit,
                "paused_count": 0,
                "paused": [],
            }

        # Over threshold — pause all active CONTINUOUS_CASCADE sessions.
        active = (
            await db.execute(
                select(MiningTask)
                .where(MiningTask.mining_mode == "CONTINUOUS_CASCADE")
                .where(MiningTask.status == "RUNNING")
            )
        ).scalars().all()
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
                    "phase": task.cascade_phase,
                })
                logger.warning(
                    f"[quota_guard] PAUSED task={task.id} region={task.region} "
                    f"(today_alpha_count={cnt} >= {threshold})"
                )
            except Exception as e:
                logger.error(f"[quota_guard] failed to pause task={task.id}: {e}")
        if paused:
            await db.commit()

    return {
        "today_alpha_count": cnt,
        "threshold": threshold,
        "limit": limit,
        "paused_count": len(paused),
        "paused": paused,
    }
