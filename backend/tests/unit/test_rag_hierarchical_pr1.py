"""Phase 3 R8 PR1 unit tests for hierarchical RAG helpers (2026-05-18).

PR1 scope: L0 exact_match + L3 field_level + extract_fields_for_rag +
RAGEntry/RAGResult dataclasses. PR2/PR3 will add L1 + L2 + orchestrator
tests.

Uses pg_session (live PG) since KnowledgeEntry has JSONB meta_data +
INSERT/SELECT exercise.
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

from backend.agents.hierarchical_rag import (  # noqa: E402
    DECAYED_KEY,
    RAGEntry,
    RAGResult,
    extract_fields_for_rag,
    layer0_exact_match,
    layer3_field_level,
)
from backend.models.knowledge import KnowledgeEntry, compute_pattern_hash  # noqa: E402


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


_TAG = f"r8_test_{uuid.uuid4().hex[:8]}"


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


# ---------------------------------------------------------------------------
# extract_fields_for_rag (pure, no DB)
# ---------------------------------------------------------------------------

def test_extract_fields_basic():
    fields = extract_fields_for_rag("rank(ts_mean(close, 20))")
    assert "close" in fields
    assert "rank" not in fields  # known op
    assert "ts_mean" not in fields  # known op


def test_extract_fields_multiple():
    fields = extract_fields_for_rag("ts_corr(close, volume, 20) - ts_mean(returns, 5)")
    assert set(fields) == {"close", "volume", "returns"}


def test_extract_fields_excludes_numbers():
    """Pure numeric tokens not returned as fields."""
    fields = extract_fields_for_rag("rank(close) + 0.5")
    assert "0" not in fields
    assert "5" not in fields
    assert fields == ["close"]


def test_extract_fields_empty():
    assert extract_fields_for_rag("") == []
    assert extract_fields_for_rag(None) == []  # type: ignore


def test_extract_fields_case_normalizes():
    """All output lowercase."""
    fields = extract_fields_for_rag("rank(CLOSE)")
    assert fields == ["close"]


def test_extract_fields_dedupe():
    fields = extract_fields_for_rag("ts_corr(close, close, 20)")
    assert fields == ["close"]  # deduped


def test_extract_fields_sorted():
    fields = extract_fields_for_rag("ts_corr(volume, close, 20)")
    assert fields == sorted(fields)


# ---------------------------------------------------------------------------
# RAGResult dataclass
# ---------------------------------------------------------------------------

def test_rag_result_defaults():
    r = RAGResult()
    assert r.patterns == []
    assert r.pitfalls == []
    assert r.layer_hits == {"L0": 0, "L1": 0, "L2": 0, "L3": 0}
    assert r.total_bullets() == 0


def test_rag_entry_defaults():
    e = RAGEntry(pattern_hash="abc", pattern="rank(close)", entry_type="SUCCESS_PATTERN")
    assert e.meta_data == {}
    assert e.relevance_score == 0.5


# ---------------------------------------------------------------------------
# Layer 0: exact_match
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_layer0_empty_expression_returns_empty(pg_session):
    succ, fail = await layer0_exact_match(pg_session, current_expression=None)
    assert succ == [] and fail == []


@pytest.mark.asyncio
async def test_layer0_no_match_returns_empty(pg_session):
    succ, fail = await layer0_exact_match(
        pg_session, current_expression=f"nonexistent_{_TAG}(close)"
    )
    assert succ == [] and fail == []


@pytest.mark.asyncio
async def test_layer0_success_hit_excludes_decayed(pg_session):
    """SUCCESS_PATTERN entry matched but decayed=True → NOT returned."""
    expr = f"rank({_TAG}_field1)"
    phash = compute_pattern_hash(expr, None, None)
    pg_session.add(KnowledgeEntry(
        entry_type="SUCCESS_PATTERN",
        pattern=expr, pattern_hash=phash,
        description="decayed test",
        meta_data={DECAYED_KEY: "true", "source": "test"},
        is_active=True, created_by="TEST",
    ))
    await pg_session.commit()
    succ, fail = await layer0_exact_match(pg_session, current_expression=expr)
    assert succ == [], "decayed SUCCESS should be excluded"


@pytest.mark.asyncio
async def test_layer0_success_hit_includes_non_decayed(pg_session):
    expr = f"rank({_TAG}_field2)"
    phash = compute_pattern_hash(expr, None, None)
    pg_session.add(KnowledgeEntry(
        entry_type="SUCCESS_PATTERN",
        pattern=expr, pattern_hash=phash,
        description="not decayed",
        meta_data={"source": "test"},
        is_active=True, created_by="TEST",
    ))
    await pg_session.commit()
    succ, fail = await layer0_exact_match(pg_session, current_expression=expr)
    assert len(succ) == 1
    assert succ[0].source_layer == "L0_exact"
    assert succ[0].relevance_score == 1.0


@pytest.mark.asyncio
async def test_layer0_failure_hit_includes_decayed(pg_session):
    """FAILURE_PITFALL: decayed entries returned (they're the avoid set)."""
    expr = f"rank({_TAG}_field3)"
    phash = compute_pattern_hash(expr, None, None)
    pg_session.add(KnowledgeEntry(
        entry_type="FAILURE_PITFALL",
        pattern=expr, pattern_hash=phash,
        description="decayed failure",
        meta_data={DECAYED_KEY: "true"},
        is_active=True, created_by="TEST",
    ))
    await pg_session.commit()
    succ, fail = await layer0_exact_match(pg_session, current_expression=expr)
    assert succ == []
    assert len(fail) == 1
    assert fail[0].entry_type == "FAILURE_PITFALL"


# ---------------------------------------------------------------------------
# Layer 3: field_level
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_layer3_empty_returns_empty(pg_session):
    succ, fail = await layer3_field_level(pg_session, current_expression=None)
    assert succ == [] and fail == []


@pytest.mark.asyncio
async def test_layer3_no_fields_returns_empty(pg_session):
    """Pure op chain with no field tokens → nothing to match."""
    succ, fail = await layer3_field_level(pg_session, current_expression="rank(rank(rank(rank(rank(rank())))))")
    assert succ == [] and fail == []


@pytest.mark.asyncio
async def test_layer3_field_match_success(pg_session):
    field = f"{_TAG}_uniqfield"
    # Seed: SUCCESS entry containing the field token
    pg_session.add(KnowledgeEntry(
        entry_type="SUCCESS_PATTERN",
        pattern=f"ts_mean({field}, 20)",
        pattern_hash=compute_pattern_hash(f"ts_mean({field}, 20)", None, None),
        description="field match test",
        meta_data={"source": "test"},
        is_active=True, created_by="TEST",
    ))
    await pg_session.commit()
    succ, fail = await layer3_field_level(
        pg_session, current_expression=f"rank({field})",
    )
    assert len(succ) >= 1
    assert any(field in e.pattern for e in succ)
    assert all(e.source_layer == "L3_field" for e in succ)


@pytest.mark.asyncio
async def test_layer3_excludes_decayed_success(pg_session):
    """Decayed SUCCESS not returned even on field match."""
    field = f"{_TAG}_decayed_fld"
    pg_session.add(KnowledgeEntry(
        entry_type="SUCCESS_PATTERN",
        pattern=f"ts_mean({field}, 20)",
        pattern_hash=compute_pattern_hash(f"ts_mean({field}, 20)", None, None),
        description="decayed match",
        meta_data={DECAYED_KEY: "true"},
        is_active=True, created_by="TEST",
    ))
    await pg_session.commit()
    succ, fail = await layer3_field_level(
        pg_session, current_expression=f"rank({field})",
    )
    assert succ == [], "decayed SUCCESS must be excluded"


@pytest.mark.asyncio
async def test_layer3_region_filter(pg_session):
    """SUCCESS entry with meta_data['region']!=ctx_region → excluded."""
    field = f"{_TAG}_regfld"
    pg_session.add(KnowledgeEntry(
        entry_type="SUCCESS_PATTERN",
        pattern=f"rank({field})",
        pattern_hash=compute_pattern_hash(f"rank({field})", None, None),
        description="region scoped",
        meta_data={"region": "CHN"},
        is_active=True, created_by="TEST",
    ))
    await pg_session.commit()
    succ, fail = await layer3_field_level(
        pg_session, current_expression=f"rank({field})", region="USA",
    )
    # Region CHN entry, query USA → excluded
    assert all(e.meta_data.get("region") != "CHN" for e in succ)


@pytest.mark.asyncio
async def test_layer3_budget_caps_returns(pg_session):
    """budget=2 → return at most 2 entries even if more match."""
    field = f"{_TAG}_budget_fld"
    for i in range(5):
        pg_session.add(KnowledgeEntry(
            entry_type="SUCCESS_PATTERN",
            pattern=f"op{i}({field})",
            pattern_hash=compute_pattern_hash(f"op{i}({field})", None, None),
            description=f"budget test {i}",
            meta_data={"source": "test"},
            is_active=True, created_by="TEST",
        ))
    await pg_session.commit()
    succ, fail = await layer3_field_level(
        pg_session, current_expression=f"rank({field})", budget=2,
    )
    assert len(succ) <= 2
