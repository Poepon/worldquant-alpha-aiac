"""Mining Orchestrator — Sub-phase 1 + 2 (2026-05-29).

依据 docs/orchestrator_plan_2026-05-29.md Q1-Q7 决策:
- Q1: celery beat 注册 task
- Q2: 事件驱动主路径 (orchestrator_evaluate_after_finalize) + cron 1h fallback
      (orchestrator_periodic_scan)
- Q5: 保守阈值 max_running=3 / daily=10 / backoff=2h / short_lived=5min
- Q6: launched_by 标记 — orchestrator 只让位自己启的 task,manual 不动
- Q7: 主路径事件 + cron fallback 防丢事件

Sub-phase 2 范围(2026-05-29 晚):
- 事件投递端:`_run_flat_iteration` finalize 末尾投递 (mining_tasks.py)
- 消费端:idempotency + 决策上下文(pool/quota/daily/短命/launched_by)+ 规则
- 真实 launch wire 到 `TaskService.start_flat_session(launched_by="orchestrator")`
- Sub-phase 3 会改 region/dataset 选择算法(目前用最近成功 task 的 region)

防生产烧灰:flag OFF default → 立即返回。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select, func, and_, or_

from backend.celery_app import celery_app
from backend.tasks import run_async

logger = logging.getLogger("tasks.orchestrator")

# Sentinel values for decision results
SKIP_FLAG_OFF = "flag_off"
SKIP_IDEMPOTENT = "idempotent_recent"
SKIP_QUOTA_REACHED = "quota_threshold_reached"
SKIP_MAX_RUNNING = "max_running_reached"
SKIP_DAILY_LIMIT = "daily_limit_reached"
SKIP_SHORT_LIVED = "short_lived_task"
SKIP_NOT_FINALIZED = "task_not_finalized"
SKIP_TASK_GONE = "task_not_found"
SKIP_NO_PARAMS = "no_default_launch_params"
LAUNCHED = "launched"


def _orchestrator_enabled() -> bool:
    """Single source of truth for whether orchestrator should act."""
    try:
        from backend.config import settings
    except Exception as ex:  # noqa: BLE001
        logger.error(f"[orchestrator] settings import failed: {ex}")
        return False
    return bool(getattr(settings, "ENABLE_AUTO_ORCHESTRATOR", False))


def _orchestrator_thresholds() -> Dict[str, int]:
    """Q5 DECIDED thresholds — read fresh each call (no caching)."""
    from backend.config import settings
    return {
        "max_running": int(getattr(settings, "ORCHESTRATOR_MAX_RUNNING_TASKS", 3)),
        "daily_limit": int(getattr(settings, "ORCHESTRATOR_DAILY_LAUNCH_LIMIT", 10)),
        "short_lived_min": int(getattr(settings, "ORCHESTRATOR_SHORT_LIVED_MIN", 5)),
        "idempotency_min": int(getattr(settings, "ORCHESTRATOR_IDEMPOTENCY_MIN", 5)),
    }


# ---------------------------------------------------------------------------
# Celery tasks
# ---------------------------------------------------------------------------

@celery_app.task(name="backend.tasks.orchestrator_evaluate_after_finalize")
def orchestrator_evaluate_after_finalize(task_id: int) -> Dict[str, Any]:
    """事件驱动主路径(Q2 DECIDED).

    被 `_run_flat_iteration` finalize 末尾投递。读 task 终态 + 当前 RUNNING/
    PAUSED pool + 配额状态 + 历史 PASS rate → 决策是否 launch 下一个。
    """
    if not _orchestrator_enabled():
        return {
            "task_id": task_id,
            "skipped_reason": SKIP_FLAG_OFF,
            "launched": 0,
        }
    return run_async(_evaluate_async(task_id, source="event"))


@celery_app.task(name="backend.tasks.orchestrator_periodic_scan")
def orchestrator_periodic_scan() -> Dict[str, Any]:
    """cron 1h fallback (Q7 DECIDED).

    扫描最近 finalized 但 orchestrator_processed_at 未标的 task,补丢事件。
    """
    if not _orchestrator_enabled():
        return {
            "skipped_reason": SKIP_FLAG_OFF,
            "scanned": 0,
            "launched": 0,
        }
    return run_async(_scan_async())


# ---------------------------------------------------------------------------
# Core async logic
# ---------------------------------------------------------------------------

async def _evaluate_async(task_id: int, *, source: str) -> Dict[str, Any]:
    """单 task 触发的评估 — 是否要 launch 下一个 task?

    Args:
        task_id: 触发评估的 task(刚 finalize 的)
        source: "event" / "cron_fallback" — 仅用于 log
    """
    from backend.database import AsyncSessionLocal
    from backend.models import MiningTask

    th = _orchestrator_thresholds()

    async with AsyncSessionLocal() as db:
        # 1. 读 task,确认 finalized
        task = await db.get(MiningTask, task_id)
        if task is None:
            return {
                "task_id": task_id,
                "skipped_reason": SKIP_TASK_GONE,
                "launched": 0,
                "source": source,
            }
        if task.status not in ("COMPLETED", "STOPPED", "PAUSED", "EARLY_STOPPED"):
            # 事件可能在 finalize 投递前到达(罕见 race),或 watchdog 复活后状态变
            return {
                "task_id": task_id,
                "skipped_reason": SKIP_NOT_FINALIZED,
                "task_status": task.status,
                "launched": 0,
                "source": source,
            }

        # 2. Idempotency:防双发(事件 + cron fallback 同时触发)
        cfg = (task.config or {}) if isinstance(task.config, dict) else {}
        processed_at = cfg.get("orchestrator_processed_at")
        if processed_at:
            try:
                dt = datetime.fromisoformat(processed_at)
                age_min = (datetime.utcnow() - dt).total_seconds() / 60.0
                if age_min < th["idempotency_min"]:
                    return {
                        "task_id": task_id,
                        "skipped_reason": SKIP_IDEMPOTENT,
                        "age_min": round(age_min, 1),
                        "launched": 0,
                        "source": source,
                    }
            except Exception:  # noqa: BLE001
                pass  # 坏的时间戳,继续处理

        # 3. 决策上下文
        running_count = await _count_orchestrator_running(db)
        today_launches = await _count_today_orchestrator_launches(db)
        quota_state = await _read_quota_state()
        finalize_age_min = _finalize_age_minutes(task)
        is_short_lived = (
            finalize_age_min is not None
            and finalize_age_min <= th["short_lived_min"]
            and (task.progress_current or 0) == 0
        )

        # 4. 规则:从严到宽,任一不满足即 skip
        if quota_state.get("over_threshold"):
            return await _stamp_async(
                db, task, source,
                {"skipped_reason": SKIP_QUOTA_REACHED, "quota": quota_state},
            )
        if running_count >= th["max_running"]:
            return await _stamp_async(
                db, task, source,
                {
                    "skipped_reason": SKIP_MAX_RUNNING,
                    "running_count": running_count,
                    "max_running": th["max_running"],
                },
            )
        if today_launches >= th["daily_limit"]:
            return await _stamp_async(
                db, task, source,
                {
                    "skipped_reason": SKIP_DAILY_LIMIT,
                    "today_launches": today_launches,
                    "daily_limit": th["daily_limit"],
                },
            )
        if is_short_lived:
            # 短命 task 不当让位事件 — 防一个 5 秒挂的 task 触发 launch storm
            return await _stamp_async(
                db, task, source,
                {
                    "skipped_reason": SKIP_SHORT_LIVED,
                    "finalize_age_min": finalize_age_min,
                    "progress_current": task.progress_current,
                },
            )

        # 5. 选 launch 参数(Sub-phase 3 改进:历史 PASS rate EMA 加权)
        params = await _select_launch_params(db, task)
        if params is None:
            return await _stamp_async(
                db, task, source,
                {"skipped_reason": SKIP_NO_PARAMS},
            )

        # 6. 真实 launch
        new_task_info = await _launch_next_task(db, params)
        return await _stamp_async(
            db, task, source,
            {
                "skipped_reason": None,
                "launched": 1,
                "new_task_id": new_task_info.get("task_id"),
                "params": params,
            },
        )


async def _stamp_async(db, task, source: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Stamp orchestrator_processed_at into task.config + commit."""
    from sqlalchemy.orm.attributes import flag_modified

    try:
        if task.config is None:
            task.config = {}
        task.config["orchestrator_processed_at"] = datetime.utcnow().isoformat()
        task.config["orchestrator_processed_source"] = source
        flag_modified(task, "config")
        await db.commit()
    except Exception as ex:  # noqa: BLE001
        logger.warning(f"[orchestrator] stamp commit failed (non-fatal): {ex}")
        try:
            await db.rollback()
        except Exception:
            pass

    out = {
        "task_id": task.id,
        "launched": result.get("launched", 0),
        "source": source,
        **{k: v for k, v in result.items() if k != "launched"},
    }
    logger.info(f"[orchestrator] eval task={task.id} {out}")
    return out


async def _scan_async() -> Dict[str, Any]:
    """cron 1h fallback — find finalized tasks missing orchestrator_processed_at."""
    from backend.database import AsyncSessionLocal
    from backend.models import MiningTask

    th = _orchestrator_thresholds()
    cutoff = datetime.utcnow() - timedelta(hours=24)
    scanned = 0
    launched = 0
    results: List[Dict[str, Any]] = []

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(MiningTask.id).where(
                MiningTask.status.in_(("COMPLETED", "STOPPED", "PAUSED", "EARLY_STOPPED")),
                or_(MiningTask.modified_at >= cutoff, MiningTask.created_at >= cutoff),
            ).limit(100)
        )).scalars().all()

    for tid in rows:
        scanned += 1
        out = await _evaluate_async(tid, source="cron_fallback")
        if out.get("launched"):
            launched += int(out["launched"])
        results.append(out)
        # 即使一次性扫到 100,daily_limit 兜底防 launch storm

    return {"scanned": scanned, "launched": launched, "results_count": len(results)}


# ---------------------------------------------------------------------------
# Decision context helpers
# ---------------------------------------------------------------------------

async def _count_orchestrator_running(db) -> int:
    """orchestrator launch 的 RUNNING/PAUSED task 数。"""
    from backend.models import MiningTask
    # SQLAlchemy JSONB ->> 'launched_by' 跨方言查询用 cast,SQLite/PG 兼容
    # 简化:Python-side 过滤(数量少,可接受)
    rows = (await db.execute(
        select(MiningTask).where(MiningTask.status.in_(("RUNNING", "PAUSED")))
    )).scalars().all()
    return sum(
        1 for t in rows
        if isinstance(t.config, dict) and t.config.get("launched_by") == "orchestrator"
    )


async def _count_today_orchestrator_launches(db) -> int:
    """今天(UTC date)orchestrator launch 的 task 数。"""
    from backend.models import MiningTask
    today_start = datetime(
        datetime.utcnow().year, datetime.utcnow().month, datetime.utcnow().day
    )
    rows = (await db.execute(
        select(MiningTask).where(MiningTask.created_at >= today_start)
    )).scalars().all()
    return sum(
        1 for t in rows
        if isinstance(t.config, dict) and t.config.get("launched_by") == "orchestrator"
    )


async def _read_quota_state() -> Dict[str, Any]:
    """读 quota_guard 当前累计 + threshold,返回是否 over。"""
    try:
        from backend.tasks.session_watchdog import _quota_guard_async
        # quota_guard 本身就是判 + PAUSE,我们只读 count + threshold
        # 简单复用:运行一次轻量版本
        from backend.config import settings
        from backend.database import AsyncSessionLocal
        from backend.models import Alpha, AlphaFailure

        limit = int(getattr(settings, "BRAIN_DAILY_SIMULATE_LIMIT", 1000))
        pct = float(getattr(settings, "BRAIN_QUOTA_PAUSE_PCT", 0.9))
        threshold = int(limit * pct)
        today_start = datetime(
            datetime.utcnow().year, datetime.utcnow().month, datetime.utcnow().day
        )
        async with AsyncSessionLocal() as db:
            a = (await db.execute(
                select(func.count(Alpha.id)).where(
                    Alpha.created_at >= today_start, Alpha.task_id.isnot(None)
                )
            )).scalar() or 0
            f = (await db.execute(
                select(func.count(AlphaFailure.id)).where(
                    AlphaFailure.created_at >= today_start,
                    ~func.coalesce(AlphaFailure.error_type, "").in_(
                        ["PRESIM_SKIP", "DEDUP_SKIP"]
                    ),
                )
            )).scalar() or 0
        cnt = a + f
        return {
            "today_count": cnt,
            "threshold": threshold,
            "limit": limit,
            "over_threshold": cnt >= threshold,
        }
    except Exception as ex:  # noqa: BLE001
        logger.warning(f"[orchestrator] quota state read failed (non-fatal): {ex}")
        return {"over_threshold": False, "error": str(ex)[:200]}


def _finalize_age_minutes(task) -> Optional[float]:
    """Task finalized 到现在的分钟数。"""
    # MiningTask 没有 finished_at 列;用 created_at 作 lower bound 估算
    # — 不准但用于"短命"检测足够(短命定义是 task 总时长 ≤ 5min + 0 alpha)。
    created = getattr(task, "created_at", None)
    if created is None:
        return None
    try:
        return (datetime.utcnow() - created).total_seconds() / 60.0
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Launch param selection + actual launch
# ---------------------------------------------------------------------------

async def _select_launch_params(db, finalized_task) -> Optional[Dict[str, Any]]:
    """选下一个 task 的参数(Sub-phase 2 占位:复用刚 finalize 的 region;
    Sub-phase 3 改成历史 PASS rate EMA 加权选 region/dataset)。
    """
    region = getattr(finalized_task, "region", None)
    universe = getattr(finalized_task, "universe", None)
    if not region or not universe:
        return None
    # 默认 AUTO datasets(让 _get_datasets_to_mine 选),delay=1(established)
    return {
        "region": region,
        "universe": universe,
        "datasets": [],
        "delay": 1,
    }


async def _launch_next_task(db, params: Dict[str, Any]) -> Dict[str, Any]:
    """call TaskService.start_flat_session(launched_by="orchestrator")."""
    from backend.services.task_service import TaskService

    svc = TaskService(db)
    info = await svc.start_flat_session(
        region=params["region"],
        universe=params["universe"],
        datasets=params.get("datasets") or [],
        delay=int(params.get("delay", 1)),
        enable_pipeline=True,   # orchestrator 启的 task 默认走流水线
        launched_by="orchestrator",
    )
    return {"task_id": getattr(info, "task_id", None)}


__all__ = [
    "orchestrator_evaluate_after_finalize",
    "orchestrator_periodic_scan",
    "_orchestrator_enabled",
    "_orchestrator_thresholds",
]
