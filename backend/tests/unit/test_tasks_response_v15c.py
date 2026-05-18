"""Phase 1.5-C frontend cutover — TaskResponse + TaskDetailResponse v15c fields
(2026-05-18).

Verifies the legacy /tasks/* router now surfaces the V1.2-C5 scheduling
fields + the derived current_tier so the frontend cutover sites
(TaskManagement.jsx / Dashboard.jsx / TaskDetail.jsx) can read them
without falling back to agent_mode.

Covers:
  - _derive_current_tier maps T1/T2/T3 → 1/2/3
  - None cascade_phase → None tier (flat/discrete)
  - lowercase phase accepted (defensive)
  - unknown phase token → None (no exception)
  - TaskResponse / TaskDetailResponse schema include current_tier
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# _derive_current_tier helper
# ---------------------------------------------------------------------------

def test_derive_current_tier_maps_phases():
    from backend.routers.tasks import _derive_current_tier

    assert _derive_current_tier(SimpleNamespace(cascade_phase="T1")) == 1
    assert _derive_current_tier(SimpleNamespace(cascade_phase="T2")) == 2
    assert _derive_current_tier(SimpleNamespace(cascade_phase="T3")) == 3


def test_derive_current_tier_accepts_lowercase():
    """Defensive lowercase normalization — Phase 1.5-B backfill cases."""
    from backend.routers.tasks import _derive_current_tier
    assert _derive_current_tier(SimpleNamespace(cascade_phase="t2")) == 2


def test_derive_current_tier_returns_none_for_flat_discrete():
    from backend.routers.tasks import _derive_current_tier
    assert _derive_current_tier(SimpleNamespace(cascade_phase=None)) is None
    assert _derive_current_tier(SimpleNamespace(cascade_phase="")) is None


def test_derive_current_tier_unknown_phase_returns_none():
    """Defensive: a malformed phase string must not raise."""
    from backend.routers.tasks import _derive_current_tier
    assert _derive_current_tier(SimpleNamespace(cascade_phase="T9")) is None
    assert _derive_current_tier(SimpleNamespace(cascade_phase="garbage")) is None


def test_derive_current_tier_handles_missing_attribute():
    """Object without cascade_phase attribute → None (Pydantic / SimpleNamespace)."""
    from backend.routers.tasks import _derive_current_tier
    assert _derive_current_tier(SimpleNamespace()) is None


# ---------------------------------------------------------------------------
# Schema shape — current_tier + all V1.2-C5 fields present
# ---------------------------------------------------------------------------

def test_task_response_schema_includes_v15c_fields():
    from backend.routers.tasks import TaskResponse
    fields = TaskResponse.model_fields
    for name in (
        "schedule", "starting_tier", "mining_mode",
        "cascade_phase", "cascade_round_idx", "current_tier",
    ):
        assert name in fields, f"missing V1.2-C5 field: {name}"


def test_task_detail_response_inherits_v15c_fields():
    """TaskDetailResponse extends TaskResponse so it must include all fields."""
    from backend.routers.tasks import TaskDetailResponse
    fields = TaskDetailResponse.model_fields
    for name in (
        "schedule", "starting_tier", "current_tier", "trace_steps",
    ):
        assert name in fields


# ---------------------------------------------------------------------------
# TaskResponse construction with new fields
# ---------------------------------------------------------------------------

def test_task_response_constructs_with_current_tier():
    from datetime import datetime
    from backend.routers.tasks import TaskResponse

    resp = TaskResponse(
        id=1,
        task_name="t",
        region="USA",
        universe="TOP3000",
        dataset_strategy="manual",
        agent_mode="AUTONOMOUS",
        status="RUNNING",
        daily_goal=4,
        progress_current=0,
        max_iterations=10,
        created_at=datetime.utcnow(),
        schedule="CASCADE",
        starting_tier=1,
        cascade_phase="T2",
        current_tier=2,
    )
    assert resp.current_tier == 2
    assert resp.schedule == "CASCADE"
    assert resp.cascade_phase == "T2"


def test_task_response_defaults_v15c_fields_to_none():
    """Old clients constructing without new fields → None defaults."""
    from datetime import datetime
    from backend.routers.tasks import TaskResponse

    resp = TaskResponse(
        id=1, task_name="t", region="USA", universe="TOP3000",
        dataset_strategy="manual", agent_mode="AUTONOMOUS",
        status="RUNNING", daily_goal=4, progress_current=0,
        max_iterations=10, created_at=datetime.utcnow(),
    )
    assert resp.schedule is None
    assert resp.starting_tier is None
    assert resp.current_tier is None
    assert resp.cascade_phase is None
