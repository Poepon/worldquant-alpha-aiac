"""V-19.2 (2026-05-05) — per-row savepoint persistence regression tests.

The production tables (alphas, mining_tasks) use JSONB columns that don't
render under aiosqlite, so the SAVEPOINT semantics are exercised on a
minimal in-memory schema. The point of these tests is to lock in the
behavior `async with session.begin_nested(): session.add(...); await
session.flush()` rolls back ONLY the offending row when a UNIQUE constraint
fires — pre-V-19.2 a single batch commit aborted the entire batch.

Plus two unit tests for the file-based persistence_errors.log writer.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import Column, Integer, String, UniqueConstraint, select, func
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool


_TestBase = declarative_base()


class _Row(_TestBase):
    """Minimal table mimicking alphas' uq_alpha_id constraint."""
    __tablename__ = "v19_2_rows"
    id = Column(Integer, primary_key=True)
    alpha_id = Column(String(20))
    expression = Column(String(200))
    __table_args__ = (UniqueConstraint("alpha_id", name="uq_v19_2_alpha_id"),)


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(_TestBase.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_savepoint_isolates_duplicate_unique_key(session):
    """Insert 3 rows where the 2nd duplicates the 1st alpha_id. Pre-V-19.2 a
    single batch commit aborted all 3; with savepoints only #2 rolls back."""
    rows = [
        _Row(alpha_id="DUP_AAA", expression="ts_rank(close, 5)"),
        _Row(alpha_id="DUP_AAA", expression="ts_rank(volume, 5)"),  # duplicate
        _Row(alpha_id="UNIQ_BBB", expression="rank(returns)"),
    ]
    inserted = []
    rolled_back = []
    for row in rows:
        try:
            async with session.begin_nested():
                session.add(row)
                await session.flush()
            inserted.append(row.alpha_id)
        except Exception:
            rolled_back.append(row.alpha_id)

    await session.commit()

    assert inserted == ["DUP_AAA", "UNIQ_BBB"]
    assert rolled_back == ["DUP_AAA"]  # the 2nd one

    r = await session.execute(select(func.count()).select_from(_Row))
    assert r.scalar() == 2

    r = await session.execute(select(_Row.alpha_id).order_by(_Row.alpha_id))
    landed = [row[0] for row in r.fetchall()]
    assert landed == ["DUP_AAA", "UNIQ_BBB"]


@pytest.mark.asyncio
async def test_pre_v19_2_batch_commit_aborts_entire_batch(session):
    """Document the pre-V-19.2 behavior: without savepoints, ONE duplicate
    aborts the full batch. This is the exact failure mode V-19.2 fixes."""
    rows = [
        _Row(alpha_id="X1", expression="e1"),
        _Row(alpha_id="X1", expression="e2"),  # duplicate
        _Row(alpha_id="X2", expression="e3"),
    ]
    for row in rows:
        session.add(row)

    with pytest.raises(Exception):
        # IntegrityError fires on commit, not add()
        await session.commit()

    await session.rollback()

    r = await session.execute(select(func.count()).select_from(_Row))
    # All 3 rolled back — this is the exact bug V-19.2 fixes.
    assert r.scalar() == 0


def test_persistence_error_logger_writes_file(tmp_path, monkeypatch):
    import backend.agents.graph.persistence_errors as pe_mod
    test_log = tmp_path / "persistence_errors.log"
    monkeypatch.setattr(pe_mod, "_LOG_PATH", test_log)

    try:
        raise RuntimeError("synthetic V-19.2 test error")
    except RuntimeError as e:
        pe_mod.log_persistence_error(
            task_id=42,
            phase="alpha_insert",
            exc=e,
            alpha_id="TST123",
            expression="ts_rank(close, 20)",
            quality_status="PASS",
            extra={"factor_tier": 1, "dataset_id": "pv1"},
        )

    assert test_log.exists()
    content = test_log.read_text(encoding="utf-8")
    assert "phase=alpha_insert" in content
    assert "task=42" in content
    assert "alpha_id=TST123" in content
    assert "RuntimeError: synthetic V-19.2 test error" in content
    assert "factor_tier=1" in content


def test_persistence_error_logger_swallows_log_failures(monkeypatch, tmp_path):
    """Logger must NEVER raise — silent failure required so file-write bugs
    don't mask the original persistence error."""
    import backend.agents.graph.persistence_errors as pe_mod
    # Point at a path whose parent we cannot create (root-level disk path
    # we don't have permission to write to). The mkdir call inside should
    # raise, then be swallowed.
    monkeypatch.setattr(pe_mod, "_LOG_PATH", tmp_path / "nonexistent" / "deeply" / "nested" / "log.log")

    # Force mkdir to fail
    def _broken_mkdir(self, *args, **kwargs):
        raise OSError("disk full")

    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "mkdir", _broken_mkdir)

    try:
        raise ValueError("inner exception")
    except ValueError as e:
        # Must not raise
        pe_mod.log_persistence_error(task_id=1, phase="x", exc=e)
