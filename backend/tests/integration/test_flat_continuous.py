"""Integration: flat-F1 FLAT_CONTINUOUS mining mode (Phase 3, plan v1.5).

Tests the GO criteria from plan v1.5 §5:
  1. Flag ON + start_flat_session creates FLAT task
  2. Q1 V2: flat_cursor preserved across resume (inherit_runtime_state=True)
  3. [REMOVED in phase15-D PR3e cleanup] cascade resume sanity test —
     cascade path retired
  4. Q2 A: intervene_task blocks FLAT mode (PAUSE + RESUME)
  5. start_flat_session rejects unsupported region
  6. resume_flat_session rejects non-FLAT task (DISCRETE since cascade
     retired)

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
    """plan §5 (1): start_flat_session creates FLAT task with cursor=0.

    Post tier-system removal (Ship #7) mining_mode column is dropped — the
    flat marker is now task.schedule == 'FLAT' (per TaskService.start_flat_session).
    """
    _flag_override_cache["ENABLE_FLAT_CONTINUOUS"] = True
    svc = TaskService(pg_session)

    info = await svc.start_flat_session(
        region="USA", universe="TOP3000", datasets=["pv1", "fundamental6"],
    )
    # rename for cleanup tagging
    task = await svc.task_repo.get_by_id(info.task_id)
    task.task_name = f"{_TAG}_{task.task_name}"
    await pg_session.commit()

    assert info.status == "RUNNING"
    assert task.target_datasets == ["pv1", "fundamental6"]
    assert (task.config or {}).get("flat_cursor") == 0
    assert task.schedule == "FLAT"


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
# Test 3 removed (phase15-D PR3e cleanup, 2026-05-18): cascade resume path
# is retired — svc.resume_session was deleted in PR3e along with the
# cascade_phase / cascade_round_idx ORM columns (PR3b). The Q1 V2 "cascade
# callers use default inherit=False" sanity assertion is now vacuous.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Test 2b: pause_flat_session (2026-05-20) — FLAT manual pause endpoint
# counterpart to resume, fixing the frontend 恢复/暂停 button 400 on FLAT.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pause_flat_session_running_to_paused(pg_session):
    """pause_flat_session sets RUNNING→PAUSED without worker dispatch; the
    flat worker self-exits at the next round boundary (mirrors quota_guard)."""
    svc = TaskService(pg_session)

    task = MiningTask(
        task_name=f"{_TAG}_pause_running",
        region="USA",
        universe="TOP3000",
        status="RUNNING",
        schedule="FLAT",
    )
    created = await svc.task_repo.create(task)
    await pg_session.commit()

    info = await svc.pause_flat_session(created.id)
    assert info.status == "PAUSED"

    refreshed = await svc.task_repo.get_by_id(created.id)
    assert refreshed.status == "PAUSED"


@pytest.mark.asyncio
async def test_pause_flat_session_rejects_non_flat(pg_session):
    """pause_flat_session validates schedule == FLAT."""
    svc = TaskService(pg_session)

    task = MiningTask(
        task_name=f"{_TAG}_pause_nonflat",
        region="USA",
        universe="TOP3000",
        status="RUNNING",
        schedule="ONESHOT",
    )
    created = await svc.task_repo.create(task)
    await pg_session.commit()

    with pytest.raises(ValueError, match=r"not a FLAT session"):
        await svc.pause_flat_session(created.id)


@pytest.mark.asyncio
async def test_pause_flat_session_rejects_non_running(pg_session):
    """pause_flat_session refuses to pause a task that isn't RUNNING
    (e.g. already PAUSED returns idempotently; STOPPED raises)."""
    svc = TaskService(pg_session)

    # Already PAUSED → idempotent return (no error)
    paused = MiningTask(
        task_name=f"{_TAG}_pause_idempotent",
        region="USA", universe="TOP3000",
        status="PAUSED", schedule="FLAT",
    )
    created_p = await svc.task_repo.create(paused)
    await pg_session.commit()
    info = await svc.pause_flat_session(created_p.id)
    assert info.status == "PAUSED"

    # STOPPED → ValueError
    stopped = MiningTask(
        task_name=f"{_TAG}_pause_stopped",
        region="USA", universe="TOP3000",
        status="STOPPED", schedule="FLAT",
    )
    created_s = await svc.task_repo.create(stopped)
    await pg_session.commit()
    with pytest.raises(ValueError, match=r"cannot pause from status"):
        await svc.pause_flat_session(created_s.id)


# ---------------------------------------------------------------------------
# Test 4: Q2 A — intervene_task blocks FLAT mode (parametrized PAUSE+RESUME)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["PAUSE", "RESUME"])
async def test_intervene_task_blocks_flat_mode(pg_session, action):
    """intervene_task raises ValueError on FLAT-scheduled tasks (post tier-
    removal the marker is task.schedule == 'FLAT')."""
    svc = TaskService(pg_session)

    task = MiningTask(
        task_name=f"{_TAG}_intervene_{action}",
        region="USA",
        universe="TOP3000",
        status="RUNNING" if action == "PAUSE" else "PAUSED",
        schedule="FLAT",
    )
    created = await svc.task_repo.create(task)
    await pg_session.commit()

    with pytest.raises(ValueError, match=r"FLAT sessions use POST /ops/flat-sessions"):
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
async def test_resume_flat_session_rejects_non_flat_task(pg_session):
    """resume_flat_session validates schedule (post tier-removal — was
    mining_mode pre-Ship-#7)."""
    svc = TaskService(pg_session)

    task = MiningTask(
        task_name=f"{_TAG}_discrete_misroute",
        region="CHN",
        universe="TOP2000A",
        status="PAUSED",
        schedule="ONESHOT",
    )
    created = await svc.task_repo.create(task)
    await pg_session.commit()

    with pytest.raises(ValueError, match="is not a FLAT session"):
        await svc.resume_flat_session(created.id)
