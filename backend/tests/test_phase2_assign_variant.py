"""F-5 assign_variant — 50/50 A/B variant assignment tests.

Plan v5+ §F-5 specified the mechanism but pre-2026-05-06 it was config-
slot-only (no code consumed HYPOTHESIS_CENTRIC_CANDIDATE). Now
TaskService.create_task injects `task.config["hypothesis_centric_variant"]`
based on settings:
  - candidate <= level → no-op (legacy behavior)
  - candidate > level  → random.choice([level, candidate]) per task
  - data.config supplies own variant → caller wins (ad-hoc pins)

Tests are pure-Python (no DB) — verify the assignment logic in isolation.
The DB integration is exercised by the smoke test launcher (runs against
live PG).
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock, AsyncMock

import pytest


@pytest.mark.asyncio
async def test_no_assignment_when_candidate_equals_level():
    """CANDIDATE = LEVEL = 0 → no auto-injection."""
    from backend.services.task_service import TaskService, TaskCreateData

    svc = TaskService.__new__(TaskService)
    svc._validate_tier_eligibility = AsyncMock()
    svc.task_repo = MagicMock()
    svc.task_repo.create = AsyncMock(side_effect=lambda t: t)
    svc.commit = AsyncMock()
    svc._to_summary = MagicMock(return_value=None)

    data = TaskCreateData(
        name="t", region="USA", universe="TOP3000",
        dataset_strategy="AUTO", target_datasets=[],
        agent_mode="AUTONOMOUS_TIER1", daily_goal=4,
        config={},
    )

    captured_config = {}
    def _capture(t):
        captured_config.update(t.config or {})
        return t
    svc.task_repo.create = AsyncMock(side_effect=_capture)

    with patch("backend.config.settings") as mock_settings:
        mock_settings.HYPOTHESIS_CENTRIC_LEVEL = 0
        mock_settings.HYPOTHESIS_CENTRIC_CANDIDATE = 0
        await svc.create_task(data)

    assert "hypothesis_centric_variant" not in captured_config


@pytest.mark.asyncio
async def test_assignment_when_candidate_higher_than_level():
    """CANDIDATE=2 > LEVEL=0 → variant ∈ {0, 2} injected."""
    from backend.services.task_service import TaskService, TaskCreateData

    svc = TaskService.__new__(TaskService)
    svc._validate_tier_eligibility = AsyncMock()
    svc.task_repo = MagicMock()
    svc.commit = AsyncMock()
    svc._to_summary = MagicMock(return_value=None)

    data = TaskCreateData(
        name="t", region="USA", universe="TOP3000",
        dataset_strategy="AUTO", target_datasets=[],
        agent_mode="AUTONOMOUS_TIER1", daily_goal=4,
        config={},
    )

    captured = []
    def _capture(t):
        captured.append(t.config.get("hypothesis_centric_variant"))
        return t
    svc.task_repo.create = AsyncMock(side_effect=_capture)

    # Patch random to deterministic
    with patch("backend.config.settings") as mock_settings, \
         patch("random.choice", side_effect=lambda lst: lst[1]):  # always picks CANDIDATE
        mock_settings.HYPOTHESIS_CENTRIC_LEVEL = 0
        mock_settings.HYPOTHESIS_CENTRIC_CANDIDATE = 2
        await svc.create_task(data)

    assert captured == [2]


@pytest.mark.asyncio
async def test_assignment_random_picks_level():
    """When random picks LEVEL side."""
    from backend.services.task_service import TaskService, TaskCreateData

    svc = TaskService.__new__(TaskService)
    svc._validate_tier_eligibility = AsyncMock()
    svc.task_repo = MagicMock()
    svc.commit = AsyncMock()
    svc._to_summary = MagicMock(return_value=None)

    data = TaskCreateData(
        name="t", region="USA", universe="TOP3000",
        dataset_strategy="AUTO", target_datasets=[],
        agent_mode="AUTONOMOUS_TIER1", daily_goal=4,
        config={},
    )

    captured = []
    svc.task_repo.create = AsyncMock(
        side_effect=lambda t: captured.append(t.config.get("hypothesis_centric_variant")) or t
    )

    with patch("backend.config.settings") as mock_settings, \
         patch("random.choice", side_effect=lambda lst: lst[0]):  # always picks LEVEL
        mock_settings.HYPOTHESIS_CENTRIC_LEVEL = 0
        mock_settings.HYPOTHESIS_CENTRIC_CANDIDATE = 2
        await svc.create_task(data)

    assert captured == [0]


@pytest.mark.asyncio
async def test_caller_supplied_variant_wins():
    """If data.config already contains hypothesis_centric_variant, F-5 logic
    must not overwrite it (ad-hoc scripts pin variants for targeted runs)."""
    from backend.services.task_service import TaskService, TaskCreateData

    svc = TaskService.__new__(TaskService)
    svc._validate_tier_eligibility = AsyncMock()
    svc.task_repo = MagicMock()
    svc.commit = AsyncMock()
    svc._to_summary = MagicMock(return_value=None)

    data = TaskCreateData(
        name="t", region="USA", universe="TOP3000",
        dataset_strategy="AUTO", target_datasets=[],
        agent_mode="AUTONOMOUS_TIER1", daily_goal=4,
        config={"hypothesis_centric_variant": 99, "other": "preserved"},
    )

    captured = []
    svc.task_repo.create = AsyncMock(
        side_effect=lambda t: captured.append(dict(t.config)) or t
    )

    with patch("backend.config.settings") as mock_settings:
        mock_settings.HYPOTHESIS_CENTRIC_LEVEL = 0
        mock_settings.HYPOTHESIS_CENTRIC_CANDIDATE = 2
        await svc.create_task(data)

    assert captured[0]["hypothesis_centric_variant"] == 99
    assert captured[0]["other"] == "preserved"


@pytest.mark.asyncio
async def test_5050_split_distribution():
    """Run 100 task creations and verify ~50/50 split between LEVEL and CANDIDATE."""
    import random as _r
    from backend.services.task_service import TaskService, TaskCreateData

    svc = TaskService.__new__(TaskService)
    svc._validate_tier_eligibility = AsyncMock()
    svc.task_repo = MagicMock()
    svc.commit = AsyncMock()
    svc._to_summary = MagicMock(return_value=None)

    data = TaskCreateData(
        name="t", region="USA", universe="TOP3000",
        dataset_strategy="AUTO", target_datasets=[],
        agent_mode="AUTONOMOUS_TIER1", daily_goal=4,
        config={},
    )

    captured = []
    svc.task_repo.create = AsyncMock(
        side_effect=lambda t: captured.append(t.config.get("hypothesis_centric_variant")) or t
    )

    _r.seed(42)
    with patch("backend.config.settings") as mock_settings:
        mock_settings.HYPOTHESIS_CENTRIC_LEVEL = 0
        mock_settings.HYPOTHESIS_CENTRIC_CANDIDATE = 2
        for _ in range(100):
            await svc.create_task(data)

    n_level = sum(1 for v in captured if v == 0)
    n_candidate = sum(1 for v in captured if v == 2)
    assert n_level + n_candidate == 100
    # 50/50 ± 30% tolerance — random.choice over 100 trials
    assert 35 <= n_level <= 65, f"unbalanced split: level={n_level}"
    assert 35 <= n_candidate <= 65, f"unbalanced split: candidate={n_candidate}"
