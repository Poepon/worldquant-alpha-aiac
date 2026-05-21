"""record_success_pattern provenance + reconcile gate tests (2026-05-20).

Real-ORM tests (per [[feedback_orm_constructor_real_test]]) since the source
change touches the KnowledgeEntry(...) constructor meta_data dict.
"""
from __future__ import annotations

import pytest


def _session_maker(async_engine):
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import sessionmaker
    return sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.mark.asyncio
async def test_record_success_pattern_stamps_provenance_source(async_engine, monkeypatch):
    """source='sync_reconcile' must land in meta_data['source'] + sources."""
    from sqlalchemy import select
    from backend.agents.services.rag_service import RAGService
    from backend.models import KnowledgeEntry

    sm = _session_maker(async_engine)
    monkeypatch.setattr("backend.database.AsyncSessionLocal", sm)

    rag = RAGService(None)
    ok = await rag.record_success_pattern(
        expression="group_neutralize(ts_arg_max(close, 60), industry)",
        metrics={"sharpe": 1.6, "fitness": 1.3, "turnover": 0.2},
        region="USA", dataset_id="pv1", alpha_id="X1",
        source="sync_reconcile",
    )
    assert ok is True

    async with sm() as db:
        row = (
            await db.execute(
                select(KnowledgeEntry).where(KnowledgeEntry.entry_type == "SUCCESS_PATTERN")
            )
        ).scalar_one()
    assert row.meta_data["source"] == "sync_reconcile"
    assert "sync_reconcile" in row.meta_data["sources"]
    assert row.meta_data["alpha_id"] == "X1"


@pytest.mark.asyncio
async def test_record_success_pattern_default_source_unchanged(async_engine, monkeypatch):
    """Existing callers (no source kwarg) keep 'feedback_loop' — backward compat."""
    from sqlalchemy import select
    from backend.agents.services.rag_service import RAGService
    from backend.models import KnowledgeEntry

    sm = _session_maker(async_engine)
    monkeypatch.setattr("backend.database.AsyncSessionLocal", sm)

    rag = RAGService(None)
    await rag.record_success_pattern(
        expression="group_zscore(ts_rank(returns, 20), sector)",
        metrics={"sharpe": 1.5, "fitness": 1.2, "turnover": 0.25},
        region="USA",
    )

    async with sm() as db:
        row = (
            await db.execute(
                select(KnowledgeEntry).where(KnowledgeEntry.entry_type == "SUCCESS_PATTERN")
            )
        ).scalar_one()
    assert row.meta_data["source"] == "feedback_loop"


@pytest.mark.asyncio
async def test_record_success_pattern_dedups_by_skeleton_and_tracks_sources(
    async_engine, monkeypatch
):
    """Two alphas with the same skeleton → one entry, usage_count bumped,
    both contributing sources tracked."""
    from sqlalchemy import select
    from backend.agents.services.rag_service import RAGService
    from backend.models import KnowledgeEntry

    sm = _session_maker(async_engine)
    monkeypatch.setattr("backend.database.AsyncSessionLocal", sm)

    rag = RAGService(None)
    # first: mining feedback_loop
    await rag.record_success_pattern(
        expression="group_neutralize(ts_arg_max(close, 60), industry)",
        metrics={"sharpe": 1.6, "fitness": 1.3, "turnover": 0.2},
        region="USA", source="feedback_loop",
    )
    # second: same skeleton (different field/window → same skeleton) via reconcile
    await rag.record_success_pattern(
        expression="group_neutralize(ts_arg_max(volume, 40), sector)",
        metrics={"sharpe": 1.4, "fitness": 1.1, "turnover": 0.3},
        region="USA", source="sync_reconcile",
    )

    async with sm() as db:
        rows = (
            await db.execute(
                select(KnowledgeEntry).where(KnowledgeEntry.entry_type == "SUCCESS_PATTERN")
            )
        ).scalars().all()
    assert len(rows) == 1, "same skeleton must dedup to a single entry"
    entry = rows[0]
    assert entry.usage_count == 2
    assert set(entry.meta_data["sources"]) == {"feedback_loop", "sync_reconcile"}


def test_reconcile_gate_nesting_filter():
    """Generic single-op skeletons (nesting < 2) are filtered; compound ones pass.
    Mirrors the gate in scripts/reconcile_success_patterns.py."""
    from backend.knowledge_extraction import expression_to_skeleton

    generic = expression_to_skeleton("ts_arg_max(close, 60)")
    compound = expression_to_skeleton("group_neutralize(ts_arg_max(close, 60), industry)")

    assert generic.count("(") < 2          # filtered out as noise
    assert compound.count("(") >= 2        # qualifies for ingestion
