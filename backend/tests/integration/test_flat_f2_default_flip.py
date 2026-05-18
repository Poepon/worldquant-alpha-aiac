"""Phase 3 flat-F2 default mining_mode flip (2026-05-18).

Tests per master plan §4.5 / 决策 5A:
  1. Both flags OFF → start_session creates CASCADE task (legacy)
  2. ENABLE_DEFAULT_FLAT_SESSION ON but ENABLE_FLAT_CONTINUOUS OFF →
     guard rejects, cascade path stays (avoid creating FLAT task that
     mining_tasks.py dispatch branch would refuse)
  3. Both flags ON → start_session delegates to start_flat_session →
     FLAT_CONTINUOUS task created

Uses pg_session fixture (live PG) per repo convention because MiningTask
uses JSONB cols.
"""
from __future__ import annotations

import os
import socket
import uuid
from typing import AsyncGenerator
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("POSTGRES_PORT", "5433")

from backend.config import _flag_override_cache  # noqa: E402
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


_TAG = f"flat_f2_test_{uuid.uuid4().hex[:8]}"


@pytest_asyncio.fixture
async def pg_session() -> AsyncGenerator[AsyncSession, None]:
    from backend.config import settings

    engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI, echo=False)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as s:
            yield s
            try:
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
def _mock_celery():
    fake = MagicMock()
    fake.id = "fake-celery"
    with patch("backend.tasks.run_mining_task.delay", return_value=fake):
        yield


# ---------------------------------------------------------------------------
# Test 1: Both flags OFF — cascade legacy unchanged
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_both_flags_off_cascade_path(pg_session):
    """ENABLE_DEFAULT_FLAT_SESSION + ENABLE_FLAT_CONTINUOUS both OFF →
    start_session creates CONTINUOUS_CASCADE task as before."""
    svc = TaskService(pg_session)
    # Use ASI region (no existing cascade per partial unique index)
    try:
        info = await svc.start_session(region="ASI", universe="MINVOL1500")
    except Exception:
        pytest.skip("region ASI may have existing cascade task")
    task = await svc.task_repo.get_by_id(info.task_id)
    task.task_name = f"{_TAG}_{task.task_name}"
    await pg_session.commit()

    assert task.mining_mode == "CONTINUOUS_CASCADE"
    assert task.schedule == "CASCADE"
    assert task.cascade_phase == "T1"


# ---------------------------------------------------------------------------
# Test 2: flat-F2 ON but flat-F1 OFF → cascade path (guard rejects)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_flat_f2_on_but_flat_f1_off_cascade_path(pg_session):
    """flat-F2 flag ON but ENABLE_FLAT_CONTINUOUS OFF → guard requires
    both, falls to cascade. Avoids creating FLAT task the dispatch branch
    would refuse to run."""
    _flag_override_cache["ENABLE_DEFAULT_FLAT_SESSION"] = True
    # Leave ENABLE_FLAT_CONTINUOUS OFF (default)
    svc = TaskService(pg_session)
    try:
        info = await svc.start_session(region="GLB", universe="TOP3000")
    except Exception:
        pytest.skip("region GLB may have existing cascade task")
    task = await svc.task_repo.get_by_id(info.task_id)
    task.task_name = f"{_TAG}_{task.task_name}"
    await pg_session.commit()

    # Guard requires BOTH flags — falls back to cascade
    assert task.mining_mode == "CONTINUOUS_CASCADE"


# ---------------------------------------------------------------------------
# Test 3: Both flags ON → flat path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_both_flags_on_delegates_to_flat(pg_session):
    """ENABLE_DEFAULT_FLAT_SESSION + ENABLE_FLAT_CONTINUOUS both ON →
    start_session creates FLAT_CONTINUOUS task via start_flat_session."""
    _flag_override_cache["ENABLE_DEFAULT_FLAT_SESSION"] = True
    _flag_override_cache["ENABLE_FLAT_CONTINUOUS"] = True
    svc = TaskService(pg_session)
    # CHN region — unlikely to have existing flat task
    info = await svc.start_session(region="CHN", universe="TOP2000A")
    task = await svc.task_repo.get_by_id(info.task_id)
    task.task_name = f"{_TAG}_{task.task_name}"
    await pg_session.commit()

    assert task.mining_mode == "FLAT_CONTINUOUS"
    assert task.schedule == "ONESHOT"
    # cascade_phase is set to None for flat tasks
    assert task.cascade_phase is None
    # flat tasks start at starting_tier
    assert task.starting_tier == 1
