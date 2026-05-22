"""Tests for the hypothesis_centric level-pinning root-cause fix (2026-05-22).

Root cause (empirical): alpha.hypothesis_id was NULL on ~98% of FLAT alphas
because HYPOTHESIS_CENTRIC_LEVEL lived only in .env (not a refreshable flag),
so a Celery worker started before .env was bumped ran at level 0 forever, and
FLAT sessions never pinned the level into task.config. Explicit variant=2 tasks
linked at 98%; absent-variant FLAT at ~5-13%.

Fix 1: start_flat_session stamps config['hypothesis_centric_variant'] = the
       caller-process HYPOTHESIS_CENTRIC_LEVEL → worker reads the pinned value.
Fix 2: register HYPOTHESIS_CENTRIC_LEVEL in SUPPORTED_FLAGS → hot-refreshable.
"""
from unittest.mock import MagicMock, patch

import pytest

from backend.services.feature_flag_service import SUPPORTED_FLAGS


class _FakeTask:
    def __init__(self, config):
        self.config = config


def _active_level(config):
    from backend.tasks.mining_tasks import _get_active_level
    return _get_active_level(_FakeTask(config))


class TestActiveLevelResolution:
    def test_stamped_variant_wins_over_settings(self, monkeypatch):
        # A pinned config variant is honored regardless of the worker's
        # (possibly stale) settings.HYPOTHESIS_CENTRIC_LEVEL.
        from backend.config import settings
        monkeypatch.setattr(settings, "HYPOTHESIS_CENTRIC_LEVEL", 0, raising=False)
        assert _active_level({"hypothesis_centric_variant": 2}) == 2  # pinned wins
        assert _active_level({"hypothesis_centric_variant": 0}) == 0

    def test_absent_variant_falls_back_to_settings(self, monkeypatch):
        from backend.config import settings
        monkeypatch.setattr(settings, "HYPOTHESIS_CENTRIC_LEVEL", 2, raising=False)
        assert _active_level({"flat_cursor": 0}) == 2  # this is the fragile path
        monkeypatch.setattr(settings, "HYPOTHESIS_CENTRIC_LEVEL", 0, raising=False)
        assert _active_level({}) == 0


def test_hypothesis_centric_level_registered_as_int_flag():
    spec = SUPPORTED_FLAGS.get("HYPOTHESIS_CENTRIC_LEVEL")
    assert spec is not None, "HYPOTHESIS_CENTRIC_LEVEL must be hot-refreshable"
    assert spec.flag_type == "int"


@pytest.mark.asyncio
async def test_start_flat_session_pins_level_into_config(db_session, monkeypatch):
    from backend.config import settings
    from backend.services.task_service import TaskService

    monkeypatch.setattr(settings, "HYPOTHESIS_CENTRIC_LEVEL", 2, raising=False)

    async def _noop_dispatch(self, *a, **k):  # avoid celery / run creation
        return None
    monkeypatch.setattr(TaskService, "_dispatch_session_worker", _noop_dispatch)

    svc = TaskService(db_session)
    info = await svc.start_flat_session(region="USA", universe="TOP3000", datasets=["pv1"])
    task = await svc.task_repo.get_by_id(info.task_id)

    # The created FLAT task pins the level → immune to a stale worker .env.
    assert (task.config or {}).get("hypothesis_centric_variant") == 2
    assert (task.config or {}).get("flat_cursor") == 0
    assert task.schedule == "FLAT"

    # And it resolves correctly even if the worker's setting is wrong.
    monkeypatch.setattr(settings, "HYPOTHESIS_CENTRIC_LEVEL", 0, raising=False)
    assert _active_level(task.config) == 2
