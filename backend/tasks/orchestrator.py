"""Mining Orchestrator — Sub-phase 1 骨架 (2026-05-29).

依据 docs/orchestrator_plan_2026-05-29.md Q1-Q7 决策:
- Q1: 跑在 celery beat(本 module 注册 task)
- Q2: 事件驱动主路径 (orchestrator_evaluate_after_finalize) + cron 1h fallback
      (orchestrator_periodic_scan)
- Q6: task.config["launched_by"] 标记区分 manual / orchestrator;orchestrator
      只动自己启的 task

Sub-phase 1 范围:仅骨架。两个 celery task 都注册了但 short-circuit
(ENABLE_AUTO_ORCHESTRATOR default OFF),Sub-phase 2 接 finalize hook,
Sub-phase 3 接规则引擎。

防生产烧灰:
- flag OFF 默认 → 任何调用立即 return early,无副作用
- 事件 task 即使被 finalize 投递 → flag 检查 → no-op
- cron fallback 同样 flag 检查
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from backend.celery_app import celery_app

logger = logging.getLogger("tasks.orchestrator")


def _orchestrator_enabled() -> bool:
    """Single source of truth for whether orchestrator should act.

    Sub-phase 1 骨架阶段 default OFF;Sub-phase 2/3 ship 完毕 + Phase B soak
    通过后,operator 翻转 flag 生效。
    """
    try:
        from backend.config import settings
    except Exception as ex:  # noqa: BLE001
        logger.error(f"[orchestrator] settings import failed: {ex}")
        return False
    return bool(getattr(settings, "ENABLE_AUTO_ORCHESTRATOR", False))


@celery_app.task(name="backend.tasks.orchestrator_evaluate_after_finalize")
def orchestrator_evaluate_after_finalize(task_id: int) -> Dict[str, Any]:
    """事件驱动主路径(Q2 DECIDED).

    被 ``_run_flat_iteration`` finalize 末尾投递(Sub-phase 2 接线)。读当前
    task 终态 + RUNNING/PAUSED pool + 配额状态 + 历史 PASS rate,决定是否
    launch 下一个 task(自己的 launched_by="orchestrator" 标记,见 Q6)。

    Sub-phase 1 范围:flag OFF 立即返回,不读 DB / 不算配额 / 不 launch。

    Idempotency: 消费端会去重(同 task_id 5min 内不重复处理),Sub-phase 2 加。
    """
    if not _orchestrator_enabled():
        return {
            "task_id": task_id,
            "skipped_reason": "flag_off",
            "launched": 0,
        }

    # Sub-phase 2/3 实现规则评估 + launch 决策
    logger.info(
        f"[orchestrator] task={task_id} finalize event received (skeleton — "
        f"Sub-phase 2 将实现 evaluate)"
    )
    return {
        "task_id": task_id,
        "skipped_reason": "skeleton_not_implemented",
        "launched": 0,
    }


@celery_app.task(name="backend.tasks.orchestrator_periodic_scan")
def orchestrator_periodic_scan() -> Dict[str, Any]:
    """cron 1h fallback (Q7 DECIDED).

    防丢事件兜底:每小时扫描全 task pool + 配额 + 历史 PASS rate,补 launch
    决策。覆盖 worker 重启吞事件 / 投递失败 / 边界 case。

    Sub-phase 1 范围:flag OFF 立即返回。
    """
    if not _orchestrator_enabled():
        return {
            "skipped_reason": "flag_off",
            "scanned": 0,
            "launched": 0,
        }

    logger.info(
        "[orchestrator] periodic scan tick (skeleton — Sub-phase 2 将实现 scan)"
    )
    return {
        "skipped_reason": "skeleton_not_implemented",
        "scanned": 0,
        "launched": 0,
    }


__all__ = [
    "orchestrator_evaluate_after_finalize",
    "orchestrator_periodic_scan",
]
