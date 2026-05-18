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


# ===========================================================================
# PR2 — Layer 1 (pillar) + Layer 2 (family) tests
# ===========================================================================

from backend.agents.hierarchical_rag import layer1_pillar, layer2_family
from backend.family_classifier import family_signature


# --- Layer 1: pillar ---

@pytest.mark.asyncio
async def test_layer1_empty_expression_and_no_pillar_returns_empty(pg_session):
    """No expression + no hypothesis_pillar → can't resolve pillar."""
    succ, fail = await layer1_pillar(pg_session, current_expression=None, hypothesis_pillar=None)
    assert succ == [] and fail == []


@pytest.mark.asyncio
async def test_layer1_other_pillar_short_circuits(pg_session):
    """pillar='other' too broad → returns empty per [V1.0-A2-1]."""
    succ, fail = await layer1_pillar(pg_session, hypothesis_pillar="other")
    assert succ == [] and fail == []


@pytest.mark.asyncio
async def test_layer1_explicit_pillar_finds_match(pg_session):
    """Seed entry with meta_data.pillar_classified=momentum → L1 returns it."""
    pattern = f"rank({_TAG}_mom_field)"
    pg_session.add(KnowledgeEntry(
        entry_type="SUCCESS_PATTERN",
        pattern=pattern,
        pattern_hash=compute_pattern_hash(pattern, None, None),
        description="momentum entry",
        meta_data={"pillar_classified": "momentum", "source": "test"},
        is_active=True, created_by="TEST",
    ))
    await pg_session.commit()
    succ, fail = await layer1_pillar(
        pg_session, hypothesis_pillar="momentum", budget=10,
    )
    assert any(e.pattern == pattern for e in succ)
    assert all(e.source_layer == "L1_pillar" for e in succ)
    assert all(e.relevance_score == 0.75 for e in succ)


@pytest.mark.asyncio
async def test_layer1_excludes_decayed_success(pg_session):
    pattern = f"rank({_TAG}_decayed_mom)"
    pg_session.add(KnowledgeEntry(
        entry_type="SUCCESS_PATTERN",
        pattern=pattern,
        pattern_hash=compute_pattern_hash(pattern, None, None),
        description="decayed momentum",
        meta_data={"pillar_classified": "momentum", DECAYED_KEY: "true"},
        is_active=True, created_by="TEST",
    ))
    await pg_session.commit()
    succ, fail = await layer1_pillar(
        pg_session, hypothesis_pillar="momentum", budget=10,
    )
    # Should not include this decayed entry
    assert all(e.pattern != pattern for e in succ)


@pytest.mark.asyncio
async def test_layer1_includes_decayed_failure(pg_session):
    """FAILURE_PITFALL with decayed → INCLUDED (avoid set)."""
    pattern = f"rank({_TAG}_pitfall_mom)"
    pg_session.add(KnowledgeEntry(
        entry_type="FAILURE_PITFALL",
        pattern=pattern,
        pattern_hash=compute_pattern_hash(pattern, None, None),
        description="decayed pitfall",
        meta_data={"pillar_classified": "momentum", DECAYED_KEY: "true"},
        is_active=True, created_by="TEST",
    ))
    await pg_session.commit()
    succ, fail = await layer1_pillar(
        pg_session, hypothesis_pillar="momentum", budget=10,
    )
    assert any(e.pattern == pattern for e in fail)


@pytest.mark.asyncio
async def test_layer1_infers_pillar_from_expression(pg_session):
    """No explicit hypothesis_pillar → infer from current_expression."""
    pattern = f"rank({_TAG}_infer_mom)"
    pg_session.add(KnowledgeEntry(
        entry_type="SUCCESS_PATTERN",
        pattern=pattern,
        pattern_hash=compute_pattern_hash(pattern, None, None),
        description="inferred momentum",
        meta_data={"pillar_classified": "momentum"},
        is_active=True, created_by="TEST",
    ))
    await pg_session.commit()
    # ts_mean is momentum operator per pillar_classifier
    succ, fail = await layer1_pillar(
        pg_session,
        current_expression="rank(ts_mean(close, 252) - ts_mean(close, 20))",
        budget=10,
    )
    # Should at least find our seeded momentum entry
    assert any(e.pattern == pattern for e in succ)


# --- Layer 2: family ---

@pytest.mark.asyncio
async def test_layer2_empty_expression_returns_empty(pg_session):
    succ, fail = await layer2_family(pg_session, current_expression=None)
    assert succ == [] and fail == []


@pytest.mark.asyncio
async def test_layer2_no_operators_returns_empty(pg_session):
    """Bare field, no ops → family signature is '<empty>' → skip."""
    succ, fail = await layer2_family(pg_session, current_expression="close")
    assert succ == [] and fail == []


@pytest.mark.asyncio
async def test_layer2_same_family_finds_match(pg_session):
    """Two expressions with same op pipeline → same family_signature."""
    pattern_kb = f"rank(ts_mean({_TAG}_fam_a, 20))"
    sig = family_signature(pattern_kb)
    pg_session.add(KnowledgeEntry(
        entry_type="SUCCESS_PATTERN",
        pattern=pattern_kb,
        pattern_hash=compute_pattern_hash(pattern_kb, None, None),
        description="family member A",
        meta_data={"family_signature": sig, "source": "test"},
        is_active=True, created_by="TEST",
    ))
    await pg_session.commit()
    # Query with DIFFERENT expression but SAME op pipeline (rank+ts_mean)
    succ, fail = await layer2_family(
        pg_session,
        current_expression=f"rank(ts_mean({_TAG}_fam_b, 60))",
        budget=10,
    )
    assert any(e.pattern == pattern_kb for e in succ)
    assert all(e.source_layer == "L2_family" for e in succ)


@pytest.mark.asyncio
async def test_layer2_excludes_family_capped(pg_session):
    """[V1.0-S5] family_capped entries excluded — R10 purpose."""
    pattern = f"rank(ts_mean({_TAG}_capped, 20))"
    sig = family_signature(pattern)
    pg_session.add(KnowledgeEntry(
        entry_type="SUCCESS_PATTERN",
        pattern=pattern,
        pattern_hash=compute_pattern_hash(pattern, None, None),
        description="family capped",
        meta_data={"family_signature": sig, "family_capped": "true"},
        is_active=True, created_by="TEST",
    ))
    await pg_session.commit()
    succ, fail = await layer2_family(
        pg_session, current_expression=pattern, budget=10,
    )
    # The capped entry must NOT appear
    assert all(e.pattern != pattern for e in succ)


@pytest.mark.asyncio
async def test_layer2_excludes_decayed_success_includes_decayed_failure(pg_session):
    """Same as L0/L1/L3 dual-filter pattern."""
    pattern_succ = f"rank(ts_mean({_TAG}_dec_s, 20))"
    pattern_fail = f"rank(ts_mean({_TAG}_dec_f, 20))"
    sig = family_signature(pattern_succ)  # both share same family
    pg_session.add(KnowledgeEntry(
        entry_type="SUCCESS_PATTERN",
        pattern=pattern_succ,
        pattern_hash=compute_pattern_hash(pattern_succ, None, None),
        description="decayed success", meta_data={"family_signature": sig, DECAYED_KEY: "true"},
        is_active=True, created_by="TEST",
    ))
    pg_session.add(KnowledgeEntry(
        entry_type="FAILURE_PITFALL",
        pattern=pattern_fail,
        pattern_hash=compute_pattern_hash(pattern_fail, None, None),
        description="decayed failure", meta_data={"family_signature": sig, DECAYED_KEY: "true"},
        is_active=True, created_by="TEST",
    ))
    await pg_session.commit()
    succ, fail = await layer2_family(
        pg_session,
        current_expression=f"rank(ts_mean({_TAG}_dec_q, 20))",
        budget=10,
    )
    # SUCCESS decayed excluded
    assert all(e.pattern != pattern_succ for e in succ)
    # FAILURE decayed included
    assert any(e.pattern == pattern_fail for e in fail)


# ===========================================================================
# PR3 — Orchestrator (query_hierarchical) tests
# ===========================================================================

from backend.agents.hierarchical_rag import query_hierarchical


@pytest.mark.asyncio
async def test_orchestrator_empty_no_inputs_returns_empty_result(pg_session):
    """No expression + no pillar → orchestrator returns empty RAGResult."""
    r = await query_hierarchical(pg_session)
    assert r.total_bullets() == 0
    assert r.total_queries == 0
    assert r.layer_hits == {"L0": 0, "L1": 0, "L2": 0, "L3": 0}


@pytest.mark.asyncio
async def test_orchestrator_l0_exact_hit_short_circuits_layers(pg_session):
    """L0 exact match fills max_patterns → subsequent layers skipped."""
    expr = f"rank({_TAG}_orch_exact)"
    # Seed 1 SUCCESS at the exact pattern (L0 hit)
    pg_session.add(KnowledgeEntry(
        entry_type="SUCCESS_PATTERN", pattern=expr,
        pattern_hash=compute_pattern_hash(expr, None, None),
        description="L0 exact",
        meta_data={"pillar_classified": "momentum",
                   "family_signature": family_signature(expr),
                   "source": "test"},
        is_active=True, created_by="TEST",
    ))
    await pg_session.commit()
    r = await query_hierarchical(
        pg_session, current_expression=expr, max_patterns=1, max_pitfalls=0,
    )
    assert len(r.patterns) == 1
    assert r.patterns[0].source_layer == "L0_exact"
    assert r.layer_hits["L0"] == 1
    # max_patterns=1 satisfied → L1/L2/L3 SQL still ran but consumed nothing
    # (L0 met budget). Each later layer counts as 1 SQL query.
    assert r.total_queries >= 1


@pytest.mark.asyncio
async def test_orchestrator_dedupe_across_layers(pg_session):
    """Same pattern_hash hits multiple layers → only counted once."""
    pattern = f"rank({_TAG}_orch_dedupe)"
    phash = compute_pattern_hash(pattern, None, None)
    sig = family_signature(pattern)
    # Single entry that matches both L1 (pillar) and L2 (family) AND L3 (field)
    pg_session.add(KnowledgeEntry(
        entry_type="SUCCESS_PATTERN", pattern=pattern,
        pattern_hash=phash,
        description="dedupe test",
        meta_data={"pillar_classified": "momentum", "family_signature": sig,
                   "source": "test"},
        is_active=True, created_by="TEST",
    ))
    await pg_session.commit()
    r = await query_hierarchical(
        pg_session, current_expression=pattern,
        hypothesis_pillar="momentum",
        max_patterns=20, max_pitfalls=10,
    )
    # Should appear once even though it matched multiple layers
    count = sum(1 for p in r.patterns if p.pattern == pattern)
    assert count == 1


@pytest.mark.asyncio
async def test_orchestrator_max_patterns_cap_enforced(pg_session):
    """max_patterns=2 → at most 2 patterns returned even with many matches."""
    base = f"{_TAG}_orch_cap"
    sig = family_signature(f"rank({base})")  # consistent family
    for i in range(8):
        p = f"rank({base}_{i})"
        pg_session.add(KnowledgeEntry(
            entry_type="SUCCESS_PATTERN", pattern=p,
            pattern_hash=compute_pattern_hash(p, None, None),
            description=f"cap {i}",
            meta_data={"family_signature": sig, "pillar_classified": "momentum"},
            is_active=True, created_by="TEST",
        ))
    await pg_session.commit()
    r = await query_hierarchical(
        pg_session, current_expression=f"rank({base})", max_patterns=2,
    )
    assert len(r.patterns) <= 2


@pytest.mark.asyncio
async def test_orchestrator_q9_decayed_strict_filter_consistent(pg_session):
    """No decayed SUCCESS surfaces across ANY of the 4 layers (plan §10 GO d)."""
    pattern = f"rank({_TAG}_orch_decayed)"
    pg_session.add(KnowledgeEntry(
        entry_type="SUCCESS_PATTERN", pattern=pattern,
        pattern_hash=compute_pattern_hash(pattern, None, None),
        description="decayed across all layers",
        meta_data={
            "pillar_classified": "momentum",
            "family_signature": family_signature(pattern),
            DECAYED_KEY: "true",
        },
        is_active=True, created_by="TEST",
    ))
    await pg_session.commit()
    r = await query_hierarchical(
        pg_session, current_expression=pattern, hypothesis_pillar="momentum",
        max_patterns=20,
    )
    # The decayed SUCCESS entry must NOT appear in patterns from any layer
    assert all(p.pattern != pattern for p in r.patterns), \
        "decayed SUCCESS leaked into hierarchical RAG result"
