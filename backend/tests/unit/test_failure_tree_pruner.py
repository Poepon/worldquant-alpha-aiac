"""Phase 3 R1b.3 review LOW: failure_tree pruner tests (2026-05-18).

Verifies:
  - DELETEs FAILURE_PITFALL+failure_tree rows older than the cutoff
  - Skips FAILURE_PITFALL rows WITHOUT failure_tree meta (e.g. from
    ``negative_knowledge_extract``)
  - Skips rows newer than the cutoff
  - Soft-fail on DB error → returns 0, never raises

Uses pg_session (live PG) like ``test_rag_hierarchical_pr1.py`` since the
DELETE uses the Postgres JSONB ``?`` key-existence operator that aiosqlite
cannot emulate.
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

from backend.models.knowledge import KnowledgeEntry, compute_pattern_hash  # noqa: E402
from backend.tasks.failure_tree_pruner import prune_old_failure_tree_entries  # noqa: E402


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


_TAG = f"ftpruner_{uuid.uuid4().hex[:8]}"


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
                    "DELETE FROM knowledge_entries WHERE pattern LIKE :p"
                ), {"p": f"%{_TAG}%"})
                await s.commit()
            except Exception:
                await s.rollback()
    finally:
        await engine.dispose()


async def _seed_entry(
    session: AsyncSession,
    *,
    pattern: str,
    entry_type: str,
    meta_data: dict,
    created_at: datetime,
) -> int:
    """Insert a KnowledgeEntry and force-update created_at (server_default
    otherwise clobbers it to now()). Returns the new row id."""
    phash = compute_pattern_hash(pattern, None, None)
    entry = KnowledgeEntry(
        entry_type=entry_type,
        pattern=pattern,
        pattern_hash=phash,
        description="pruner test",
        meta_data=meta_data,
        is_active=True,
        created_by="TEST",
    )
    session.add(entry)
    await session.commit()
    # Override created_at via raw SQL (server_default fires on INSERT only,
    # but onupdate is set on updated_at, not created_at — safe).
    await session.execute(
        text("UPDATE knowledge_entries SET created_at = :ts WHERE id = :id"),
        {"ts": created_at, "id": entry.id},
    )
    await session.commit()
    return int(entry.id)


@pytest.mark.asyncio
async def test_prune_old_failure_tree_entries_deletes_matching(pg_session):
    """Seed an old FAILURE_PITFALL with failure_tree → pruner deletes it."""
    old_ts = datetime.now(timezone.utc) - timedelta(days=120)
    eid = await _seed_entry(
        pg_session,
        pattern=f"R1B_FAILURE_TREE: {_TAG}_old",
        entry_type="FAILURE_PITFALL",
        meta_data={"failure_tree": {"statement": "old root", "children": []},
                   "source": "r1b_loop"},
        created_at=old_ts,
    )

    deleted = await prune_old_failure_tree_entries(days=90)
    assert deleted >= 1

    # Verify the row is gone
    row = (await pg_session.execute(
        text("SELECT id FROM knowledge_entries WHERE id = :id"),
        {"id": eid},
    )).first()
    assert row is None, "old failure_tree row should be deleted"


@pytest.mark.asyncio
async def test_prune_skips_non_failure_tree_entries(pg_session):
    """FAILURE_PITFALL WITHOUT failure_tree meta → NOT deleted (out of scope:
    e.g. rows from negative_knowledge_extract)."""
    old_ts = datetime.now(timezone.utc) - timedelta(days=120)
    eid = await _seed_entry(
        pg_session,
        pattern=f"FAILURE_NO_TREE: {_TAG}_other",
        entry_type="FAILURE_PITFALL",
        meta_data={"source": "negative_knowledge_extract",
                   "category": "high_turnover"},  # NO failure_tree key
        created_at=old_ts,
    )

    await prune_old_failure_tree_entries(days=90)

    row = (await pg_session.execute(
        text("SELECT id FROM knowledge_entries WHERE id = :id"),
        {"id": eid},
    )).first()
    assert row is not None, (
        "FAILURE_PITFALL without meta_data->'failure_tree' must NOT be deleted"
    )


@pytest.mark.asyncio
async def test_prune_skips_recent_entries(pg_session):
    """failure_tree row newer than retention window → NOT deleted."""
    recent_ts = datetime.now(timezone.utc) - timedelta(days=10)
    eid = await _seed_entry(
        pg_session,
        pattern=f"R1B_FAILURE_TREE: {_TAG}_recent",
        entry_type="FAILURE_PITFALL",
        meta_data={"failure_tree": {"statement": "recent", "children": []},
                   "source": "r1b_loop"},
        created_at=recent_ts,
    )

    await prune_old_failure_tree_entries(days=90)

    row = (await pg_session.execute(
        text("SELECT id FROM knowledge_entries WHERE id = :id"),
        {"id": eid},
    )).first()
    assert row is not None, "recent failure_tree row must NOT be deleted"


@pytest.mark.asyncio
async def test_prune_soft_fail_on_db_error():
    """DB session open raises → pruner returns 0 without re-raising."""
    class _Boom:
        def __call__(self):  # AsyncSessionLocal() invocation
            raise RuntimeError("simulated PG outage")

    with patch("backend.database.AsyncSessionLocal", _Boom()):
        result = await prune_old_failure_tree_entries(days=90)
    assert result == 0
