"""Phase 1.5-Fields (plan v1.3 §5, 2026-05-17) — 3-field consolidation tests.

Covers:
- Schedule enum mirror of legacy mining_mode
- tier_from_task helper priority (starting_tier > AGENT_MODE_TO_TIER fallback)
- TaskCreateRequest/Data accept new schedule + starting_tier fields
- create_task explicit fields take priority over derived agent_mode mapping
- mining_agent.factor_tier prefers tier_from_task
- INTERACTIVE legacy agent_mode → 1 fallback (0 prod rows)
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# Schedule enum
# ---------------------------------------------------------------------------

class TestScheduleEnum:
    def test_enum_values(self):
        from backend.models.base import Schedule
        assert Schedule.ONESHOT.value == "ONESHOT"
        assert Schedule.CASCADE.value == "CASCADE"

    def test_enum_member_count(self):
        from backend.models.base import Schedule
        assert len(list(Schedule)) == 2

    def test_str_compatibility(self):
        from backend.models.base import Schedule
        # str enum — value equals string
        assert Schedule.ONESHOT == "ONESHOT"
        assert Schedule.CASCADE == "CASCADE"


# ---------------------------------------------------------------------------
# tier_from_task helper — priority + fallback
# ---------------------------------------------------------------------------

class TestTierFromTask:
    def test_prefers_starting_tier_when_set(self):
        from backend.services.task_service import TaskService
        task = SimpleNamespace(starting_tier=3, agent_mode="AUTONOMOUS_TIER1")
        # starting_tier=3 wins over agent_mode mapping which would give 1
        assert TaskService.tier_from_task(task) == 3

    def test_falls_back_to_agent_mode_when_starting_tier_none(self):
        from backend.services.task_service import TaskService
        task = SimpleNamespace(starting_tier=None, agent_mode="AUTONOMOUS_TIER2")
        assert TaskService.tier_from_task(task) == 2

    def test_falls_back_to_agent_mode_when_starting_tier_invalid(self):
        from backend.services.task_service import TaskService
        task = SimpleNamespace(starting_tier=99, agent_mode="AUTONOMOUS_TIER3")
        # 99 not in (1,2,3) → fall back to mapping
        assert TaskService.tier_from_task(task) == 3

    def test_interactive_legacy_falls_back_to_1(self):
        """INTERACTIVE → AGENT_MODE_TO_TIER value None → 1 default
        (0 production rows confirmed pre-flight)."""
        from backend.services.task_service import TaskService
        task = SimpleNamespace(starting_tier=None, agent_mode="INTERACTIVE")
        assert TaskService.tier_from_task(task) == 1

    def test_unknown_agent_mode_defaults_to_1(self):
        from backend.services.task_service import TaskService
        task = SimpleNamespace(starting_tier=None, agent_mode="UNKNOWN_FUTURE")
        assert TaskService.tier_from_task(task) == 1

    def test_missing_starting_tier_attr_falls_back(self):
        # If task object doesn't have starting_tier at all (pre-Revision A model)
        from backend.services.task_service import TaskService
        class _LegacyTask:
            agent_mode = "AUTONOMOUS_TIER2"
        assert TaskService.tier_from_task(_LegacyTask()) == 2


# ---------------------------------------------------------------------------
# TaskCreateData + TaskCreateRequest accept new fields
# ---------------------------------------------------------------------------

class TestTaskCreateDataNewFields:
    def test_data_accepts_schedule(self):
        from backend.services.task_service import TaskCreateData
        d = TaskCreateData(name="t", schedule="CASCADE")
        assert d.schedule == "CASCADE"

    def test_data_accepts_starting_tier(self):
        from backend.services.task_service import TaskCreateData
        d = TaskCreateData(name="t", starting_tier=3)
        assert d.starting_tier == 3

    def test_data_defaults_both_none(self):
        from backend.services.task_service import TaskCreateData
        d = TaskCreateData(name="t")
        assert d.schedule is None
        assert d.starting_tier is None

    def test_request_accepts_schedule(self):
        from backend.routers.tasks import TaskCreateRequest
        req = TaskCreateRequest(name="t", schedule="CASCADE")
        assert req.schedule == "CASCADE"

    def test_request_accepts_starting_tier(self):
        from backend.routers.tasks import TaskCreateRequest
        req = TaskCreateRequest(name="t", starting_tier=3)
        assert req.starting_tier == 3

    def test_request_backward_compat_no_new_fields(self):
        """TaskCreateRequest with only legacy fields still works."""
        from backend.routers.tasks import TaskCreateRequest
        req = TaskCreateRequest(name="t", agent_mode="AUTONOMOUS_TIER2")
        assert req.schedule is None
        assert req.starting_tier is None
        assert req.agent_mode == "AUTONOMOUS_TIER2"


# ---------------------------------------------------------------------------
# create_task parsing — explicit fields take priority
# ---------------------------------------------------------------------------

class TestCreateTaskParsingPriority:
    """Source-inspection tests (DB-free) — verifies create_task code reads
    explicit data.schedule / data.starting_tier before falling back to
    agent_mode mapping."""

    @pytest.fixture
    def task_service_src(self):
        from pathlib import Path
        return (Path(__file__).parent.parent.parent / "services"
                / "task_service.py").read_text(encoding="utf-8")

    def test_create_task_reads_data_schedule_first(self, task_service_src):
        idx = task_service_src.find("async def create_task")
        section = task_service_src[idx:idx + 4000]
        # New parsing: schedule = (data.schedule or "ONESHOT").upper()
        assert "data.schedule or" in section
        assert ".upper()" in section

    def test_create_task_reads_data_starting_tier_first(self, task_service_src):
        idx = task_service_src.find("async def create_task")
        section = task_service_src[idx:idx + 4000]
        # New parsing: if data.starting_tier in (1, 2, 3)
        assert "data.starting_tier in (1, 2, 3)" in section


# ---------------------------------------------------------------------------
# mining_agent.factor_tier prefers tier_from_task
# ---------------------------------------------------------------------------

class TestMiningAgentFactorTierPriority:
    def test_factor_tier_uses_tier_from_task(self):
        from pathlib import Path
        src = (Path(__file__).parent.parent.parent / "agents" / "mining_agent.py"
               ).read_text(encoding="utf-8")
        idx = src.find("factor_tier_override is not None")
        assert idx >= 0
        section = src[idx:idx + 600]
        # Old: factor_tier_from_mode(task.agent_mode)
        # New: tier_from_task(task)
        assert "TaskService.tier_from_task(task)" in section
        # Old call should be gone from this section
        assert "factor_tier_from_mode(task.agent_mode)" not in section


# ---------------------------------------------------------------------------
# Backward-compat: legacy AGENT_MODE_TO_TIER still works
# ---------------------------------------------------------------------------

class TestLegacyMappingPreserved:
    def test_agent_mode_to_tier_unchanged(self):
        from backend.services.task_service import TaskService
        # All 5 legacy values preserved
        assert TaskService.AGENT_MODE_TO_TIER == {
            "AUTONOMOUS": 1,
            "AUTONOMOUS_TIER1": 1,
            "AUTONOMOUS_TIER2": 2,
            "AUTONOMOUS_TIER3": 3,
            "INTERACTIVE": None,
        }

    def test_factor_tier_from_mode_unchanged(self):
        from backend.services.task_service import TaskService
        assert TaskService.factor_tier_from_mode("AUTONOMOUS_TIER2") == 2
        assert TaskService.factor_tier_from_mode("INTERACTIVE") is None
