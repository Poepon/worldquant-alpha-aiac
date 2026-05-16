"""Unit tests for tasks._role_helpers.read_role_snapshot.

Plan §5.2 helper used by sync_tasks + refresh_tasks to read MiningTask.config
["brain_role_snapshot"] for BRAIN-role-aware sharpe override. Covers:
  - task_id=None short-circuit (legacy alpha pre-v5)
  - task not found in DB → empty dict
  - task.config is None (Postgres NULL) → empty dict
  - task.config has no brain_role_snapshot → empty dict
  - task.config has snapshot → returns it
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.tasks._role_helpers import read_role_snapshot


@pytest.mark.asyncio
async def test_returns_empty_when_task_id_is_none():
    """Legacy alpha (alpha.task_id NULL) — short-circuit, no DB query."""
    mock_db = AsyncMock()  # would fail if any method called
    result = await read_role_snapshot(None, mock_db)
    assert result == {}
    mock_db.execute.assert_not_called()


def _mock_db_returning(task_obj):
    """Helper: build a mock AsyncSession whose execute().scalar_one_or_none()
    yields task_obj."""
    mock_result = MagicMock()
    mock_result.scalar_one_or_none = MagicMock(return_value=task_obj)
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=mock_result)
    return mock_db


@pytest.mark.asyncio
async def test_returns_empty_when_task_not_found():
    mock_db = _mock_db_returning(None)
    result = await read_role_snapshot(42, mock_db)
    assert result == {}


@pytest.mark.asyncio
async def test_returns_empty_when_config_is_none():
    """task.config is None (Postgres NULL) → empty dict (not crash on .get)."""
    task = MagicMock()
    task.config = None
    mock_db = _mock_db_returning(task)
    result = await read_role_snapshot(42, mock_db)
    assert result == {}


@pytest.mark.asyncio
async def test_returns_empty_when_no_brain_role_snapshot():
    """task.config exists but doesn't have brain_role_snapshot key."""
    task = MagicMock()
    task.config = {"hypothesis_centric_variant": "control"}
    mock_db = _mock_db_returning(task)
    result = await read_role_snapshot(42, mock_db)
    assert result == {}


@pytest.mark.asyncio
async def test_returns_snapshot_when_present():
    """task.config has brain_role_snapshot → returns the snapshot dict."""
    snapshot = {
        "brain_consultant_mode_at_start": True,
        "effective_default_test_period": "P0Y",
        "effective_sharpe_submit_min": 1.58,
        "effective_region_universes": {"USA": "TOP3000", "CHN": "TOP2000A"},
    }
    task = MagicMock()
    task.config = {
        "hypothesis_centric_variant": "control",  # other keys preserved
        "brain_role_snapshot": snapshot,
    }
    mock_db = _mock_db_returning(task)
    result = await read_role_snapshot(42, mock_db)
    assert result == snapshot


@pytest.mark.asyncio
async def test_returns_empty_when_snapshot_key_is_none():
    """Defensive: if snapshot key exists but value is None, return {}."""
    task = MagicMock()
    task.config = {"brain_role_snapshot": None}
    mock_db = _mock_db_returning(task)
    result = await read_role_snapshot(42, mock_db)
    assert result == {}
