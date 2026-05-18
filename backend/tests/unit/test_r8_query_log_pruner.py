"""Phase 3 R8 query telemetry review LOW: r8_query_log pruner tests (2026-05-18).

Verifies:
  - DELETEs r8_query_log rows older than the cutoff
  - Skips rows newer than the cutoff
  - Soft-fail on DB error → returns 0, never raises

Uses pg_session (live PG) — mirrors ``test_failure_tree_pruner.py`` even
though aiosqlite COULD handle a plain ``created_at < :cutoff`` predicate;
keeping the same fixture style for consistency with the sibling pruner.
"""
from __future__ import annotations

import os
import socket
import uuid
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("POSTGRES_PORT", "5433")

from backend.models.r8_query_log import R8QueryLog  # noqa: E402
from backend.tasks.r8_query_log_pruner import prune_old_r8_query_log_entries  # noqa: E402


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


_TAG = f"r8pruner_{uuid.uuid4().hex[:8]}"


@pytest_asyncio.fixture
async def pg_session() -> AsyncGenerator[AsyncSession, None]:
    from backend.config import settings
    engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI, echo=False)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as s:
            yield s
            try:
                # Clean up any rows whose dataset_id was tagged for this run
                await s.execute(text(
                    "DELETE FROM r8_query_log WHERE dataset_id LIKE :p"
                ), {"p": f"%{_TAG}%"})
                await s.commit()
            except Exception:
                await s.rollback()
    finally:
        await engine.dispose()


async def _seed_row(
    session: AsyncSession,
    *,
    dataset_id: str,
    created_at: datetime,
) -> int:
    """Insert an R8QueryLog row and force-update created_at (server_default
    otherwise clobbers it to now()). Returns the new row id."""
    row = R8QueryLog(
        task_id=None,
        region="USA",
        dataset_id=dataset_id,
        current_expression_hash=None,
        layer_hits={"L0_exact": 1},
        total_queries=1,
        cache_hit=False,
        had_failure_tree_elevation=False,
    )
    session.add(row)
    await session.commit()
    await session.execute(
        text("UPDATE r8_query_log SET created_at = :ts WHERE id = :id"),
        {"ts": created_at, "id": row.id},
    )
    await session.commit()
    return int(row.id)


@pytest.mark.asyncio
async def test_prune_old_r8_query_log_entries_deletes_matching(pg_session):
    """Seed an old r8_query_log row → pruner deletes it."""
    old_ts = datetime.now(timezone.utc) - timedelta(days=120)
    rid = await _seed_row(
        pg_session,
        dataset_id=f"{_TAG}_old",
        created_at=old_ts,
    )

    deleted = await prune_old_r8_query_log_entries(days=90)
    assert deleted >= 1

    row = (await pg_session.execute(
        text("SELECT id FROM r8_query_log WHERE id = :id"),
        {"id": rid},
    )).first()
    assert row is None, "old r8_query_log row should be deleted"


@pytest.mark.asyncio
async def test_prune_skips_recent_entries(pg_session):
    """Recent r8_query_log row → NOT deleted."""
    recent_ts = datetime.now(timezone.utc) - timedelta(days=10)
    rid = await _seed_row(
        pg_session,
        dataset_id=f"{_TAG}_recent",
        created_at=recent_ts,
    )

    await prune_old_r8_query_log_entries(days=90)

    row = (await pg_session.execute(
        text("SELECT id FROM r8_query_log WHERE id = :id"),
        {"id": rid},
    )).first()
    assert row is not None, "recent r8_query_log row must NOT be deleted"


@pytest.mark.asyncio
async def test_prune_soft_fail_on_db_error():
    """DB session open raises → pruner returns 0 without re-raising."""
    class _Boom:
        def __call__(self):  # AsyncSessionLocal() invocation
            raise RuntimeError("simulated PG outage")

    with patch("backend.database.AsyncSessionLocal", _Boom()):
        result = await prune_old_r8_query_log_entries(days=90)
    assert result == 0
