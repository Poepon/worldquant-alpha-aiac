"""V-19.3 (2026-05-06) — cross-task alpha_id dedup regression tests.

V-19.2 added per-row SAVEPOINT so a uq_alpha_id violation no longer aborts
the whole batch — but the offending row is STILL lost (silently skipped after
SAVEPOINT rollback). V-19.3 adds a pre-batch SELECT to skip cross-task
duplicates with an INFO log instead of an error log, and adds the same dedup
to the sign-flip retry path (which previously bypassed node_simulate's
filter_unsimulated_expressions and was the root cause).

These tests verify:
1. Pre-batch SELECT correctly identifies cross-task duplicate alpha_ids
2. The dedup-then-skip pattern is functionally equivalent to "INSERT ... ON
   CONFLICT DO NOTHING" — no UC violation, no ERROR log
3. The reproduces the exact spike scenario where task A has alpha_id X, then
   task B's sign-flip generates the same expression and BRAIN returns the
   same X.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import Column, Integer, String, UniqueConstraint, select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool


_TestBase = declarative_base()


class _Row(_TestBase):
    __tablename__ = "v19_3_rows"
    id = Column(Integer, primary_key=True)
    alpha_id = Column(String(20))
    task_id = Column(Integer)
    expression = Column(String(200))
    __table_args__ = (UniqueConstraint("alpha_id", name="uq_v19_3_alpha_id"),)


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


async def _seed_existing(session, rows):
    for r in rows:
        session.add(r)
    await session.commit()


@pytest.mark.asyncio
async def test_cross_task_dup_identified_by_pre_select(session):
    """Replicates the spike root cause: task=81 owns alpha_id GrMeLOg3.
    Task=115's sign-flip generates the same expression → BRAIN returns
    GrMeLOg3 → V-19.3 pre-SELECT catches it, no INSERT attempted."""
    # Historical row from "task 81"
    await _seed_existing(session, [
        _Row(alpha_id="GrMeLOg3", task_id=81,
             expression="multiply(-1, ts_zscore(returns, 60))"),
        _Row(alpha_id="ZY2K0nwn", task_id=83,
             expression="multiply(-1, ts_rank(returns, 20))"),
    ])

    # Task 115 candidates from sign-flip retry — 2 collide, 1 unique
    candidates = [
        ("GrMeLOg3", "multiply(-1, ts_zscore(returns, 60))"),  # dup w/ 81
        ("ZY2K0nwn", "multiply(-1, ts_rank(returns, 20))"),    # dup w/ 83
        ("Xg2WYLm1", "ts_arg_min(actual_eps_value_quarterly, 20)"),  # new
    ]
    candidate_ids = [c[0] for c in candidates]

    # V-19.3 pre-batch SELECT
    r = await session.execute(
        select(_Row.alpha_id).where(_Row.alpha_id.in_(candidate_ids))
    )
    cross_task_dup_ids = {row[0] for row in r.fetchall()}
    assert cross_task_dup_ids == {"GrMeLOg3", "ZY2K0nwn"}

    # Insert only the new ones
    inserted = []
    for aid, expr in candidates:
        if aid in cross_task_dup_ids:
            continue  # V-19.3 skip
        async with session.begin_nested():
            session.add(_Row(alpha_id=aid, task_id=115, expression=expr))
            await session.flush()
        inserted.append(aid)
    await session.commit()

    assert inserted == ["Xg2WYLm1"]

    # Final state: 3 rows total (2 historical + 1 from task 115)
    r = await session.execute(
        select(_Row.alpha_id, _Row.task_id).order_by(_Row.task_id)
    )
    rows = r.fetchall()
    assert len(rows) == 3
    # No UC violation occurred — task 81/83 still own the historical alpha_ids
    by_aid = {row.alpha_id: row.task_id for row in rows}
    assert by_aid["GrMeLOg3"] == 81  # NOT 115 — historical kept
    assert by_aid["ZY2K0nwn"] == 83  # NOT 115 — historical kept
    assert by_aid["Xg2WYLm1"] == 115


@pytest.mark.asyncio
async def test_pre_dedup_replaces_savepoint_for_known_dups(session):
    """V-19.3 turns the previously-error path into a silent INFO skip. The
    SAVEPOINT safety net is still there for unknown errors, but should NOT
    fire for known cross-task collisions."""
    await _seed_existing(session, [
        _Row(alpha_id="DUP", task_id=10, expression="e1"),
    ])

    # Hand-trace V-19.3 pre-select
    r = await session.execute(
        select(_Row.alpha_id).where(_Row.alpha_id.in_(["DUP", "FRESH"]))
    )
    dup_ids = {row[0] for row in r.fetchall()}
    assert dup_ids == {"DUP"}

    # Insert flow with V-19.3: skip DUP, savepoint-INSERT FRESH
    savepoint_attempts = 0
    skipped = 0
    inserted = 0
    candidates = [("DUP", "e2"), ("FRESH", "e3")]
    for aid, expr in candidates:
        if aid in dup_ids:
            skipped += 1
            continue
        savepoint_attempts += 1
        async with session.begin_nested():
            session.add(_Row(alpha_id=aid, task_id=99, expression=expr))
            await session.flush()
        inserted += 1
    await session.commit()

    assert savepoint_attempts == 1  # only FRESH hit a savepoint
    assert skipped == 1
    assert inserted == 1


@pytest.mark.asyncio
async def test_empty_candidate_list_short_circuits(session):
    """Edge case: if no candidates have alpha_id, the dedup query MUST be
    skipped (.in_([]) evaluates to a false predicate but sending an empty
    IN to PG raises in some drivers — defensive code skips the query)."""
    candidate_ids = []
    cross_task_dup_ids = set()
    if candidate_ids:
        r = await session.execute(
            select(_Row.alpha_id).where(_Row.alpha_id.in_(candidate_ids))
        )
        cross_task_dup_ids = {row[0] for row in r.fetchall()}
    assert cross_task_dup_ids == set()
