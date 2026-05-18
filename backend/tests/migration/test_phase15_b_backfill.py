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

    def test_downgrade_guards_enable_task_schema_v2_bug_m6(self, revision_path):
        """Bug M6 guard 1: downgrade must refuse when ENABLE_TASK_SCHEMA_V2
        is ON — wiping runtime_state would silently regress in-flight
        cascades.
        """
        src = revision_path.read_text(encoding="utf-8")
        idx = src.find("def downgrade(")
        assert idx >= 0
        downgrade_section = src[idx:]
        # Reads override row first, falls back to settings
        assert "feature_flag_overrides" in downgrade_section
        assert "ENABLE_TASK_SCHEMA_V2" in downgrade_section
        # Refuses with a clear exception
        assert "refusing downgrade" in downgrade_section
        assert "ENABLE_TASK_SCHEMA_V2 is ON" in downgrade_section

    def test_downgrade_guards_live_cascade_running_bug_m6(self, revision_path):
        """Bug M6 guard 2: downgrade must refuse if any RUNNING
        CONTINUOUS_CASCADE task exists — even with the flag OFF, a live
        cascade still depends on runtime_state.
        """
        src = revision_path.read_text(encoding="utf-8")
        idx = src.find("def downgrade(")
        downgrade_section = src[idx:]
        # Checks mining_tasks for live cascade
        assert "status = 'RUNNING'" in downgrade_section
        assert "mining_mode = 'CONTINUOUS_CASCADE'" in downgrade_section
        # Refuses with row count in the message
        assert "CONTINUOUS_CASCADE task(s) " in downgrade_section or \
               "CONTINUOUS_CASCADE task" in downgrade_section

    def test_downgrade_emits_pre_flight_warning_bug_m6(self, revision_path):
        """Bug M6: downgrade emits a logger.warning before the blanket
        UPDATE so the operator sees how many rows will be affected.
        """
        src = revision_path.read_text(encoding="utf-8")
        idx = src.find("def downgrade(")
        downgrade_section = src[idx:]
        assert "logger.warning" in downgrade_section
        assert "Bug M6" in downgrade_section


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

    # phase15-D PR3d/3e (2026-05-18): _start_cascade_session helper +
    # CONTINUOUS_CASCADE construction site deleted. test removed.


# phase15-D PR3d (2026-05-18): _stamp_heartbeat helper deleted alongside
# the 5 cascade helpers (~1028 LoC swept). TestStampHeartbeatV12B2Fix
# class removed — verified vestigial code that no longer exists.
#
# phase15-D PR3d (2026-05-18): cascade phase advancement
# (task.cascade_phase = "T2"/"T3"/"T1" + cascade_round_idx ++) deleted
# from mining_tasks.py. TestCascadePhaseAdvancementDualWrite class removed
# — same provenance.


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


# ---------------------------------------------------------------------------
# Bug M6 runtime guard tests — exercise downgrade() with a mocked bind
# ---------------------------------------------------------------------------

class TestM6DowngradeRuntimeGuards:
    """Bug M6: exercise the downgrade() guards in-process by loading the
    migration module under a controlled `op.get_bind()` mock. Verifies the
    Exception fires before the blanket UPDATE runs.
    """

    @pytest.fixture
    def migration_module(self):
        """Load the revision module without triggering Alembic's runner."""
        import importlib.util
        path = (
            Path(__file__).parent.parent.parent / "alembic" / "versions"
            / f"{REVISION}_phase15_b_backfill.py"
        )
        spec = importlib.util.spec_from_file_location(
            "phase15_b_migration", str(path)
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_downgrade_refuses_when_flag_on_via_override_row(
        self, migration_module, monkeypatch
    ):
        """ENABLE_TASK_SCHEMA_V2 override row = 'true' → Exception."""
        # Mock op.get_bind() to return a fake bind whose execute() answers
        # the override SELECT with a true value.
        class FakeRow:
            def __init__(self, v): self._v = v
            def __getitem__(self, i): return self._v
        class FakeResult:
            def __init__(self, row): self._row = row
            def fetchone(self): return self._row
        class FakeBind:
            def execute(self, stmt):
                s = str(stmt)
                if "feature_flag_overrides" in s:
                    return FakeResult(FakeRow("true"))
                # mining_tasks count should never run — but be safe
                return FakeResult(FakeRow(0))

        monkeypatch.setattr(migration_module.op, "get_bind", lambda: FakeBind())

        with pytest.raises(Exception, match="ENABLE_TASK_SCHEMA_V2 is ON"):
            migration_module.downgrade()

    def test_downgrade_refuses_when_live_cascade_running(
        self, migration_module, monkeypatch
    ):
        """Flag OFF but a RUNNING CONTINUOUS_CASCADE row exists → Exception."""
        class FakeRow:
            def __init__(self, v): self._v = v
            def __getitem__(self, i): return self._v
        class FakeResult:
            def __init__(self, row): self._row = row
            def fetchone(self): return self._row
        class FakeBind:
            def execute(self, stmt):
                s = str(stmt)
                if "feature_flag_overrides" in s:
                    # no override row
                    return FakeResult(None)
                if "RUNNING" in s and "CONTINUOUS_CASCADE" in s:
                    # 3 live cascades
                    return FakeResult(FakeRow(3))
                return FakeResult(FakeRow(0))

        monkeypatch.setattr(migration_module.op, "get_bind", lambda: FakeBind())
        # Force settings flag OFF too
        from backend.config import settings
        monkeypatch.setattr(settings, "ENABLE_TASK_SCHEMA_V2", False)

        with pytest.raises(Exception, match="CONTINUOUS_CASCADE"):
            migration_module.downgrade()

    def test_downgrade_proceeds_when_flag_off_and_no_live_cascade(
        self, migration_module, monkeypatch
    ):
        """Both guards pass → blanket UPDATEs are issued (captured)."""
        executed_sql = []

        class FakeRow:
            def __init__(self, v): self._v = v
            def __getitem__(self, i): return self._v
        class FakeResult:
            def __init__(self, row): self._row = row
            def fetchone(self): return self._row
        class FakeBind:
            def execute(self, stmt):
                s = str(stmt)
                if "feature_flag_overrides" in s:
                    return FakeResult(None)
                if "RUNNING" in s and "CONTINUOUS_CASCADE" in s:
                    return FakeResult(FakeRow(0))
                if "COUNT(*) FROM mining_tasks" in s:
                    return FakeResult(FakeRow(10))
                if "COUNT(*) FROM experiment_runs" in s:
                    return FakeResult(FakeRow(5))
                return FakeResult(FakeRow(0))

        monkeypatch.setattr(migration_module.op, "get_bind", lambda: FakeBind())
        monkeypatch.setattr(
            migration_module.op, "execute",
            lambda sql: executed_sql.append(sql)
        )
        from backend.config import settings
        monkeypatch.setattr(settings, "ENABLE_TASK_SCHEMA_V2", False)

        migration_module.downgrade()

        # Both blanket UPDATEs ran
        joined = "\n".join(executed_sql)
        assert "UPDATE mining_tasks" in joined
        assert "'ONESHOT'" in joined
        assert "UPDATE experiment_runs" in joined
        assert "'{}'::jsonb" in joined
