"""Unit tests for BrainRoleSwitchService (plan §9).

Covers:
  - activate: sets flag + shortens multi-sim latch + deletes EVER key + enqueues sync_datasets
  - deactivate: only clears flag (does NOT touch multi-sim latch — R1-M-1)
  - get_state: returns mode + effective_* + running_tasks_count + last_switched_at/by (UTC marker)
  - _iso_utc: appends 'Z' suffix to naive UTC datetimes
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.config import _flag_override_cache
from backend.services.brain_role_switch_service import (
    BrainRoleSwitchService,
    _iso_utc,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    _flag_override_cache.clear()
    yield
    _flag_override_cache.clear()


def test_iso_utc_appends_z_to_naive_datetime():
    """Naive datetime → ISO 8601 string with 'Z' suffix (frontend parses as UTC)."""
    dt = datetime(2026, 5, 16, 3, 14, 15, 123456)
    result = _iso_utc(dt)
    assert result.endswith("Z")
    assert result == "2026-05-16T03:14:15.123456Z"


def test_iso_utc_none_returns_none():
    assert _iso_utc(None) is None


@pytest.mark.asyncio
async def test_activate_consultant_mode_sets_flag_and_enqueues_sync():
    """activate: flag set + latch shortened/EVER deleted + sync_datasets.delay called."""
    flag_svc = AsyncMock()
    db = AsyncMock()
    svc = BrainRoleSwitchService(db, flag_svc)

    mock_redis = AsyncMock()
    with (
        patch("backend.adapters.brain_adapter.BrainAdapter._get_slot_redis",
              new=AsyncMock(return_value=mock_redis)),
        patch("backend.tasks.sync_tasks.sync_datasets.delay") as mock_delay,
    ):
        result = await svc.activate_consultant_mode(actor="test_user", note="test")

    # flag set
    flag_svc.set.assert_awaited_once_with(
        "ENABLE_BRAIN_CONSULTANT_MODE", True, actor="test_user", note="test",
    )
    # latch shortened (TTL 300s) + EVER key deleted
    mock_redis.expire.assert_awaited_once_with("brain:no_multisim", 300)
    mock_redis.delete.assert_awaited_once_with("brain:no_multisim_ever")
    # sync_datasets.delay called with regions list (CONSULTANT_REGION_UNIVERSES keys)
    mock_delay.assert_called_once()
    kwargs = mock_delay.call_args.kwargs
    assert "regions" in kwargs
    assert set(kwargs["regions"]) == {"USA", "CHN", "HKG", "JPN", "EUR"}

    # response payload
    assert result["mode"] == "CONSULTANT"
    assert result["sync_enqueued"] is True
    assert result["actor"] == "test_user"


@pytest.mark.asyncio
async def test_deactivate_consultant_mode_only_clears_flag():
    """deactivate must NOT touch multi-sim latch (R1-M-1 — would create
    24h invisible perf cliff if BRAIN still has Consultant access)."""
    flag_svc = AsyncMock()
    db = AsyncMock()
    svc = BrainRoleSwitchService(db, flag_svc)

    # Patch redis getter so we can assert it's not called
    with patch(
        "backend.adapters.brain_adapter.BrainAdapter._get_slot_redis",
        new=AsyncMock(),
    ) as mock_get_redis:
        result = await svc.deactivate_consultant_mode(actor="ops")

    flag_svc.clear_override.assert_awaited_once_with(
        "ENABLE_BRAIN_CONSULTANT_MODE", actor="ops", note="手动回退",
    )
    # NO redis interaction on deactivate (R1-M-1)
    mock_get_redis.assert_not_called()
    assert result["mode"] == "USER"


@pytest.mark.asyncio
async def test_activate_tolerates_redis_failure_silently():
    """Redis errors in latch cleanup must NOT raise (best-effort)."""
    flag_svc = AsyncMock()
    db = AsyncMock()
    svc = BrainRoleSwitchService(db, flag_svc)

    with (
        patch("backend.adapters.brain_adapter.BrainAdapter._get_slot_redis",
              new=AsyncMock(side_effect=ConnectionError("redis down"))),
        patch("backend.tasks.sync_tasks.sync_datasets.delay") as mock_delay,
    ):
        result = await svc.activate_consultant_mode(actor="ops")

    # flag still set despite redis failure
    flag_svc.set.assert_awaited_once()
    # sync still enqueued
    mock_delay.assert_called_once()
    assert result["mode"] == "CONSULTANT"


@pytest.mark.asyncio
async def test_get_state_returns_mode_and_effective_and_timestamp():
    """get_state: mode + effective_* + running_tasks_count + last_switched_at (UTC Z)."""
    flag_svc = AsyncMock()
    # FlagState-like mock
    mock_flag_state = MagicMock()
    mock_flag_state.updated_at = datetime(2026, 5, 16, 3, 14, 15)
    mock_flag_state.updated_by = "ops_console"
    flag_svc.get_one = AsyncMock(return_value=mock_flag_state)

    # mock db.execute returning 3 running tasks
    mock_scalar_result = MagicMock()
    mock_scalar_result.scalar = MagicMock(return_value=3)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=mock_scalar_result)

    svc = BrainRoleSwitchService(db, flag_svc)
    # USER mode default
    state = await svc.get_state()

    assert state["mode"] == "USER"
    assert state["effective_default_test_period"] == "P2Y0M"
    assert state["effective_sharpe_submit_min"] == 1.5
    assert state["effective_region_universes"] == {"USA": "TOP3000"}
    assert state["running_tasks_count"] == 3
    assert state["last_switched_at"] == "2026-05-16T03:14:15Z"
    assert state["last_switched_by"] == "ops_console"


@pytest.mark.asyncio
async def test_get_state_consultant_mode_reflects_flag():
    """When flag is set, get_state reports CONSULTANT mode + updated effective_*."""
    _flag_override_cache["ENABLE_BRAIN_CONSULTANT_MODE"] = True

    flag_svc = AsyncMock()
    flag_svc.get_one = AsyncMock(return_value=None)  # never been set in DB
    mock_scalar_result = MagicMock()
    mock_scalar_result.scalar = MagicMock(return_value=0)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=mock_scalar_result)

    svc = BrainRoleSwitchService(db, flag_svc)
    state = await svc.get_state()

    assert state["mode"] == "CONSULTANT"
    assert state["effective_default_test_period"] == "P0Y"
    assert state["effective_sharpe_submit_min"] == 1.58
    assert "CHN" in state["effective_region_universes"]
    assert state["last_switched_at"] is None
    assert state["last_switched_by"] is None
