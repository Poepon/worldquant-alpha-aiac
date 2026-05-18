"""TaskResponse schema sanity post tier-system removal (2026-05-18).

Verifies that after Ships #1-#7 the Pydantic response schemas:
  * have ``schedule`` as the sole scheduling field
  * have dropped ``agent_mode`` / ``starting_tier`` / ``mining_mode`` /
    ``current_tier`` / ``cascade_phase`` / ``cascade_round_idx``
  * still inherit cleanly from TaskResponse → TaskDetailResponse
"""
from __future__ import annotations

from datetime import datetime


_DROPPED_FIELDS = (
    "agent_mode",
    "starting_tier",
    "mining_mode",
    "current_tier",
    "cascade_phase",
    "cascade_round_idx",
)


def test_task_response_schedule_field_present():
    """schedule survives as the sole scheduling field."""
    from backend.routers.tasks import TaskResponse
    assert "schedule" in TaskResponse.model_fields


def test_task_response_dropped_tier_fields_absent():
    """All tier-related response fields gone post Ship #1."""
    from backend.routers.tasks import TaskResponse
    for name in _DROPPED_FIELDS:
        assert name not in TaskResponse.model_fields, (
            f"{name} should be removed post tier-system removal (Ship #1)"
        )


def test_task_detail_response_inherits_schedule():
    """TaskDetailResponse extends TaskResponse so schedule + trace_steps
    are both present + the dropped-tier-fields invariant carries through
    inheritance."""
    from backend.routers.tasks import TaskDetailResponse
    fields = TaskDetailResponse.model_fields
    assert "schedule" in fields
    assert "trace_steps" in fields
    for name in _DROPPED_FIELDS:
        assert name not in fields


def test_task_response_constructs_with_schedule():
    from backend.routers.tasks import TaskResponse

    resp = TaskResponse(
        id=1,
        task_name="t",
        region="USA",
        universe="TOP3000",
        dataset_strategy="manual",
        status="RUNNING",
        daily_goal=4,
        progress_current=0,
        max_iterations=10,
        created_at=datetime.utcnow(),
        schedule="FLAT",
    )
    assert resp.schedule == "FLAT"


def test_task_response_schedule_defaults_to_none():
    """Old clients constructing without schedule → None."""
    from backend.routers.tasks import TaskResponse

    resp = TaskResponse(
        id=1, task_name="t", region="USA", universe="TOP3000",
        dataset_strategy="manual", status="RUNNING", daily_goal=4,
        progress_current=0, max_iterations=10, created_at=datetime.utcnow(),
    )
    assert resp.schedule is None


def test_task_create_request_extra_ignore_accepts_stale_agent_mode():
    """Per Round 5 B2 fix: TaskCreateRequest has Config.extra='ignore' so
    cached frontend clients sending agent_mode/starting_tier don't 422."""
    from backend.routers.tasks import TaskCreateRequest

    req = TaskCreateRequest(
        name="t", region="USA", universe="TOP3000",
        agent_mode="AUTONOMOUS_TIER2",        # stale field
        starting_tier=2,                       # stale field
    )
    assert req.name == "t"
    # The stale fields are silently dropped.
    assert not hasattr(req, "agent_mode")
    assert not hasattr(req, "starting_tier")
