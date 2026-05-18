"""Phase 1.5-C TaskResponse v15c schema tests (2026-05-18).

phase15-D PR3b/cleanup (2026-05-18): cascade_phase + cascade_round_idx
fields removed from MiningTask ORM + DB schema. The PR1 _derive_current_tier
helper was the source-of-truth for these fields; it's now gone too. This
test module covers what remains: TaskResponse + TaskDetailResponse
v15c fields present + cascade_phase absent.
"""
from __future__ import annotations

import pytest


def test_task_response_schema_includes_v15c_fields():
    """schedule + starting_tier + mining_mode + current_tier present."""
    from backend.routers.tasks import TaskResponse
    fields = TaskResponse.model_fields
    for name in (
        "schedule", "starting_tier", "mining_mode", "current_tier",
    ):
        assert name in fields, f"missing V1.2-C5 field: {name}"


def test_task_response_schema_no_cascade_phase_post_pr3b():
    """phase15-D PR3b/cleanup: cascade_phase + cascade_round_idx dropped."""
    from backend.routers.tasks import TaskResponse
    fields = TaskResponse.model_fields
    assert "cascade_phase" not in fields, (
        "cascade_phase should be dropped by phase15-D cleanup pass"
    )
    assert "cascade_round_idx" not in fields, (
        "cascade_round_idx should be dropped by phase15-D cleanup pass"
    )


def test_task_detail_response_inherits_v15c_fields():
    """TaskDetailResponse extends TaskResponse so it must include all fields."""
    from backend.routers.tasks import TaskDetailResponse
    fields = TaskDetailResponse.model_fields
    for name in (
        "schedule", "starting_tier", "current_tier", "trace_steps",
    ):
        assert name in fields


def test_derive_current_tier_helper_removed():
    """phase15-D PR3b/cleanup: _derive_current_tier helper deleted —
    cascade_phase column gone so no derivation source. current_tier
    defaults None until a future enhancement reads run.runtime_state."""
    import backend.routers.tasks as tasks_router
    assert not hasattr(tasks_router, "_derive_current_tier"), (
        "_derive_current_tier should be removed by phase15-D cleanup pass"
    )


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
        current_tier=2,
    )
    assert resp.current_tier == 2
    assert resp.schedule == "CASCADE"


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
