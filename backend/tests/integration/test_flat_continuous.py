"""Integration: flat-F1 FLAT_CONTINUOUS mining mode (Phase 3, plan v1.5).

Tests the GO criteria from plan v1.5 §5:
  1. Flag ON + start_flat_session creates FLAT task
  2. Q1 V2: flat_cursor preserved across resume (inherit_runtime_state=True)
  3. Q1 V2 sanity: cascade resume does NOT inherit runtime_state
  4. Q2 A: intervene_task blocks FLAT mode (PAUSE + RESUME)
  5. start_flat_session rejects unsupported region
  6. resume_flat_session rejects non-FLAT task

Uses ``pg_session`` fixture (live Postgres on localhost:5433) per the
existing repo convention — MiningTask + ExperimentRun use JSONB columns
that aiosqlite cannot render. Rows are tagged with a unique uuid prefix
and cleaned up by the fixture finally block.
"""
from __future__ import annotations

import os
import socket
import uuid
from typing import AsyncGenerator
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("POSTGRES_PORT", "5433")

from backend.config import _flag_override_cache  # noqa: E402
from backend.models import ExperimentRun, MiningTask  # noqa: E402
from backend.services.task_service import TaskService  # noqa: E402


def _pg_reachable() -> bool:
    try:
        s = socket.create_connection(("localhost", 5433), timeout=1)
        s.close()
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_reachable(),
    reason="Postgres not reachable on localhost:5433",
)


_TAG = f"flat_f1_test_{uuid.uuid4().hex[:8]}"


@pytest_asyncio.fixture
async def pg_session() -> AsyncGenerator[AsyncSession, None]:
    """Live PG session; cleans up _TAG-prefixed mining_tasks + their runs."""
    from backend.config import settings

    engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI, echo=False)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as s:
            yield s
            try:
                # ExperimentRun has FK on mining_tasks — delete by task_name match
                await s.execute(text(
                    "DELETE FROM experiment_runs WHERE task_id IN "
                    "(SELECT id FROM mining_tasks WHERE task_name LIKE :p)"
                ), {"p": f"{_TAG}%"})
                await s.execute(text(
                    "DELETE FROM mining_tasks WHERE task_name LIKE :p"
                ), {"p": f"{_TAG}%"})
                await s.commit()
            except Exception:
                await s.rollback()
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def _clear_flag_cache():
    _flag_override_cache.clear()
    yield
    _flag_override_cache.clear()


@pytest.fixture(autouse=True)
def _mock_celery_dispatch():
    """Prevent real celery enqueue during tests — return a fake task id."""
    fake_task = MagicMock()
    fake_task.id = "fake-celery-id"
    with patch(
        "backend.tasks.run_mining_task.delay",
        return_value=fake_task,
    ) as m:
        yield m


# ---------------------------------------------------------------------------
# Test 1: Flag ON + start_flat_session creates FLAT task
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_flat_session_creates_flat_task(pg_session):
    """plan §5 (1): start_flat_session creates FLAT_CONTINUOUS task with cursor=0."""
    _flag_override_cache["ENABLE_FLAT_CONTINUOUS"] = True
    svc = TaskService(pg_session)

    info = await svc.start_flat_session(
        region="USA", universe="TOP3000", datasets=["pv1", "fundamental6"],
    )
    # rename for cleanup tagging
    task = await svc.task_repo.get_by_id(info.task_id)
    task.task_name = f"{_TAG}_{task.task_name}"
    await pg_session.commit()

    assert info.mining_mode == "FLAT_CONTINUOUS"
    assert info.status == "RUNNING"
    assert task.target_datasets == ["pv1", "fundamental6"]
    assert (task.config or {}).get("flat_cursor") == 0
    assert task.schedule == "ONESHOT"


# ---------------------------------------------------------------------------
# Test 2: Q1 V2 — flat_cursor preserved across resume
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_flat_resume_preserves_cursor(pg_session):
    """plan §5 (2) Q1 V2: resume_flat_session inherits runtime_state into new run."""
    _flag_override_cache["ENABLE_FLAT_CONTINUOUS"] = True
    svc = TaskService(pg_session)

    info = await svc.start_flat_session(region="USA", datasets=["pv1"])
    task = await svc.task_repo.get_by_id(info.task_id)
    task.task_name = f"{_TAG}_{task.task_name}"
    await pg_session.commit()
    task_id = info.task_id

    # Simulate worker progress: set cursor=5 on the first run
    first_run = await svc.run_repo.get_latest_by_task(task_id)
    assert first_run is not None
    first_run.runtime_state = {"flat_cursor": 5, "flat_total_alphas": 12}
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(first_run, "runtime_state")
    await pg_session.commit()

    # Pause the task
    await svc.task_repo.update_status(task_id, "PAUSED")
    await pg_session.commit()

    # Resume via FLAT-specific endpoint → should inherit runtime_state
    await svc.resume_flat_session(task_id)

    # The new (latest) ExperimentRun should have the inherited cursor
    new_run = await svc.run_repo.get_latest_by_task(task_id)
    assert new_run.id != first_run.id, "resume should create a NEW ExperimentRun"
    assert new_run.runtime_state.get("flat_cursor") == 5
    assert new_run.runtime_state.get("flat_total_alphas") == 12


# ---------------------------------------------------------------------------
# Test 3: Q1 V2 sanity — cascade resume does NOT inherit runtime_state
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cascade_resume_does_not_inherit_runtime_state(pg_session):
    """plan §5 (3) Q1 V2 sanity: cascade callers use default inherit=False."""
    svc = TaskService(pg_session)

    task = MiningTask(
        task_name=f"{_TAG}_cascade",
        region="EUR",                # USA cascade may be occupied by prod task
        universe="TOP2500",
        mining_mode="CONTINUOUS_CASCADE",
        cascade_phase="T1",
        cascade_round_idx=0,
        status="PAUSED",
        schedule="CASCADE",
        starting_tier=1,
    )
    created = await svc.task_repo.create(task)
    await pg_session.commit()
    task_id = created.id

    # Seed an old run with non-empty runtime_state
    old_run = ExperimentRun(
        task_id=task_id,
        status="COMPLETED",
        trigger_source="MINING_SESSION",
        runtime_state={"cascade_round_idx": 3, "should_not_leak": True},
    )
    pg_session.add(old_run)
    await pg_session.commit()

    # resume_session (cascade) — default inherit_runtime_state=False
    await svc.resume_session(task_id)

    new_run = await svc.run_repo.get_latest_by_task(task_id)
    assert new_run.id != old_run.id
    # Cascade callers do NOT inherit — runtime_state starts fresh (empty)
    assert (new_run.runtime_state or {}).get("should_not_leak") is None


# ---------------------------------------------------------------------------
# Test 4: Q2 A — intervene_task blocks FLAT mode (parametrized PAUSE+RESUME)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["PAUSE", "RESUME"])
async def test_intervene_task_blocks_flat_mode(pg_session, action):
    """plan §5 (4) Q2 A: intervene_task raises ValueError on FLAT_CONTINUOUS."""
    svc = TaskService(pg_session)

    task = MiningTask(
        task_name=f"{_TAG}_intervene_{action}",
        region="USA",
        universe="TOP3000",
        mining_mode="FLAT_CONTINUOUS",
        status="RUNNING" if action == "PAUSE" else "PAUSED",
        schedule="ONESHOT",
        starting_tier=1,
    )
    created = await svc.task_repo.create(task)
    await pg_session.commit()

    with pytest.raises(ValueError, match=r"FLAT_CONTINUOUS tasks use POST /ops/flat-sessions"):
        await svc.intervene_task(created.id, action)

    # Status should NOT have changed
    refreshed = await svc.task_repo.get_by_id(created.id)
    expected_status = "RUNNING" if action == "PAUSE" else "PAUSED"
    assert refreshed.status == expected_status


# ---------------------------------------------------------------------------
# Test 5: start_flat_session rejects unsupported region
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_flat_session_unknown_region_rejected(pg_session):
    """plan §5 (5): unsupported region raises ValueError regardless of flag."""
    _flag_override_cache["ENABLE_FLAT_CONTINUOUS"] = True
    svc = TaskService(pg_session)

    with pytest.raises(ValueError, match="not supported"):
        await svc.start_flat_session(region="MARS", datasets=[])


# ---------------------------------------------------------------------------
# Test 6: resume_flat_session rejects non-FLAT task
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resume_flat_session_rejects_cascade_task(pg_session):
    """plan §5 (6): resume_flat_session validates mining_mode."""
    svc = TaskService(pg_session)

    task = MiningTask(
        task_name=f"{_TAG}_cascade_misroute",
        region="CHN",                # avoid USA (occupied) and EUR (used in test_3)
        universe="TOP2000A",
        mining_mode="CONTINUOUS_CASCADE",
        cascade_phase="T1",
        cascade_round_idx=0,
        status="PAUSED",
        schedule="CASCADE",
        starting_tier=1,
    )
    created = await svc.task_repo.create(task)
    await pg_session.commit()

    with pytest.raises(ValueError, match="is not FLAT_CONTINUOUS"):
        await svc.resume_flat_session(created.id)
