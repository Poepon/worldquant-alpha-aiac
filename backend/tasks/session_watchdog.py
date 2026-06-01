"""V-19.7 Persistent Mining Service watchdog + BRAIN quota guard.

Two beat-driven Celery tasks:

1. `watchdog_revive_dead_sessions` (every 5 min)
   Detects DEAD-but-RUNNING tasks (worker crashed / hibernated mid-round)
   and re-dispatches them so mining keeps making progress without user
   intervention. Covers DISCRETE + FLAT tasks: dead = status=RUNNING AND
   latest_trace_step < NOW() - DEAD_THRESHOLD_MIN. Without this, a worker
   crash leaves a task RUNNING forever — user has to manually reset+restart
   (the V-22.6 spike tasks 528-535 all needed manual revive after restarts).
   Cascade revive was retired with the tier system (2026-05-19); cascade
   tasks can no longer be created, so there is nothing to revive there.

   Grace: skip tasks whose task.created_at > NOW() - GRACE_MIN
          (a fresh task may not have written its first trace_step yet — IX-6
          mitigation against false-positive at start-up).

2. `quota_guard_pause_at_threshold` (every 10 min)
   Counts today's alpha rows (UTC date). When >= 90% of BRAIN_DAILY_SIMULATE_LIMIT,
   pauses every active (RUNNING) session as a defensive measure.
   Logs a clear reason; user can resume next day or after raising the limit.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select, update, func

from backend.celery_app import celery_app
from backend.config import settings
from backend.database import AsyncSessionLocal
from backend.models import Alpha, AlphaFailure, ExperimentRun, MiningTask
from backend.models.task import TraceStep
from backend.tasks import run_async


def _is_cascade_schedule(task) -> bool:
    """Cascade detection via task.schedule (mining_mode column dropped)."""
    sched = getattr(task, "schedule", None) or ""
    return sched.upper() == "CASCADE"


# ---------------------------------------------------------------------------
# 1. Watchdog — revive dead sessions
# ---------------------------------------------------------------------------

@celery_app.task(name="backend.tasks.watchdog_revive_dead_sessions")
def watchdog_revive_dead_sessions():
    """Beat-scheduled. Re-dispatch celery worker for dead-but-RUNNING sessions."""
    return run_async(_watchdog_revive_async())


def _discrete_task_is_dead(latest_trace, worker_alive: bool, dead_cutoff) -> bool:
    """Decide whether a RUNNING discrete/FLAT task should be re-dispatched.

    - own trace fresh (latest_trace > dead_cutoff)            → alive, NOT dead
    - NO trace ever + a worker is progressing (worker_alive)  → QUEUED on a busy
      (solo) worker, hasn't started → NOT dead (2026-06-01 batch-ONESHOT
      false-revive fix: don't spam phantom Runs for tasks waiting their turn)
    - NO trace ever + worker looks dead (no trace anywhere)   → stuck / lost
      dispatch → DEAD (preserves recovery)
    - trace stale (<= dead_cutoff)                            → stalled → DEAD
    """
    if latest_trace is not None and latest_trace > dead_cutoff:
        return False
    if latest_trace is None and worker_alive:
        return False
    return True


async def _watchdog_revive_async() -> dict:
    dead_threshold_min = getattr(settings, "CASCADE_WATCHDOG_DEAD_MIN", 15)
    grace_min = getattr(settings, "CASCADE_WATCHDOG_GRACE_MIN", 15)
    now = datetime.now(timezone.utc)
    dead_cutoff = now - timedelta(minutes=dead_threshold_min)
    grace_cutoff = now - timedelta(minutes=grace_min)

    revived = []
    async with AsyncSessionLocal() as db:
        # Cascade revive retired (tier-system removal, 2026-05-19): cascade
        # tasks can no longer be created (mining_session router deleted +
        # run_mining_task refuses CASCADE with FAILED), so there is nothing to
        # revive. Only the DISCRETE / FLAT liveness probe below runs.
        #
        # DISCRETE / FLAT tasks don't update last_alpha_persisted_at every
        # round; use the latest trace_step as the liveness signal instead.
        stmt = select(MiningTask).where(MiningTask.status == "RUNNING")
        discrete_rows = (await db.execute(stmt)).scalars().all()

        # 2026-06-01: global liveness signal. A RUNNING task with NO trace step
        # ever hasn't STARTED executing — it's QUEUED (e.g. batch-dispatched
        # behind others on the solo worker), not a stalled session. Reviving it
        # just adds a redundant queue entry while a worker is alive. Only revive
        # such never-started tasks when the worker looks DEAD globally (no trace
        # ANYWHERE within the dead window — then a queued task is genuinely stuck
        # / its dispatch was lost). If some task IS progressing, queued no-trace
        # tasks will run when their turn comes → don't spam phantom revives.
        global_latest_trace = (
            await db.execute(select(func.max(TraceStep.created_at)))
        ).scalar()
        worker_alive = bool(global_latest_trace and global_latest_trace > dead_cutoff)

        for task in discrete_rows:
            if _is_cascade_schedule(task):
                # cascade is retired — skip (cascade loop above is no-op anyway)
                continue
            if task.created_at and task.created_at > grace_cutoff:
                continue
            # Find latest trace_step for this task
            latest_trace_stmt = (
                select(func.max(TraceStep.created_at))
                .where(TraceStep.task_id == task.id)
            )
            latest_trace = (await db.execute(latest_trace_stmt)).scalar()
            if not _discrete_task_is_dead(latest_trace, worker_alive, dead_cutoff):
                continue  # fresh / queued-not-started → not dead
            # V-27.2: skip if already revived within the dead-threshold
            # window — discrete revive holds no lock, so this is the only
            # guard against the watchdog double-dispatching the task.
            if await _recently_revived(db, task.id, dead_cutoff):
                continue
            # Dead — re-dispatch
            await _redispatch_task(
                db, task, now,
                reason_payload={
                    "latest_trace_step_at": (
                        latest_trace.isoformat() if latest_trace else None
                    ),
                    "kind": "DISCRETE",
                    "schedule": getattr(task, "schedule", None),
                    "dataset_strategy": task.dataset_strategy,
                },
                revived=revived,
            )

    if revived:
        logger.info(f"[watchdog] revived {len(revived)} dead task(s): {revived}")
    return {"revived_count": len(revived), "revived": revived}


async def _recently_revived(db, task_id: int, cutoff: datetime) -> bool:
    """True if a WATCHDOG_REVIVE run for this task started after `cutoff`.

    V-27.2: the watchdog runs every 5 min but the dead-threshold is 15 min.
    Without this guard, a task revived on tick N is still "dead" on ticks
    N+1 / N+2 (the freshly-dispatched worker hasn't persisted an alpha or
    written a trace_step yet) — so the watchdog re-dispatches it AGAIN and
    two workers end up on one task. Discrete revive in particular acquires
    no lock, so nothing else catches the double-dispatch. Skipping a task
    whose last WATCHDOG_REVIVE is within the dead-threshold gives the
    revived worker that full window to come alive.
    """
    last = (
        await db.execute(
            select(func.max(ExperimentRun.started_at))
            .where(ExperimentRun.task_id == task_id)
            .where(ExperimentRun.trigger_source == "WATCHDOG_REVIVE")
        )
    ).scalar()
    if last is None:
        return False
    # ExperimentRun.started_at is a naive DateTime column (models/task.py —
    # no timezone=True), so asyncpg returns it tz-naive; `cutoff` is derived
    # from datetime.now(timezone.utc) and is tz-aware. Comparing the two
    # directly raises TypeError. Normalise the DB value to UTC-aware.
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return last > cutoff


async def _redispatch_task(db, task, now, *, reason_payload: dict, revived: list) -> None:
    """Common re-dispatch path used by both CONTINUOUS and DISCRETE handlers."""
    try:
        # Discrete/flat revive: no lock takeover. Cascade lock handling was
        # removed with cascade retirement — discrete tasks acquire no lock and
        # flat tasks re-acquire fresh on dispatch.

        # V-26.33 (2026-05-13): inherit the dead ExperimentRun's config /
        # strategy snapshots so the revival preserves audit lineage. Pre-fix
        # the new Run started with empty dicts, dropping things like the
        # cascade variant tag and the experiment_variant from the original
        # dispatch — making "why did revive change behaviour?" analysis
        # impossible. Falls back to the previous empty dicts if no prior
        # Run exists.
        prior_run = None
        try:
            prior_run = (
                await db.execute(
                    select(ExperimentRun)
                    .where(ExperimentRun.task_id == task.id)
                    .order_by(ExperimentRun.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        except Exception as _e:
            logger.debug(f"[watchdog] V-26.33 prior-run lookup failed: {_e}")
        inherited_config = (
            dict(prior_run.config_snapshot) if (prior_run and isinstance(prior_run.config_snapshot, dict)) else {}
        )
        inherited_config["watchdog_revive"] = {
            "at": now.isoformat(), **reason_payload
        }
        if prior_run is not None:
            inherited_config["watchdog_revive"]["prior_run_id"] = prior_run.id
        inherited_strategy = (
            dict(prior_run.strategy_snapshot) if (prior_run and isinstance(prior_run.strategy_snapshot, dict)) else {}
        )

        # Phase 1.5-C [V1.2-B3] (2026-05-18): inherit scheduling state from the
        # prior run's runtime_state. Without this, every watchdog revive creates
        # a run with runtime_state={}, and a revived FLAT session restarts at
        # flat_cursor=0 — losing the dataset-iteration progress accumulated
        # since the last successful round. progress / iteration /
        # last_persisted_at intentionally NOT inherited — heartbeat repopulates
        # them on the first successful round of the new run; inheriting stale
        # values risks the watchdog mis-judging liveness.
        prior_runtime_state: dict = {}
        if prior_run is not None and isinstance(prior_run.runtime_state, dict):
            prior_runtime_state = prior_run.runtime_state
        # round_idx + flat_cursor are the only live scheduling keys
        # (current_tier was dropped with the tier system).
        inherited_runtime_state = {
            "round_idx": prior_runtime_state.get("round_idx", 0),
        }
        if "flat_cursor" in prior_runtime_state:
            inherited_runtime_state["flat_cursor"] = prior_runtime_state["flat_cursor"]

        run = ExperimentRun(
            task_id=task.id,
            status="RUNNING",
            trigger_source="WATCHDOG_REVIVE",
            celery_task_id=None,
            config_snapshot=inherited_config,
            strategy_snapshot=inherited_strategy,
            runtime_state=inherited_runtime_state,
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
