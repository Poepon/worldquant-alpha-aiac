"""L1 dataset-category SET-overlap retrieval tests (2026-05-21 redesign).

Proves the fix for "step-1 knowledge retrieval returns identical content for
every dataset in a region": L1 now ranks candidates by the overlap between the
query's dataset-category set and each pattern's field-derived
``meta_data['dataset_categories_used']`` — and selects candidates relevance-first
so category-matching rows enter the pool regardless of recency.

L1/JSONB ``@>`` tests need real PG (aiosqlite can't do containment); they are
isolated from production rows by tagging seeded rows with a UNIQUE pillar so
only they match the pillar filter. Pure-logic + cache-key tests need no DB.
"""
from __future__ import annotations

import os
import socket
import uuid
from types import SimpleNamespace
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("POSTGRES_PORT", "5433")

from backend.agents.hierarchical_rag import (  # noqa: E402
    _l1_category_overlap,
    _make_layer_cache_key,
    _score_l1_success,
    _score_l1_pitfall,
    layer1_pillar,
)
from backend.models.knowledge import KnowledgeEntry, compute_pattern_hash  # noqa: E402
from backend.config import settings as _settings  # noqa: E402


def _pg_reachable() -> bool:
    try:
        s = socket.create_connection(("localhost", 5433), timeout=1)
        s.close()
        return True
    except OSError:
        return False


_TAG = f"l1cat_{uuid.uuid4().hex[:8]}"
_PIL = f"pil_{_TAG}"  # unique pillar → isolates seeded rows from production


# ===========================================================================
# Pure-logic tests (no DB)
# ===========================================================================

def test_category_overlap_counts_intersection():
    md = {"dataset_categories_used": ["pv", "fundamental"]}
    assert _l1_category_overlap(md, ["fundamental"]) == 1
    assert _l1_category_overlap(md, ["pv", "fundamental"]) == 2
    assert _l1_category_overlap(md, ["news"]) == 0
    assert _l1_category_overlap(md, []) == 0
    assert _l1_category_overlap({"dataset_categories_used": "notalist"}, ["pv"]) == 0
    assert _l1_category_overlap({}, ["pv"]) == 0


def test_score_success_overlap_beats_quality():
    """A category match (+CATEGORY_EXACT per overlap) must outrank a higher-quality
    non-matching row — overlap is the cross-dataset discriminator."""
    match = {"dataset_categories_used": ["fundamental"], "score": 0.1, "expected_sharpe": 0.5}
    nomatch_hi_quality = {"dataset_categories_used": ["pv"], "score": 1.0, "expected_sharpe": 2.0}
    s_match = _score_l1_success(match, query_categories=["fundamental"], dataset_id=None, region=None, settings=_settings)
    s_nomatch = _score_l1_success(nomatch_hi_quality, query_categories=["fundamental"], dataset_id=None, region=None, settings=_settings)
    assert s_match > s_nomatch


def test_score_success_exact_dataset_bonus():
    md = {"dataset_categories_used": ["pv"], "dataset": "pv1"}
    s_exact = _score_l1_success(md, query_categories=["pv"], dataset_id="pv1", region=None, settings=_settings)
    s_plain = _score_l1_success(md, query_categories=["pv"], dataset_id="pv9", region=None, settings=_settings)
    assert s_exact > s_plain


def test_score_pitfall_severity_and_overlap():
    hi = {"severity": "high", "dataset_categories_used": ["news"]}
    lo = {"severity": "low", "dataset_categories_used": []}
    assert _score_l1_pitfall(hi, query_categories=["news"], settings=_settings) > \
        _score_l1_pitfall(lo, query_categories=["news"], settings=_settings)


def test_cache_key_includes_dataset():
    """Two datasets sharing pillar/region must produce DISTINCT L1 cache keys —
    else the per-layer Redis cache masks the dataset-aware result."""
    base = {"expr": None, "pillar": "momentum", "region": "USA", "budget": 5}
    k_pv = _make_layer_cache_key("L1", {**base, "dataset": "pv1"})
    k_fund = _make_layer_cache_key("L1", {**base, "dataset": "fundamental6"})
    k_pv2 = _make_layer_cache_key("L1", {**base, "dataset": "pv1"})
    assert k_pv != k_fund
    assert k_pv == k_pv2


# ===========================================================================
# resolve_field_categories (mock db — canonical/dedup logic)
# ===========================================================================

@pytest.mark.asyncio
async def test_resolve_field_categories_canonical_dedup():
    from backend.agents.services.rag_service import resolve_field_categories
    result_obj = MagicMock()
    result_obj.scalars.return_value.all.return_value = ["pv", "PV", "fundamental", "weird_cat"]
    db = MagicMock()
    db.execute = AsyncMock(return_value=result_obj)
    cats = await resolve_field_categories("rank(close / assets)", "USA", db)
    # PV/pv dedup → pv; weird_cat → other; sorted
    assert cats == ["fundamental", "other", "pv"]


@pytest.mark.asyncio
async def test_resolve_field_categories_empty_inputs():
    from backend.agents.services.rag_service import resolve_field_categories
    db = MagicMock()
    db.execute = AsyncMock()
    assert await resolve_field_categories("", "USA", db) == []
    assert await resolve_field_categories("rank(close)", "", db) == []
    db.execute.assert_not_awaited()


# ===========================================================================
# L1 retrieval (PG) — isolated via unique pillar
# ===========================================================================

pg_only = pytest.mark.skipif(not _pg_reachable(), reason="Postgres not reachable on localhost:5433")


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


def _seed(pattern_suffix, *, entry_type="SUCCESS_PATTERN", cats=None, score=0.5, sharpe=1.0, pillar=_PIL, severity=None):
    pattern = f"rank({_TAG}_{pattern_suffix})"
    md = {"pillar_classified": pillar, "score": score, "expected_sharpe": sharpe}
    if cats is not None:
        md["dataset_categories_used"] = cats
    if severity:
        md["severity"] = severity
    return KnowledgeEntry(
        entry_type=entry_type, pattern=pattern,
        pattern_hash=compute_pattern_hash(pattern, None, None),
        description=f"{pattern_suffix} entry", meta_data=md,
        is_active=True, created_by="TEST",
    )


# Unique categories so the category pass (which spans ALL pillars) is isolated
# from production rows — only these seeds carry them.
_CAT_A = f"cat_a_{_TAG}"
_CAT_B = f"cat_b_{_TAG}"


@pg_only
@pytest.mark.asyncio
async def test_l1_category_overlap_ranks_matching_first(pg_session):
    pg_session.add(_seed("match", cats=[_CAT_A], score=0.1))   # matches, lowest quality
    pg_session.add(_seed("other", cats=[_CAT_B], score=0.9))   # higher quality, wrong cat
    await pg_session.commit()

    succ, _ = await layer1_pillar(
        pg_session, hypothesis_pillar=_PIL, query_categories=[_CAT_A], budget=5,
    )
    assert succ, "expected the category match to surface"
    # the category-matching row must rank first despite lowest quality
    assert succ[0].pattern == f"rank({_TAG}_match)"


@pg_only
@pytest.mark.asyncio
async def test_l1_multi_category_alpha_matches_either_query(pg_session):
    """A pattern whose fields span two categories must surface for EITHER
    category query — impossible with a single dataset_id key."""
    pg_session.add(_seed("multi", cats=[_CAT_A, _CAT_B], score=0.5))
    await pg_session.commit()

    for q in ([_CAT_A], [_CAT_B]):
        succ, _ = await layer1_pillar(
            pg_session, hypothesis_pillar=_PIL, query_categories=q, budget=5,
        )
        assert any(e.pattern == f"rank({_TAG}_multi)" for e in succ), f"missed for query {q}"


@pg_only
@pytest.mark.asyncio
async def test_l1_no_category_match_falls_back_to_quality_not_empty(pg_session):
    """Query category that nothing matches → NOT empty (pass-2 fill) and ranked
    by quality (not raw recency): the higher-score row comes first."""
    pg_session.add(_seed("lowq", cats=["pv"], score=0.2))
    pg_session.add(_seed("hiq", cats=["pv"], score=0.95))
    await pg_session.commit()

    # Query a category nothing has → pass-1 empty, pass-2 (pillar-scoped) fills
    # with the two seeds, quality-ranked (NOT raw recency).
    succ, _ = await layer1_pillar(
        pg_session, hypothesis_pillar=_PIL, query_categories=[f"none_{_TAG}"], budget=5,
    )
    assert len(succ) >= 2
    assert succ[0].pattern == f"rank({_TAG}_hiq)"


@pg_only
@pytest.mark.asyncio
async def test_l1_pillar_decoupled_retrieves_by_category_alone(pg_session):
    """No pillar at all → category-set overlap still drives retrieval (uses a
    unique category so only the seeded row matches, isolating from prod)."""
    uniq_cat = f"cat_{_TAG}"
    pg_session.add(_seed("decouple", cats=[uniq_cat], pillar=None, score=0.5))
    await pg_session.commit()

    succ, _ = await layer1_pillar(
        pg_session, hypothesis_pillar=None, query_categories=[uniq_cat], budget=5,
    )
    assert any(e.pattern == f"rank({_TAG}_decouple)" for e in succ)


@pg_only
@pytest.mark.asyncio
async def test_l1_no_signal_returns_empty(pg_session):
    """No pillar AND no categories → nothing to retrieve on → empty."""
    succ, fail = await layer1_pillar(pg_session, hypothesis_pillar=None, query_categories=None, budget=5)
    assert succ == [] and fail == []
