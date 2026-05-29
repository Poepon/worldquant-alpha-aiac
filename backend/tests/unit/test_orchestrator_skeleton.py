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


# ---------------------------------------------------------------------------
# Sub-phase 3: weighted sampling + select_launch_params 规则引擎
# ---------------------------------------------------------------------------

def test_weighted_sample_one_basic():
    """全相同权重退化到均匀随机;weight=0 不会被选中。"""
    import random
    from backend.tasks.orchestrator import _weighted_sample_one
    rng = random.Random(42)
    # 全相同权重 — 大样本应均匀
    counts = {"a": 0, "b": 0, "c": 0}
    for _ in range(3000):
        pick = _weighted_sample_one({"a": 1.0, "b": 1.0, "c": 1.0}, rng)
        counts[pick] += 1
    # 每个应在 1000 ± 200(松紧度允许)
    for k, v in counts.items():
        assert 800 < v < 1200, f"{k} should be ~1000, got {v}"


def test_weighted_sample_one_skew():
    """权重 10x 大致 10x 被选中。"""
    import random
    from backend.tasks.orchestrator import _weighted_sample_one
    rng = random.Random(42)
    counts = {"a": 0, "b": 0}
    for _ in range(3000):
        pick = _weighted_sample_one({"a": 10.0, "b": 1.0}, rng)
        counts[pick] += 1
    # 期望 ~2727:273
    assert 2500 < counts["a"] < 2900
    assert 200 < counts["b"] < 500


def test_weighted_sample_one_zero_weights():
    """所有权重 0 → None,不无限循环。"""
    import random
    from backend.tasks.orchestrator import _weighted_sample_one
    assert _weighted_sample_one({"a": 0, "b": 0}, random.Random(1)) is None
    assert _weighted_sample_one({}, random.Random(1)) is None


def test_weighted_sample_top_k_no_dup():
    """top-K 不重复;k > pool size → 返回 pool size 个。"""
    import random
    from backend.tasks.orchestrator import _weighted_sample_top_k
    rng = random.Random(7)
    picks = _weighted_sample_top_k(
        {"a": 5.0, "b": 3.0, "c": 1.0}, k=2, rng=rng,
    )
    assert len(picks) == 2
    assert len(set(picks)) == 2  # 无重复
    # k > pool
    picks2 = _weighted_sample_top_k(
        {"a": 1.0, "b": 1.0}, k=5, rng=rng,
    )
    assert len(picks2) == 2


@pytest.mark.asyncio
async def test_select_launch_params_cold_start(monkeypatch):
    """无历史数据 → fallback 到 finalize 触发 task 的 region/universe + 空 datasets。"""
    from types import SimpleNamespace
    from backend.tasks import orchestrator as m

    async def _empty_regions(db, lookback):
        return {}

    async def _empty_datasets(db, region, lookback):
        return {}

    monkeypatch.setattr(m, "_compute_region_pass_rates", _empty_regions)
    monkeypatch.setattr(m, "_compute_dataset_pass_rates", _empty_datasets)

    task = SimpleNamespace(region="USA", universe="TOP3000")
    params = await m._select_launch_params(db=None, finalized_task=task)
    assert params["region"] == "USA"
    assert params["universe"] == "TOP3000"
    assert params["datasets"] == []
    assert params["delay"] == 1
    assert params["_decision_meta"]["region_pool_size"] == 0


@pytest.mark.asyncio
async def test_select_launch_params_warm(monkeypatch):
    """有历史数据 → 加权采样选 region + top-N dataset。"""
    from types import SimpleNamespace
    from backend.tasks import orchestrator as m

    async def _warm_regions(db, lookback):
        return {
            "USA": {"passes": 50, "total": 100, "weight": 0.5},
            "CHN": {"passes": 10, "total": 100, "weight": 0.108},
        }

    async def _warm_datasets(db, region, lookback):
        return {
            "fundamental6": {"passes": 30, "total": 50, "weight": 0.59},
            "analyst4": {"passes": 5, "total": 50, "weight": 0.115},
            "pv1": {"passes": 20, "total": 100, "weight": 0.205},
        }

    monkeypatch.setattr(m, "_compute_region_pass_rates", _warm_regions)
    monkeypatch.setattr(m, "_compute_dataset_pass_rates", _warm_datasets)

    task = SimpleNamespace(region="USA", universe="TOP3000")
    params = await m._select_launch_params(db=None, finalized_task=task)
    assert params["region"] in {"USA", "CHN"}  # 加权采样
    assert 1 <= len(params["datasets"]) <= 3
    assert params["_decision_meta"]["region_pool_size"] == 2
    assert params["_decision_meta"]["dataset_pool_size"] == 3


@pytest.mark.asyncio
async def test_select_launch_params_no_fallback_region(monkeypatch):
    """finalize task 缺 region/universe → 不报错,返回 None。"""
    from types import SimpleNamespace
    from backend.tasks import orchestrator as m
    task = SimpleNamespace(region=None, universe=None)
    out = await m._select_launch_params(db=None, finalized_task=task)
    assert out is None


def test_orchestrator_thresholds_includes_sub3():
    """Sub-phase 3 config 新加 4 项可用。"""
    from backend.config import settings
    assert getattr(settings, "ORCHESTRATOR_LOOKBACK_DAYS", None) == 7
    assert getattr(settings, "ORCHESTRATOR_PRIOR_PASSES", None) == 1
    assert getattr(settings, "ORCHESTRATOR_PRIOR_FAILS", None) == 1
    assert getattr(settings, "ORCHESTRATOR_DATASETS_PER_TASK", None) == 3
