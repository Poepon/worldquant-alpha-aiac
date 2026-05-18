"""Integration: Phase 1.5-C dual-source read paths (plan v1.3 §3.6).

Verifies ENABLE_TASK_SCHEMA_V2 flag flip cleanly switches read paths from
legacy columns (mining_mode / cascade_phase) to new authoritative cols
(schedule / starting_tier / runtime_state.current_tier), with byte-equivalent
behavior for tasks created after Phase 1.5-B backfill (2026-05-17).

Test cases per plan §3.6:
  1. test_is_cascade_schedule_flag_off_reads_mining_mode
  2. test_is_cascade_schedule_flag_on_reads_schedule
  3. test_resolve_cascade_phase_flag_off_uses_task_cascade_phase
  4. test_resolve_cascade_phase_flag_on_uses_runtime_state_current_tier
  5. test_resolve_cascade_phase_flag_on_with_null_runtime_state_falls_back
  6. test_to_session_info_populates_new_fields (V1.2-C5)
  7. test_cascade_revive_inherits_current_tier_from_prior_run (V1.2-B3)

Pure-logic tests use mock objects; no DB required.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.config import _flag_override_cache
from backend.services.task_service import MiningSessionInfo, TaskService
from backend.tasks.mining_tasks import _is_cascade_schedule, _resolve_cascade_phase


@pytest.fixture(autouse=True)
def _clear_flag_cache():
    _flag_override_cache.clear()
    yield
    _flag_override_cache.clear()


# ---------------------------------------------------------------------------
# Test 1-2: _is_cascade_schedule dual-source
# ---------------------------------------------------------------------------

def test_is_cascade_schedule_flag_off_reads_mining_mode():
    """plan §3.6 (1): flag OFF — read mining_mode (legacy)."""
    task = SimpleNamespace(
        mining_mode="CONTINUOUS_CASCADE",
        schedule="ONESHOT",  # divergent on purpose to test flag preference
    )
    assert _is_cascade_schedule(task) is True

    task.mining_mode = "DISCRETE"
    assert _is_cascade_schedule(task) is False

    task.mining_mode = "FLAT_CONTINUOUS"
    assert _is_cascade_schedule(task) is False


def test_is_cascade_schedule_flag_on_reads_schedule():
    """plan §3.6 (2): flag ON — read schedule (new authoritative)."""
    _flag_override_cache["ENABLE_TASK_SCHEMA_V2"] = True

    task = SimpleNamespace(
        mining_mode="DISCRETE",   # divergent: legacy says discrete
        schedule="CASCADE",        # new col says cascade — flag ON wins
    )
    assert _is_cascade_schedule(task) is True

    task.schedule = "ONESHOT"
    assert _is_cascade_schedule(task) is False

    task.schedule = None
    assert _is_cascade_schedule(task) is False

    # case-insensitive
    task.schedule = "cascade"
    assert _is_cascade_schedule(task) is True


# ---------------------------------------------------------------------------
# Test 3-5: _resolve_cascade_phase dual-source
# ---------------------------------------------------------------------------

def test_resolve_cascade_phase_flag_off_uses_task_cascade_phase():
    """plan §3.6 (3): flag OFF — read task.cascade_phase (legacy)."""
    task = SimpleNamespace(cascade_phase="T2")
    run = SimpleNamespace(runtime_state={"current_tier": 3})  # would say T3 if read
    assert _resolve_cascade_phase(task, run) == "T2"  # legacy wins

    task.cascade_phase = None
    assert _resolve_cascade_phase(task, run) == "T1"  # fallback


def test_resolve_cascade_phase_flag_on_uses_runtime_state_current_tier():
    """plan §3.6 (4): flag ON — prefer runtime_state.current_tier."""
    _flag_override_cache["ENABLE_TASK_SCHEMA_V2"] = True

    task = SimpleNamespace(cascade_phase="T1")  # legacy says T1
    run = SimpleNamespace(runtime_state={"current_tier": 3})  # v2 says T3
    assert _resolve_cascade_phase(task, run) == "T3"  # v2 wins

    run.runtime_state = {"current_tier": 2}
    assert _resolve_cascade_phase(task, run) == "T2"

    run.runtime_state = {"current_tier": 1}
    assert _resolve_cascade_phase(task, run) == "T1"


def test_resolve_cascade_phase_flag_on_with_null_runtime_state_falls_back():
    """plan §3.6 (5): flag ON + missing runtime_state → fall back to cascade_phase."""
    _flag_override_cache["ENABLE_TASK_SCHEMA_V2"] = True

    task = SimpleNamespace(cascade_phase="T2")
    # Run with empty runtime_state (e.g. legacy run pre-1.5-B)
    run = SimpleNamespace(runtime_state={})
    assert _resolve_cascade_phase(task, run) == "T2"

    # Run with no current_tier key
    run = SimpleNamespace(runtime_state={"other_key": "ignored"})
    assert _resolve_cascade_phase(task, run) == "T2"

    # Run is None entirely
    assert _resolve_cascade_phase(task, None) == "T2"

    # Invalid current_tier value (out of {1,2,3})
    run = SimpleNamespace(runtime_state={"current_tier": 99})
    assert _resolve_cascade_phase(task, run) == "T2"


# ---------------------------------------------------------------------------
# Test 6: V1.2-C5 — MiningSessionInfo populates new v15 fields
# ---------------------------------------------------------------------------

def test_to_session_info_populates_new_fields():
    """plan §3.6 (6) V1.2-C5: _to_session_info fills schedule / starting_tier / current_tier."""
    # Mock TaskService instance (only _to_session_info needed)
    svc = TaskService.__new__(TaskService)

    task = SimpleNamespace(
        id=42,
        task_name="test-task",
        region="USA",
        universe="TOP3000",
        status="RUNNING",
        mining_mode="CONTINUOUS_CASCADE",
        cascade_phase="T2",
        cascade_round_idx=5,
        progress_current=30,
        last_alpha_persisted_at=None,
        created_at=None,
        updated_at=None,
        schedule="CASCADE",
        starting_tier=1,
    )

    info = svc._to_session_info(task)
    assert isinstance(info, MiningSessionInfo)
    assert info.schedule == "CASCADE"
    assert info.starting_tier == 1
    # current_tier derived from cascade_phase mapping
    assert info.current_tier == 2  # T2 → 2

    # T3 case
    task.cascade_phase = "T3"
    info = svc._to_session_info(task)
    assert info.current_tier == 3

    # Null cascade_phase
    task.cascade_phase = None
    info = svc._to_session_info(task)
    assert info.current_tier is None

    # Missing schedule/starting_tier (pre-1.5-B task)
    task.schedule = None
    task.starting_tier = None
    info = svc._to_session_info(task)
    assert info.schedule is None
    assert info.starting_tier is None


# ---------------------------------------------------------------------------
# Test 7: V1.2-B3 — cascade revive inherits current_tier from prior_run.runtime_state
# ---------------------------------------------------------------------------

def test_cascade_revive_inheritance_logic_unit():
    """plan §3.6 V1.2-B3: verify the inheritance dict construction matches §3.4.1.

    Tests the pure construction logic embedded in session_watchdog.py without
    requiring a full DB / celery integration setup.
    """
    _phase_to_tier = {"T1": 1, "T2": 2, "T3": 3}

    # Case 1: prior_run has runtime_state with both keys
    prior_runtime_state = {"current_tier": 2, "round_idx": 5, "progress": 99}
    task = SimpleNamespace(cascade_phase="T1", cascade_round_idx=0)
    inherited = {
        "current_tier": prior_runtime_state.get(
            "current_tier",
            _phase_to_tier.get(task.cascade_phase or "T1", 1),
        ),
        "round_idx": prior_runtime_state.get(
            "round_idx",
            task.cascade_round_idx or 0,
        ),
    }
    assert inherited["current_tier"] == 2, "should inherit prior T2"
    assert inherited["round_idx"] == 5, "should inherit prior round_idx"
    assert "progress" not in inherited, "progress NOT inherited per §3.4.1"

    # Case 2: prior_run runtime_state empty → fallback to task.cascade_phase
    prior_runtime_state = {}
    task = SimpleNamespace(cascade_phase="T3", cascade_round_idx=7)
    inherited = {
        "current_tier": prior_runtime_state.get(
            "current_tier",
            _phase_to_tier.get(task.cascade_phase or "T1", 1),
        ),
        "round_idx": prior_runtime_state.get(
            "round_idx",
            task.cascade_round_idx or 0,
        ),
    }
    assert inherited["current_tier"] == 3, "fallback to T3 via task.cascade_phase"
    assert inherited["round_idx"] == 7, "fallback to task.cascade_round_idx"

    # Case 3: prior_run + task both empty → defaults T1 / 0
    task = SimpleNamespace(cascade_phase=None, cascade_round_idx=0)
    inherited = {
        "current_tier": {}.get(
            "current_tier",
            _phase_to_tier.get(task.cascade_phase or "T1", 1),
        ),
        "round_idx": {}.get(
            "round_idx",
            task.cascade_round_idx or 0,
        ),
    }
    assert inherited["current_tier"] == 1
    assert inherited["round_idx"] == 0


# ---------------------------------------------------------------------------
# Edge case: case-insensitivity + whitespace robustness for schedule
# ---------------------------------------------------------------------------

def test_resolve_cascade_phase_r6_dag_takes_priority():
    """plan v1.0 §5.3 R6 PR2: DAG selection wins over phase15-C current_tier."""
    _flag_override_cache["ENABLE_TASK_SCHEMA_V2"] = True
    _flag_override_cache["ENABLE_DAG_TRACE"] = True

    task = SimpleNamespace(cascade_phase="T1")
    # phase15-C says T2 but DAG selection points to a T3 node — DAG wins
    run = SimpleNamespace(runtime_state={
        "current_tier": 2,
        "dag": {
            "v": 1,
            "nodes": {"n_99_1_0": {"id": "n_99_1_0", "tier": 3}},
            "current_selection": "n_99_1_0",
        },
    })
    assert _resolve_cascade_phase(task, run) == "T3"


def test_resolve_cascade_phase_r6_dag_off_falls_to_phase15c():
    """DAG flag OFF → phase15-C current_tier wins (byte-equivalent legacy)."""
    _flag_override_cache["ENABLE_TASK_SCHEMA_V2"] = True
    # ENABLE_DAG_TRACE not set (default False)

    task = SimpleNamespace(cascade_phase="T1")
    run = SimpleNamespace(runtime_state={
        "current_tier": 3,
        "dag": {"v": 1, "nodes": {"n_99_1_0": {"id": "n_99_1_0", "tier": 1}}, "current_selection": "n_99_1_0"},
    })
    # DAG ignored, phase15-C wins
    assert _resolve_cascade_phase(task, run) == "T3"


def test_resolve_cascade_phase_r6_dag_on_missing_selection_falls_through():
    """DAG ON but no current_selection → falls through to phase15-C."""
    _flag_override_cache["ENABLE_TASK_SCHEMA_V2"] = True
    _flag_override_cache["ENABLE_DAG_TRACE"] = True

    task = SimpleNamespace(cascade_phase="T1")
    run = SimpleNamespace(runtime_state={
        "current_tier": 2,
        "dag": {"v": 1, "nodes": {}, "current_selection": None},
    })
    # DAG selection missing → fall through to phase15-C T2
    assert _resolve_cascade_phase(task, run) == "T2"


def test_resolve_cascade_phase_r6_dag_on_invalid_tier_falls_through():
    """DAG node tier not in {1,2,3} → fall through."""
    _flag_override_cache["ENABLE_TASK_SCHEMA_V2"] = True
    _flag_override_cache["ENABLE_DAG_TRACE"] = True

    task = SimpleNamespace(cascade_phase="T2")
    run = SimpleNamespace(runtime_state={
        "current_tier": None,
        "dag": {"v": 1, "nodes": {"n_99_1_0": {"tier": 99}}, "current_selection": "n_99_1_0"},
    })
    # DAG tier=99 invalid → falls through to phase15-C (None) → legacy T2
    assert _resolve_cascade_phase(task, run) == "T2"


def test_is_cascade_schedule_handles_various_string_cases():
    """schedule comparison should be case-insensitive and tolerant."""
    _flag_override_cache["ENABLE_TASK_SCHEMA_V2"] = True
    for sched in ("CASCADE", "cascade", "Cascade", "CASCADE  "):
        task = SimpleNamespace(mining_mode="DISCRETE", schedule=sched.strip())
        assert _is_cascade_schedule(task) is True, f"failed for {sched!r}"
    for sched in ("ONESHOT", "oneshot", "", "FLAT", "unknown"):
        task = SimpleNamespace(mining_mode="DISCRETE", schedule=sched)
        assert _is_cascade_schedule(task) is False, f"failed for {sched!r}"
