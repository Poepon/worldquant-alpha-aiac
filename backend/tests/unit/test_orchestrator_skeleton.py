"""Orchestrator Sub-phase 1 skeleton tests (2026-05-29).

Sub-phase 1 范围:验证骨架 task 注册 + flag gate + launched_by 标记。
Sub-phase 2/3 接事件路径 / 规则引擎 / launch 决策时再补对应测试。
"""
from __future__ import annotations

import pytest


def test_orchestrator_tasks_importable():
    """两个 task 都在 backend.tasks 模块导出 + 名称正确。"""
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
    assert out["skipped_reason"] == "flag_off"
    assert out["launched"] == 0


def test_evaluate_after_finalize_flag_on_skeleton(monkeypatch):
    """flag ON 但 Sub-phase 1 骨架 → 返回 skeleton_not_implemented。
    Sub-phase 2 实现后此断言要更新。"""
    from backend.tasks import orchestrator as m
    monkeypatch.setattr(m, "_orchestrator_enabled", lambda: True)
    out = m.orchestrator_evaluate_after_finalize.run(task_id=42)
    assert out["task_id"] == 42
    assert out["skipped_reason"] == "skeleton_not_implemented"
    assert out["launched"] == 0


def test_periodic_scan_flag_off(monkeypatch):
    """cron fallback flag OFF → 立即返回。"""
    from backend.tasks import orchestrator as m
    monkeypatch.setattr(m, "_orchestrator_enabled", lambda: False)
    out = m.orchestrator_periodic_scan.run()
    assert out["skipped_reason"] == "flag_off"
    assert out["scanned"] == 0
    assert out["launched"] == 0


def test_periodic_scan_flag_on_skeleton(monkeypatch):
    """cron fallback flag ON 骨架 → skeleton_not_implemented。"""
    from backend.tasks import orchestrator as m
    monkeypatch.setattr(m, "_orchestrator_enabled", lambda: True)
    out = m.orchestrator_periodic_scan.run()
    assert out["skipped_reason"] == "skeleton_not_implemented"
    assert out["scanned"] == 0


def test_enable_auto_orchestrator_default_off():
    """Q1-Q7 DECIDED 后,Sub-phase 1 阶段 flag 必须 default OFF — 防生产烧灰。"""
    from backend.config import settings
    assert getattr(settings, "ENABLE_AUTO_ORCHESTRATOR", None) is False


def test_celery_beat_schedule_has_orchestrator_periodic_scan():
    """orchestrator-periodic-scan 注册在 beat schedule(Q7 cron fallback)。"""
    from backend.celery_app import celery_app
    schedule = celery_app.conf.beat_schedule
    assert "orchestrator-periodic-scan" in schedule
    entry = schedule["orchestrator-periodic-scan"]
    assert entry["task"] == "backend.tasks.orchestrator_periodic_scan"
    assert entry["schedule"] == 3600


def test_task_config_schema_has_launched_by():
    """task_config schema 必须接受 launched_by(Q6 DECIDED)。"""
    from backend.schemas.task_config import TaskConfig
    cfg = TaskConfig(launched_by="orchestrator")
    assert cfg.launched_by == "orchestrator"
    cfg2 = TaskConfig(launched_by="manual")
    assert cfg2.launched_by == "manual"
    cfg3 = TaskConfig()  # default
    assert cfg3.launched_by is None
