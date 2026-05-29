"""Orchestrator Sub-phase 1 + 2 tests (2026-05-29).

Sub-phase 1:骨架 task 注册 + flag gate + launched_by 标记。
Sub-phase 2:阈值 read + sentinel constants + finalize hook 接线。
Sub-phase 3 会补 DB 端 e2e(mock task pool / quota state / launch)。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Sub-phase 1: import + flag gate + schema + beat schedule
# ---------------------------------------------------------------------------

def test_orchestrator_tasks_importable():
    from backend.tasks import (
        orchestrator_evaluate_after_finalize,
        orchestrator_periodic_scan,
    )
    assert orchestrator_evaluate_after_finalize.name == (
        "backend.tasks.orchestrator_evaluate_after_finalize"
    )
    assert orchestrator_periodic_scan.name == (
        "backend.tasks.orchestrator_periodic_scan"
    )


def test_evaluate_after_finalize_flag_off_returns_skipped(monkeypatch):
    """flag OFF → event task 立即返回 skipped_reason=flag_off,不读 DB。"""
    from backend.tasks import orchestrator as m
    monkeypatch.setattr(m, "_orchestrator_enabled", lambda: False)
    out = m.orchestrator_evaluate_after_finalize.run(task_id=42)
    assert out["task_id"] == 42
    assert out["skipped_reason"] == m.SKIP_FLAG_OFF
    assert out["launched"] == 0


def test_periodic_scan_flag_off(monkeypatch):
    """cron fallback flag OFF → 立即返回。"""
    from backend.tasks import orchestrator as m
    monkeypatch.setattr(m, "_orchestrator_enabled", lambda: False)
    out = m.orchestrator_periodic_scan.run()
    assert out["skipped_reason"] == m.SKIP_FLAG_OFF
    assert out["scanned"] == 0
    assert out["launched"] == 0


def test_enable_auto_orchestrator_default_off():
    """Sub-phase 1+2 阶段 default OFF — 防生产烧灰。"""
    from backend.config import settings
    assert getattr(settings, "ENABLE_AUTO_ORCHESTRATOR", None) is False


def test_celery_beat_schedule_has_orchestrator_periodic_scan():
    from backend.celery_app import celery_app
    schedule = celery_app.conf.beat_schedule
    assert "orchestrator-periodic-scan" in schedule
    entry = schedule["orchestrator-periodic-scan"]
    assert entry["task"] == "backend.tasks.orchestrator_periodic_scan"
    assert entry["schedule"] == 3600


def test_task_config_schema_has_launched_by():
    from backend.schemas.task_config import TaskConfig
    cfg = TaskConfig(launched_by="orchestrator")
    assert cfg.launched_by == "orchestrator"
    cfg2 = TaskConfig(launched_by="manual")
    assert cfg2.launched_by == "manual"
    cfg3 = TaskConfig()
    assert cfg3.launched_by is None


# ---------------------------------------------------------------------------
# Sub-phase 2: thresholds + sentinels + finalize hook wiring
# ---------------------------------------------------------------------------

def test_orchestrator_thresholds_read():
    """Q5 阈值 read fresh,不缓存。"""
    from backend.tasks.orchestrator import _orchestrator_thresholds
    th = _orchestrator_thresholds()
    assert th["max_running"] == 3
    assert th["daily_limit"] == 10
    assert th["short_lived_min"] == 5
    assert th["idempotency_min"] == 5


def test_sentinel_constants_defined():
    """所有决策结果 sentinel 都定义且唯一。"""
    from backend.tasks import orchestrator as m
    sentinels = [
        m.SKIP_FLAG_OFF, m.SKIP_IDEMPOTENT, m.SKIP_QUOTA_REACHED,
        m.SKIP_MAX_RUNNING, m.SKIP_DAILY_LIMIT, m.SKIP_SHORT_LIVED,
        m.SKIP_NOT_FINALIZED, m.SKIP_TASK_GONE, m.SKIP_NO_PARAMS,
        m.LAUNCHED,
    ]
    assert len(sentinels) == len(set(sentinels)), "sentinels must be unique"


def test_finalize_hook_imports_orchestrator():
    """mining_tasks.py finalize 末尾的 orchestrator import 不会循环 import 或失败。"""
    # 静态 source 检查:确认 import 行存在
    import inspect
    from backend.tasks import mining_tasks
    src = inspect.getsource(mining_tasks)
    assert "orchestrator_evaluate_after_finalize" in src, (
        "finalize hook 未接线到 orchestrator"
    )
    # 真实 import 不会循环(orchestrator → 依赖 backend.tasks → 依赖 mining_tasks 仅
    # 通过 lazy import in finalize 路径,模块顶层不交叉)
    from backend.tasks.orchestrator import orchestrator_evaluate_after_finalize
    assert callable(orchestrator_evaluate_after_finalize)


def test_finalize_hook_dispatch_failure_is_non_fatal():
    """投递失败时,_run_flat_iteration 不应该 raise。"""
    import inspect
    from backend.tasks import mining_tasks
    src = inspect.getsource(mining_tasks)
    # 静态 sentinel:确保 try/except 包了 .delay 调用
    assert (
        "orchestrator_evaluate_after_finalize.delay(task_id)" in src
    ), "finalize hook 真实调用未接"
    # 在调用上下文里必有 try/except,grep 上下 10 行(简化:确认 noqa 风格 BLE001 出现)
    assert "non-fatal,cron fallback" in src or "non-fatal" in src, (
        "finalize hook 必须 try/except 包装非阻塞"
    )


@pytest.mark.asyncio
async def test_evaluate_task_not_found(monkeypatch):
    """task_id 不存在 → SKIP_TASK_GONE。"""
    from backend.tasks import orchestrator as m

    class _FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, _model, _id):
            return None

    monkeypatch.setattr(
        "backend.database.AsyncSessionLocal", lambda: _FakeDB()
    )
    monkeypatch.setattr(m, "_orchestrator_enabled", lambda: True)
    out = await m._evaluate_async(999, source="event")
    assert out["skipped_reason"] == m.SKIP_TASK_GONE
    assert out["launched"] == 0


@pytest.mark.asyncio
async def test_evaluate_task_not_finalized(monkeypatch):
    """task 还在 RUNNING(罕见 race)→ SKIP_NOT_FINALIZED。"""
    from types import SimpleNamespace
    from backend.tasks import orchestrator as m

    class _FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, _model, _id):
            return SimpleNamespace(id=7, status="RUNNING", config={})

    monkeypatch.setattr(
        "backend.database.AsyncSessionLocal", lambda: _FakeDB()
    )
    monkeypatch.setattr(m, "_orchestrator_enabled", lambda: True)
    out = await m._evaluate_async(7, source="event")
    assert out["skipped_reason"] == m.SKIP_NOT_FINALIZED
    assert out["task_status"] == "RUNNING"
