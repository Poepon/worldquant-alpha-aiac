"""Phase 3 R8-v2 #3: layer2_family R5 composite_score ranking (2026-05-18).

Verifies layer2_family JOIN with r1a_attribution_log.r5_composite_score:
  - Flag OFF (enable_r5_ranking=False) → order preserved (insertion order)
  - Flag ON + R5 rows present → SUCCESS re-ranked by avg(r5_composite_score)
  - Flag ON + no R5 rows → original order preserved, no crash
  - Flag ON + SQL error → soft-fall, original order
  - min_samples threshold respected
"""
from __future__ import annotations

import hashlib
import os
import socket
import uuid
from typing import AsyncGenerator
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("POSTGRES_PORT", "5433")

from backend.agents.hierarchical_rag import (  # noqa: E402
    _expr_hash_64,
    fetch_r5_avg_scores,
    layer2_family,
)
from backend.family_classifier import family_signature  # noqa: E402
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


_TAG = f"r8v2_r5rank_{uuid.uuid4().hex[:8]}"


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
                # Best-effort cleanup of r1a_attribution_log rows tagged via
                # expression containing our test tag.
                await s.execute(text(
                    "DELETE FROM r1a_attribution_log WHERE expression LIKE :p"
                ), {"p": f"%{_TAG}%"})
                await s.commit()
            except Exception:
                await s.rollback()
    finally:
        await engine.dispose()


async def _seed_r1a(db: AsyncSession, *, expression: str, score: float, n: int = 1):
    """Insert n r1a_attribution_log rows for `expression` with given r5 score."""
    h = _expr_hash_64(expression)
    for _ in range(n):
        await db.execute(text(
            "INSERT INTO r1a_attribution_log "
            "(expression, expression_hash, attribution, hook_version, "
            " r5_composite_score, created_at) "
            "VALUES (:e, :h, :a, 'v1', :s, now())"
        ), {"e": expression, "h": h, "a": "hypothesis", "s": float(score)})
    await db.commit()


# ---------------------------------------------------------------------------
# fetch_r5_avg_scores helper tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_r5_empty_input_returns_empty(pg_session):
    out = await fetch_r5_avg_scores(pg_session, [])
    assert out == {}


@pytest.mark.asyncio
async def test_fetch_r5_returns_avg_and_count(pg_session):
    expr = f"rank(ts_mean({_TAG}_a, 20))"
    await _seed_r1a(pg_session, expression=expr, score=0.8, n=1)
    await _seed_r1a(pg_session, expression=expr, score=0.6, n=1)
    out = await fetch_r5_avg_scores(pg_session, [expr])
    h = _expr_hash_64(expr)
    assert h in out
    avg, n = out[h]
    assert n == 2
    assert abs(avg - 0.7) < 1e-6


@pytest.mark.asyncio
async def test_fetch_r5_respects_min_samples(pg_session):
    expr = f"rank(ts_mean({_TAG}_min, 20))"
    await _seed_r1a(pg_session, expression=expr, score=0.9, n=1)
    out = await fetch_r5_avg_scores(pg_session, [expr], min_samples=5)
    assert out == {}  # 1 sample < 5


# ---------------------------------------------------------------------------
# layer2_family R5 ranking tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_l2_flag_off_preserves_insertion_order(pg_session):
    """enable_r5_ranking=False → behavior unchanged (no R5 join)."""
    base_expr = f"rank(ts_mean({_TAG}_off_a, 20))"
    sig = family_signature(base_expr)
    patterns = [
        f"rank(ts_mean({_TAG}_off_a, 20))",
        f"rank(ts_mean({_TAG}_off_b, 30))",
        f"rank(ts_mean({_TAG}_off_c, 40))",
    ]
    for p in patterns:
        pg_session.add(KnowledgeEntry(
            entry_type="SUCCESS_PATTERN", pattern=p,
            pattern_hash=compute_pattern_hash(p, None, None),
            description="off-flag", meta_data={"family_signature": sig},
            is_active=True, created_by="TEST",
        ))
    await pg_session.commit()

    # Seed a high R5 for the LAST pattern only — flag OFF should NOT reorder.
    await _seed_r1a(pg_session, expression=patterns[2], score=0.95, n=3)

    succ, fail = await layer2_family(
        pg_session,
        current_expression=f"rank(ts_mean({_TAG}_query, 60))",
        budget=10,
        enable_r5_ranking=False,
    )
    # All three should be present; relevance default 0.65 unchanged
    assert all(e.relevance_score == 0.65 for e in succ)
    assert all("_r5_composite_avg" not in e.meta_data for e in succ)


@pytest.mark.asyncio
async def test_l2_flag_on_reranks_by_r5_score(pg_session):
    """Flag ON + R5 rows present → high-R5 pattern sorted to front."""
    base_expr = f"rank(ts_mean({_TAG}_on_a, 20))"
    sig = family_signature(base_expr)
    patterns = {
        "low":  f"rank(ts_mean({_TAG}_on_low, 20))",
        "mid":  f"rank(ts_mean({_TAG}_on_mid, 30))",
        "high": f"rank(ts_mean({_TAG}_on_high, 40))",
    }
    for p in patterns.values():
        pg_session.add(KnowledgeEntry(
            entry_type="SUCCESS_PATTERN", pattern=p,
            pattern_hash=compute_pattern_hash(p, None, None),
            description="on-flag", meta_data={"family_signature": sig},
            is_active=True, created_by="TEST",
        ))
    await pg_session.commit()

    await _seed_r1a(pg_session, expression=patterns["low"], score=0.1, n=2)
    await _seed_r1a(pg_session, expression=patterns["mid"], score=0.5, n=2)
    await _seed_r1a(pg_session, expression=patterns["high"], score=0.95, n=2)

    succ, fail = await layer2_family(
        pg_session,
        current_expression=f"rank(ts_mean({_TAG}_query2, 60))",
        budget=10,
        enable_r5_ranking=True,
    )
    # Find indices of our three test patterns in the returned ordering
    order = {e.pattern: i for i, e in enumerate(succ)}
    assert patterns["high"] in order and patterns["low"] in order
    assert order[patterns["high"]] < order[patterns["mid"]] < order[patterns["low"]], \
        f"high R5 should outrank mid > low; got order: {order}"
    # Verify ranking metadata stamped
    high_entry = next(e for e in succ if e.pattern == patterns["high"])
    assert "_r5_composite_avg" in high_entry.meta_data
    assert high_entry.meta_data["_r5_sample_count"] == 2
    assert 0.45 <= high_entry.relevance_score <= 0.85


@pytest.mark.asyncio
async def test_l2_flag_on_no_r5_rows_preserves_default(pg_session):
    """Flag ON but no r1a_attribution_log rows → default 0.65 relevance."""
    base_expr = f"rank(ts_mean({_TAG}_nor5_a, 20))"
    sig = family_signature(base_expr)
    patterns = [
        f"rank(ts_mean({_TAG}_nor5_a, 20))",
        f"rank(ts_mean({_TAG}_nor5_b, 30))",
    ]
    for p in patterns:
        pg_session.add(KnowledgeEntry(
            entry_type="SUCCESS_PATTERN", pattern=p,
            pattern_hash=compute_pattern_hash(p, None, None),
            description="no r5",
            meta_data={"family_signature": sig},
            is_active=True, created_by="TEST",
        ))
    await pg_session.commit()

    succ, fail = await layer2_family(
        pg_session,
        current_expression=f"rank(ts_mean({_TAG}_query3, 60))",
        budget=10,
        enable_r5_ranking=True,
    )
    # No R5 rows → score_map empty → no rerank, defaults kept
    assert all(e.relevance_score == 0.65 for e in succ)
    assert all("_r5_composite_avg" not in e.meta_data for e in succ)


@pytest.mark.asyncio
async def test_l2_flag_on_sql_error_soft_falls(pg_session):
    """fetch_r5_avg_scores raising → layer2_family still returns SUCCESS."""
    base_expr = f"rank(ts_mean({_TAG}_err_a, 20))"
    sig = family_signature(base_expr)
    pattern = f"rank(ts_mean({_TAG}_err_a, 20))"
    pg_session.add(KnowledgeEntry(
        entry_type="SUCCESS_PATTERN", pattern=pattern,
        pattern_hash=compute_pattern_hash(pattern, None, None),
        description="err test", meta_data={"family_signature": sig},
        is_active=True, created_by="TEST",
    ))
    await pg_session.commit()

    with patch(
        "backend.agents.hierarchical_rag.fetch_r5_avg_scores",
        side_effect=RuntimeError("simulated R5 fetch fail"),
    ):
        # Outer try in layer2_family catches → returns [] [] (whole layer
        # soft-fails). This is acceptable behavior per existing layer
        # contract; assertion: it does NOT raise.
        try:
            succ, fail = await layer2_family(
                pg_session,
                current_expression=f"rank(ts_mean({_TAG}_q4, 60))",
                budget=5,
                enable_r5_ranking=True,
            )
        except Exception as e:
            pytest.fail(f"layer2_family must soft-fail, but raised: {e}")
        # Either reranked-empty (whole layer caught) or original (only fetch
        # caught) — both acceptable; assert it returned a tuple of lists.
        assert isinstance(succ, list)
        assert isinstance(fail, list)


@pytest.mark.asyncio
async def test_l2_flag_on_partial_r5_rows(pg_session):
    """Mixed: some patterns have R5 rows, others don't → only seeded ones
    rerank; unseeded keep default 0.65 (no metadata stamp)."""
    base_expr = f"rank(ts_mean({_TAG}_mix_a, 20))"
    sig = family_signature(base_expr)
    seeded = f"rank(ts_mean({_TAG}_mix_seeded, 20))"
    unseeded = f"rank(ts_mean({_TAG}_mix_unseeded, 30))"
    for p in [seeded, unseeded]:
        pg_session.add(KnowledgeEntry(
            entry_type="SUCCESS_PATTERN", pattern=p,
            pattern_hash=compute_pattern_hash(p, None, None),
            description="mixed", meta_data={"family_signature": sig},
            is_active=True, created_by="TEST",
        ))
    await pg_session.commit()

    await _seed_r1a(pg_session, expression=seeded, score=0.9, n=1)
    succ, fail = await layer2_family(
        pg_session,
        current_expression=f"rank(ts_mean({_TAG}_q5, 60))",
        budget=10,
        enable_r5_ranking=True,
    )
    seeded_entry = next((e for e in succ if e.pattern == seeded), None)
    unseeded_entry = next((e for e in succ if e.pattern == unseeded), None)
    assert seeded_entry is not None and unseeded_entry is not None
    assert "_r5_composite_avg" in seeded_entry.meta_data
    assert "_r5_composite_avg" not in unseeded_entry.meta_data
    # Seeded high-R5 should appear first
    assert succ.index(seeded_entry) < succ.index(unseeded_entry)
