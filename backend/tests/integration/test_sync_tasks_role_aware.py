"""Integration: sync_tasks + refresh_tasks pass task-snapshot sharpe override.

Plan §5.2. Verifies the read_role_snapshot helper integrates correctly with
get_tier_thresholds at the sync_tasks/refresh_tasks call sites:

  - alpha with task_id=None (legacy) → no override → tier_thresholds walks
    settings.effective_sharpe_submit_min (current global value)
  - alpha with task_id pointing to MiningTask with brain_role_snapshot →
    override = snapshot value, NOT current settings
  - get_tier_thresholds() fallback path (tier=None) honors override
  - T1/T2/T3 paths IGNORE override (they're internal PROVISIONAL labels,
    not the submission gate — plan §5)

Uses AsyncMock for db.execute returning MiningTask SimpleNamespace.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.agents.graph.tier_thresholds import get_tier_thresholds
from backend.config import _flag_override_cache, settings
from backend.tasks._role_helpers import read_role_snapshot


@pytest.fixture(autouse=True)
def _clear_flag_cache():
    _flag_override_cache.clear()
    yield
    _flag_override_cache.clear()


def _db_returning(task_obj):
    """Build mock AsyncSession whose execute()...scalar_one_or_none → task_obj."""
    scalar = MagicMock()
    scalar.scalar_one_or_none = MagicMock(return_value=task_obj)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=scalar)
    return db


# ---------------------------------------------------------------------------
# Helper round-trip: snapshot read → get_tier_thresholds override
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_legacy_alpha_no_task_walks_current_settings():
    """alpha.task_id=None → helper returns {} → no override → walks settings."""
    db = AsyncMock()
    snapshot = await read_role_snapshot(None, db)
    assert snapshot == {}
    db.execute.assert_not_called()

    # tier_thresholds with override=None → uses settings.effective_sharpe_submit_min
    t = get_tier_thresholds(
        None, sharpe_submit_min_override=snapshot.get("effective_sharpe_submit_min"),
    )
    assert t["sharpe_min"] == settings.effective_sharpe_submit_min


@pytest.mark.asyncio
async def test_task_with_snapshot_uses_frozen_value():
    """alpha.task_id → MiningTask with brain_role_snapshot → override applied."""
    task = SimpleNamespace(id=1, config={
        "brain_role_snapshot": {
            "brain_consultant_mode_at_start": True,
            "effective_sharpe_submit_min": 1.58,
            "effective_default_test_period": "P0Y",
            "effective_region_universes": {"USA": "TOP3000"},
        },
    })
    db = _db_returning(task)

    snapshot = await read_role_snapshot(1, db)
    assert snapshot["effective_sharpe_submit_min"] == 1.58

    # Even with current settings in User mode (1.5), tier_thresholds uses
    # the task-snapshot value (1.58)
    assert settings.ENABLE_BRAIN_CONSULTANT_MODE is False
    t = get_tier_thresholds(
        None,
        sharpe_submit_min_override=snapshot.get("effective_sharpe_submit_min"),
    )
    assert t["sharpe_min"] == 1.58


@pytest.mark.asyncio
async def test_global_flag_flip_does_not_change_running_task_threshold():
    """Critical R2-M-3/M-4: running task's sharpe threshold stays at startup
    snapshot value even after Consultant flag is flipped globally."""
    # Task was started in User mode (snapshot = 1.5)
    task = SimpleNamespace(id=1, config={
        "brain_role_snapshot": {
            "brain_consultant_mode_at_start": False,
            "effective_sharpe_submit_min": 1.5,
            "effective_default_test_period": "P2Y0M",
            "effective_region_universes": {"USA": "TOP3000"},
        },
    })
    db = _db_returning(task)
    snapshot = await read_role_snapshot(1, db)

    # Now flip Consultant mode globally
    _flag_override_cache["ENABLE_BRAIN_CONSULTANT_MODE"] = True
    assert settings.effective_sharpe_submit_min == 1.58  # current settings

    # tier_thresholds for running task still uses 1.5 (snapshot)
    t = get_tier_thresholds(
        None,
        sharpe_submit_min_override=snapshot.get("effective_sharpe_submit_min"),
    )
    assert t["sharpe_min"] == 1.5


# ---------------------------------------------------------------------------
# Override only affects fallback (tier=None) — T1/T2/T3 internal unaffected
# ---------------------------------------------------------------------------

def test_tier1_internal_sharpe_unaffected_by_override():
    """T1 internal PROVISIONAL label sharpe (TIER1_SHARPE_MIN=1.25) must NOT
    be touched by override — override is only for the submission gate
    (fallback path tier=None)."""
    t = get_tier_thresholds(1, sharpe_submit_min_override=999.0)
    assert t["sharpe_min"] == settings.TIER1_SHARPE_MIN  # internal label
    assert t["sharpe_min"] != 999.0


def test_tier3_internal_sharpe_unaffected_by_override():
    """Same for T3 — internal label TIER3_SHARPE_MIN=1.5."""
    t = get_tier_thresholds(3, sharpe_submit_min_override=999.0)
    assert t["sharpe_min"] == settings.TIER3_SHARPE_MIN


def test_fallback_path_falls_back_to_settings_when_override_none():
    """When override=None and Consultant mode ON globally, fallback path
    uses settings.effective_sharpe_submit_min (current 1.58)."""
    _flag_override_cache["ENABLE_BRAIN_CONSULTANT_MODE"] = True
    t = get_tier_thresholds(None, sharpe_submit_min_override=None)
    assert t["sharpe_min"] == 1.58


def test_fallback_path_falls_back_to_settings_in_user_mode():
    """User mode default — fallback walks SHARPE_MIN."""
    t = get_tier_thresholds(None, sharpe_submit_min_override=None)
    assert t["sharpe_min"] == settings.SHARPE_MIN  # 1.5


@pytest.mark.asyncio
async def test_snapshot_read_returns_empty_for_missing_task():
    """Task deleted between alpha creation and sync → snapshot empty,
    no crash on .get."""
    db = _db_returning(None)  # task not found
    snapshot = await read_role_snapshot(999, db)
    assert snapshot == {}
    t = get_tier_thresholds(
        None,
        sharpe_submit_min_override=snapshot.get("effective_sharpe_submit_min"),
    )
    assert t["sharpe_min"] == settings.effective_sharpe_submit_min
