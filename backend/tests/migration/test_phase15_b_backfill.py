"""Phase 1.5-B (Revision 3b1c4e5d6a78) backfill + dual-write tests.

Per plan v1.3 §2.5:
- Migration structural tests (no DB)
- TaskService dual-write tests (model introspection — aiosqlite JSONB
  incompatibility means we can't actually INSERT a MiningTask here)
- _stamp_heartbeat V1.2-B2 split-brain fix tests via mocked task/run
- TaskService create_task default starting_tier mapping verification
"""
from __future__ import annotations

from pathlib import Path

import pytest


REVISION = "3b1c4e5d6a78"


# ---------------------------------------------------------------------------
# Migration structural tests
# ---------------------------------------------------------------------------

class TestRevisionBFile:
    @pytest.fixture
    def revision_path(self):
        path = (
            Path(__file__).parent.parent.parent / "alembic" / "versions"
            / f"{REVISION}_phase15_b_backfill.py"
        )
        if not path.exists():
            pytest.fail(f"Revision file missing: {path}")
        return path

    def test_revision_file_exists(self, revision_path):
        assert revision_path.exists()

    def test_revision_chains_off_revision_a(self, revision_path):
        src = revision_path.read_text(encoding="utf-8")
        assert f"revision: str = '{REVISION}'" in src
        # Plan §2 chain: A=7a3f9e1c2b8d → B=3b1c4e5d6a78
        assert "down_revision" in src and "7a3f9e1c2b8d" in src

    def test_revision_has_schedule_backfill(self, revision_path):
        src = revision_path.read_text(encoding="utf-8")
        assert "UPDATE mining_tasks SET" in src
        assert "CONTINUOUS_CASCADE" in src and "'CASCADE'" in src
        assert "'ONESHOT'" in src
        assert "AUTONOMOUS_TIER2" in src
        assert "AUTONOMOUS_TIER3" in src

    def test_revision_has_runtime_state_backfill(self, revision_path):
        src = revision_path.read_text(encoding="utf-8")
        # Plan §2.2 — only latest run per task gets backfill (older runs
        # keep '{}' default from Revision A)
        assert "latest_runs" in src or "DISTINCT ON (task_id)" in src
        assert "jsonb_build_object" in src
        assert "current_tier" in src
        assert "round_idx" in src
        assert "progress" in src

    def test_revision_downgrade_resets_values(self, revision_path):
        src = revision_path.read_text(encoding="utf-8")
        assert "def downgrade" in src
        # Downgrade resets new cols to server_default values without
        # touching legacy mining_mode / agent_mode / cascade_phase
        assert "'ONESHOT'" in src
        assert "starting_tier = 1" in src


# ---------------------------------------------------------------------------
# TaskService dual-write — model introspection
# ---------------------------------------------------------------------------

class TestTaskServiceDualWriteIntrospection:
    """We cannot construct MiningTask + commit in aiosqlite test fixture
    (JSONB target_datasets column doesn't compile on SQLite). Verify the
    dual-write code path exists by source inspection — full integration
    runs via existing test_v27_1_cascade_lock_takeover.py + production
    smoke."""

    @pytest.fixture
    def task_service_src(self):
        path = (
            Path(__file__).parent.parent.parent / "services" / "task_service.py"
        )
        return path.read_text(encoding="utf-8")

    def test_create_task_writes_schedule_and_starting_tier(self, task_service_src):
        """create_task() includes schedule + starting_tier in MiningTask(...) call."""
        # Find the create_task code section
        idx = task_service_src.find("async def create_task")
        assert idx >= 0
        # Look in the next ~3000 chars for dual-write markers
        section = task_service_src[idx:idx + 3000]
        assert "schedule=schedule" in section or 'schedule="ONESHOT"' in section
        assert "starting_tier=starting_tier" in section or "starting_tier=" in section

    def test_create_task_maps_tier2_to_starting_tier_2(self, task_service_src):
        """create_task() agent_mode='AUTONOMOUS_TIER2' → starting_tier=2."""
        idx = task_service_src.find("async def create_task")
        section = task_service_src[idx:idx + 3000]
        assert "AUTONOMOUS_TIER2" in section
        assert "starting_tier = 2" in section

    def test_create_task_maps_tier3_to_starting_tier_3(self, task_service_src):
        idx = task_service_src.find("async def create_task")
        section = task_service_src[idx:idx + 3000]
        assert "AUTONOMOUS_TIER3" in section
        assert "starting_tier = 3" in section

    def test_start_cascade_session_writes_schedule_cascade(self, task_service_src):
        """_start_cascade_session-style helper writes schedule='CASCADE' + starting_tier=1."""
        # Use the more specific MiningTask construction marker for cascade
        idx = task_service_src.find('mining_mode="CONTINUOUS_CASCADE"')
        assert idx >= 0
        section = task_service_src[max(0, idx - 400):idx + 800]
        assert 'schedule="CASCADE"' in section
        assert "starting_tier=1" in section


# ---------------------------------------------------------------------------
# _stamp_heartbeat V1.2-B2 split-brain fix — source inspection
# ---------------------------------------------------------------------------

class TestStampHeartbeatV12B2Fix:
    @pytest.fixture
    def mining_tasks_src(self):
        path = (
            Path(__file__).parent.parent.parent / "tasks" / "mining_tasks.py"
        )
        return path.read_text(encoding="utf-8")

    def test_heartbeat_uses_instance_level_mutation(self, mining_tasks_src):
        """_stamp_heartbeat assigns to task.last_alpha_persisted_at directly,
        NOT via db.execute(update(MiningTask)...) bulk SQL."""
        idx = mining_tasks_src.find("async def _stamp_heartbeat")
        assert idx >= 0
        # Narrow window to ~1500 chars (function body is ~40 lines = ~1500 chars)
        # — wider window catches subsequent functions where update(MiningTask)
        # legitimately appears.
        section = mining_tasks_src[idx:idx + 1500]
        # Instance-level mutation present
        assert "task.last_alpha_persisted_at = now_utc" in section
        # Bulk SQL pattern is gone from THIS function. The comment about it
        # legitimately mentions "update(MiningTask)" — filter that out.
        # Extract executable lines (non-comment) and check no `update(MiningTask)` call
        code_lines = [
            l for l in section.split("\n")
            if l.strip() and not l.strip().startswith("#")
        ]
        code_only = "\n".join(code_lines)
        assert "update(MiningTask)" not in code_only, (
            "bulk SQL update(MiningTask) still present in _stamp_heartbeat — "
            "V1.2-B2 fix incomplete"
        )

    def test_heartbeat_dual_writes_runtime_state(self, mining_tasks_src):
        idx = mining_tasks_src.find("async def _stamp_heartbeat")
        section = mining_tasks_src[idx:idx + 2500]
        assert "run.runtime_state" in section
        assert 'flag_modified(run, "runtime_state")' in section
        # Specific keys backfilled by Revision B + dual-written here
        assert "last_persisted_at" in section
        assert "round_idx" in section

    def test_heartbeat_has_V12_B2_comment_marker(self, mining_tasks_src):
        """Self-documenting marker — future readers can grep for this."""
        idx = mining_tasks_src.find("async def _stamp_heartbeat")
        section = mining_tasks_src[idx:idx + 2500]
        assert "V1.2-B2" in section


# ---------------------------------------------------------------------------
# Cascade phase advancement dual-write — source inspection
# ---------------------------------------------------------------------------

class TestCascadePhaseAdvancementDualWrite:
    @pytest.fixture
    def mining_tasks_src(self):
        path = (
            Path(__file__).parent.parent.parent / "tasks" / "mining_tasks.py"
        )
        return path.read_text(encoding="utf-8")

    def test_t1_to_t2_dual_writes_current_tier_2(self, mining_tasks_src):
        # Find the T1→T2 transition block
        idx = mining_tasks_src.find('task.cascade_phase = "T2"')
        assert idx >= 0
        section = mining_tasks_src[idx:idx + 500]
        assert '"current_tier": 2' in section
        assert "flag_modified" in section

    def test_t2_to_t3_dual_writes_current_tier_3(self, mining_tasks_src):
        idx = mining_tasks_src.find('task.cascade_phase = "T3"')
        assert idx >= 0
        section = mining_tasks_src[idx:idx + 500]
        assert '"current_tier": 3' in section
        assert "flag_modified" in section

    def test_round_complete_resets_to_t1_dual_writes_tier_1(self, mining_tasks_src):
        # Find the round-complete reset (cascade_round_idx += 1 first)
        idx = mining_tasks_src.find("task.cascade_round_idx += 1")
        assert idx >= 0
        section = mining_tasks_src[idx:idx + 600]
        assert 'task.cascade_phase = "T1"' in section
        assert '"current_tier": 1' in section
        assert "flag_modified" in section


# ---------------------------------------------------------------------------
# Required imports check
# ---------------------------------------------------------------------------

class TestImportSync:
    def test_mining_tasks_imports_flag_modified(self):
        path = (
            Path(__file__).parent.parent.parent / "tasks" / "mining_tasks.py"
        )
        src = path.read_text(encoding="utf-8")
        assert "from sqlalchemy.orm.attributes import flag_modified" in src
