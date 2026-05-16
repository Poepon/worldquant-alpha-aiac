"""BRAIN role-switch helpers shared across Celery tasks.

Extracted from sync_tasks.py so refresh_tasks.py + future role-aware tasks
can reuse without circular import.

See docs/ plan §5.2 (P3-Brain).
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import MiningTask


async def read_role_snapshot(task_id: Optional[int], db: AsyncSession) -> dict:
    """Read MiningTask.config["brain_role_snapshot"] for the given task_id.

    Consultant 切换不影响已在跑的 task (R2-M3/M4 承诺):sync/refresh 后台路径
    必须按 alpha.task_id 反查 snapshot,不能直接读 settings 当前值。返回 {} 当
    task_id 为 None (legacy alpha,pre-v5) / task 不存在 / 无 snapshot — 此时
    fallback sharpe_submit_min_override=None → tier_thresholds 走当前 settings。

    Legacy 一次性副作用:切到 Consultant 的瞬间,task_id=None 的 legacy alpha
    在 sync 阶段会用 1.58 阈值判旧 PASS → 可能批量 PASS→PASS_PROVISIONAL 降级。
    建议 ops 在切换前先跑一次 sync 把 legacy alpha task_id 回填,或接受此一次性降级。
    """
    if task_id is None:
        return {}
    task = (await db.execute(
        select(MiningTask).where(MiningTask.id == task_id)
    )).scalar_one_or_none()
    if task is None or not isinstance(task.config, dict):
        return {}
    return task.config.get("brain_role_snapshot") or {}
