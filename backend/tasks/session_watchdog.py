"""V-19.7 Persistent Mining Service watchdog + BRAIN quota guard.

Two beat-driven Celery tasks:

1. `watchdog_revive_dead_sessions` (every 5 min)
   Detects DEAD-but-RUNNING tasks (worker crashed / hibernated mid-round)
   and re-dispatches them so mining keeps making progress without user
   intervention. Covers two task families:

   (a) V-19 CONTINUOUS_CASCADE sessions: dead = status=RUNNING AND
       last_alpha_persisted_at < NOW() - DEAD_THRESHOLD_MIN
   (b) V-22.9 (2026-05-13) DISCRETE tasks (AUTONOMOUS_TIER1/2/3,
       SPECIFIC, AUTO): dead = status=RUNNING AND latest_trace_step
       < NOW() - DEAD_THRESHOLD_MIN. Without this, a worker crash on a
       discrete T1 task leaves it RUNNING forever — user has to manually
       reset+restart. Tasks 528-535 in the V-22.6 spike all needed
       manual revive after worker restarts.

   Grace: skip tasks whose task.created_at > NOW() - GRACE_MIN
          (a fresh task may not have written its first trace_step yet — IX-6
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
from backend.models.task import TraceStep
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
        # --- (a) V-19 CONTINUOUS_CASCADE sessions ---
        stmt = (
            select(MiningTask)
            .where(MiningTask.mining_mode == "CONTINUOUS_CASCADE")
            .where(MiningTask.status == "RUNNING")
        )
        cascade_rows = (await db.execute(stmt)).scalars().all()
        for task in cascade_rows:
            # Skip fresh sessions (grace period) — they may not have persisted
            # their first alpha yet.
            if task.created_at and task.created_at > grace_cutoff:
                continue
            # Heartbeat fresh enough?
            if task.last_alpha_persisted_at and task.last_alpha_persisted_at > dead_cutoff:
                continue
            # Dead — re-dispatch
            await _redispatch_task(
                db, task, now,
                reason_payload={
                    "last_alpha_persisted_at": (
                        task.last_alpha_persisted_at.isoformat()
                        if task.last_alpha_persisted_at else None
                    ),
                    "cascade_phase": task.cascade_phase,
                    "cascade_round_idx": task.cascade_round_idx,
                    "kind": "CONTINUOUS_CASCADE",
                },
                revived=revived,
            )

        # --- (b) V-22.9 DISCRETE T1/T2/T3 tasks (non-cascade) ---
        # Discrete tasks don't update last_alpha_persisted_at; use latest
        # trace_step as the liveness signal instead.
        stmt = (
            select(MiningTask)
            .where(MiningTask.status == "RUNNING")
            .where(
                (MiningTask.mining_mode != "CONTINUOUS_CASCADE")
                | (MiningTask.mining_mode.is_(None))
            )
        )
        discrete_rows = (await db.execute(stmt)).scalars().all()
        for task in discrete_rows:
            if task.created_at and task.created_at > grace_cutoff:
                continue
            # Find latest trace_step for this task
            latest_trace_stmt = (
                select(func.max(TraceStep.created_at))
                .where(TraceStep.task_id == task.id)
            )
            latest_trace = (await db.execute(latest_trace_stmt)).scalar()
            if latest_trace and latest_trace > dead_cutoff:
                continue  # liveness fresh
            # Dead — re-dispatch
            await _redispatch_task(
                db, task, now,
                reason_payload={
                    "latest_trace_step_at": (
                        latest_trace.isoformat() if latest_trace else None
                    ),
                    "kind": "DISCRETE",
                    "agent_mode": task.agent_mode,
                    "dataset_strategy": task.dataset_strategy,
                },
                revived=revived,
            )

    if revived:
        logger.info(f"[watchdog] revived {len(revived)} dead task(s): {revived}")
    return {"revived_count": len(revived), "revived": revived}


async def _redispatch_task(db, task, now, *, reason_payload: dict, revived: list) -> None:
    """Common re-dispatch path used by both CONTINUOUS and DISCRETE handlers."""
    try:
        run = ExperimentRun(
            task_id=task.id,
            status="RUNNING",
            trigger_source="WATCHDOG_REVIVE",
            celery_task_id=None,
            config_snapshot={
                "watchdog_revive": {"at": now.isoformat(), **reason_payload}
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
            "kind": reason_payload.get("kind", "?"),
        })
        logger.warning(
            f"[watchdog] revived task={task.id} kind={reason_payload.get('kind')} "
            f"region={task.region} (payload={reason_payload})"
        )
    except Exception as e:
        logger.error(f"[watchdog] revive failed for task={task.id}: {e}")
        try:
            await db.rollback()
        except Exception:
            pass


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
