"""RAG A/B gate — layer1_pillar 'control' arm suppresses category-overlap (2026-05-21).

The 'control' arm must behave like pre-P0 (no dataset-category derivation), the
'category'/'' arms keep the P0 overlap. Isolated from production rows via a unique
pillar. Needs real PG (JSONB @>).
"""
from __future__ import annotations

import os
import socket
import uuid
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("POSTGRES_PORT", "5433")

from backend.agents.hierarchical_rag import layer1_pillar  # noqa: E402
from backend.models.knowledge import KnowledgeEntry, compute_pattern_hash  # noqa: E402
from backend.config import settings as _settings  # noqa: E402


def _pg() -> bool:
    try:
        s = socket.create_connection(("localhost", 5433), timeout=1)
        s.close()
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _pg(), reason="Postgres not reachable on localhost:5433")

_TAG = f"l1ab_{uuid.uuid4().hex[:8]}"
_PIL = f"pil_{_TAG}"


@pytest_asyncio.fixture
async def pg_session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine(_settings.SQLALCHEMY_DATABASE_URI, echo=False)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as s:
            yield s
            try:
                await s.execute(text("DELETE FROM knowledge_entries WHERE pattern LIKE :p"), {"p": f"%{_TAG}%"})
                await s.commit()
            except Exception:
                await s.rollback()
    finally:
        await engine.dispose()


def _seed(suffix, cats, score):
    pat = f"rank({_TAG}_{suffix})"
    return KnowledgeEntry(
        entry_type="SUCCESS_PATTERN", pattern=pat,
        pattern_hash=compute_pattern_hash(pat, None, None),
        description=f"{suffix}", is_active=True, created_by="TEST",
        meta_data={"pillar_classified": _PIL, "score": score, "expected_sharpe": 1.0,
                   **({"dataset_categories_used": cats} if cats else {})},
    )


# Robust isolation: control arm suppresses the dataset→category derivation, so
# pass-1 (category, cross-pillar) never fires → only the pillar-scoped pass-2
# (our unique _PIL rows) is returned. The category/'' arm DOES derive
# 'fundamental' and pass-1 spans ALL pillars → production fundamental rows leak
# in (rows NOT tagged with _TAG). That presence/absence is the gate signal,
# independent of score ordering vs production.

@pytest.mark.asyncio
async def test_control_arm_returns_only_pillar_scoped_rows(pg_session):
    pg_session.add(_seed("a", ["fundamental"], 0.5))
    pg_session.add(_seed("b", None, 0.5))
    await pg_session.commit()
    succ, _ = await layer1_pillar(
        pg_session, hypothesis_pillar=_PIL, dataset_id="fundamental6",
        rag_ab_arm="control", budget=10,
    )
    assert succ, "control arm should still return the pillar-scoped seeds"
    # category derivation suppressed → no cross-pillar fundamental rows
    assert all(_TAG in e.pattern for e in succ), (
        f"control leaked non-pillar rows: {[e.pattern[:40] for e in succ if _TAG not in e.pattern][:3]}"
    )


@pytest.mark.asyncio
async def test_category_arm_derives_and_spans_pillars(pg_session):
    pg_session.add(_seed("c", ["fundamental"], 0.5))
    await pg_session.commit()
    succ, _ = await layer1_pillar(
        pg_session, hypothesis_pillar=_PIL, dataset_id="fundamental6",
        rag_ab_arm="category", budget=10,
    )
    # qcats='fundamental' derived → pass-1 spans ALL pillars → production
    # fundamental rows (not _TAG) appear, proving the derivation fired.
    assert any(_TAG not in e.pattern for e in succ), (
        "category arm should pull cross-pillar fundamental rows (derivation fired)"
    )


@pytest.mark.asyncio
async def test_empty_arm_behaves_like_category(pg_session):
    """arm '' (flag OFF / no A/B) keeps P0 category behavior (derivation fires)."""
    pg_session.add(_seed("d", ["fundamental"], 0.5))
    await pg_session.commit()
    succ, _ = await layer1_pillar(
        pg_session, hypothesis_pillar=_PIL, dataset_id="fundamental6",
        rag_ab_arm="", budget=10,
    )
    assert any(_TAG not in e.pattern for e in succ), (
        "empty arm should behave like category (cross-pillar derivation)"
    )
