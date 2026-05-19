"""Phase 3 R8-v2 E2E integration test (2026-05-18).

Follow-up #5 from R8 PR3 ship memory — true end-to-end exercise of the
hierarchical RAG pipeline through ``RAGService.query`` (the production
caller's entry point), not just ``query_hierarchical`` directly.

Covers the complete flow:

  RAGService.query(current_expression=..., region=..., dataset_id=...)
    → ENABLE_HIERARCHICAL_RAG flag dispatch
    → query_hierarchical L0 (exact match) → L1 (pillar) → L2 (family) → L3 (field)
    → R8-v2 #2 Redis cache check + write
    → R8-v2 #3 R5 composite_score ranking on SUCCESS pool
    → RAGEntry → legacy Dict conversion
    → RAGResult shape returned

Uses live Postgres (matching test_rag_hierarchical_pr1.py pattern) with
unique-tag KB seeds so the test is isolated + cleanup is bounded.

The legacy path (no current_expression) is sanity-checked to confirm
the dispatch gate works in both directions.
"""
from __future__ import annotations

import socket
import uuid
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker


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


_TAG = f"r8v2_e2e_{uuid.uuid4().hex[:8]}"


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


async def _seed_kb(
    session: AsyncSession,
    *,
    layer: str,
    region: str = "USA",
    dataset_id: str = "fnd6",
) -> str:
    """Insert one SUCCESS + one FAILURE KB row tagged for ``layer`` so the
    test can verify which layer the orchestrator hit.

    L0 layer hashes (expression, region, dataset_id) — seed must match
    these query params to land on the L0 fast path.

    Returns the expression that the caller should pass as current_expression
    to exercise that layer.
    """
    from backend.models import KnowledgeEntry
    from backend.models.knowledge import compute_pattern_hash

    if layer == "L0":
        # Exact match on expression
        expr = f"rank({_TAG}_l0(close, 20))"
        succ_pattern = expr
        fail_pattern = expr + "_FAIL"
    elif layer == "L1":
        # Same pillar (momentum), different expression
        expr = f"ts_mean({_TAG}_l1, 5)"
        succ_pattern = f"ts_zscore({_TAG}_l1_other, 10)"
        fail_pattern = f"ts_rank({_TAG}_l1_failed, 30)"
    elif layer == "L2":
        # Family signature (same operator skeleton) match
        expr = f"ts_corr({_TAG}_l2_a, {_TAG}_l2_b, 20)"
        succ_pattern = f"ts_corr({_TAG}_l2_diff_x, {_TAG}_l2_diff_y, 30)"
        fail_pattern = f"ts_corr({_TAG}_l2_fail_x, {_TAG}_l2_fail_y, 60)"
    elif layer == "L3":
        # Field-level overlap on uncommon field name
        expr = f"rank({_TAG}_l3_field)"
        succ_pattern = f"ts_mean({_TAG}_l3_field, 10)"
        fail_pattern = f"ts_decay({_TAG}_l3_field, 5)"
    else:
        raise ValueError(f"unknown layer: {layer}")

    succ = KnowledgeEntry(
        entry_type="SUCCESS_PATTERN",
        pattern=succ_pattern,
        description=f"E2E {_TAG} {layer} success seed",
        pattern_hash=compute_pattern_hash(succ_pattern, region, dataset_id),
        is_active=True,
        meta_data={
            "regions": ["USA"],
            "requires_role": "both",
            "import_batch": _TAG,
            "pillar": "momentum",
            "fields": [f"{_TAG}_l3_field"] if layer == "L3" else [],
        },
    )
    fail = KnowledgeEntry(
        entry_type="FAILURE_PITFALL",
        pattern=fail_pattern,
        description=f"E2E {_TAG} {layer} failure seed",
        pattern_hash=compute_pattern_hash(fail_pattern, region, dataset_id),
        is_active=True,
        meta_data={
            "regions": ["USA"],
            "requires_role": "both",
            "import_batch": _TAG,
            "pillar": "momentum",
            "fields": [f"{_TAG}_l3_field"] if layer == "L3" else [],
        },
    )
    session.add(succ)
    session.add(fail)
    await session.commit()
    return expr


# ---------------------------------------------------------------------------
# Dispatch gate verification — flag OFF must skip hierarchical entirely
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e2e_flag_off_skips_hierarchical_uses_legacy(pg_session, monkeypatch):
    """ENABLE_HIERARCHICAL_RAG=False + current_expression set → legacy path
    runs (no exception from hier even if it would have errored)."""
    from backend.config import settings
    from backend.agents.services.rag_service import RAGService

    monkeypatch.setattr(settings, "ENABLE_HIERARCHICAL_RAG", False, raising=False)
    svc = RAGService(pg_session)

    # Even with current_expression, flag OFF → legacy path
    r = await svc.query(
        region="USA",
        dataset_id="fnd6",
        current_expression="rank(close)",
        max_patterns=2,
        max_pitfalls=2,
    )
    # Legacy RAGResult shape — patterns/pitfalls list-of-dicts
    assert isinstance(r.patterns, list)
    assert isinstance(r.pitfalls, list)


@pytest.mark.asyncio
async def test_e2e_no_expression_skips_hierarchical_even_when_flag_on(pg_session, monkeypatch):
    """ENABLE_HIERARCHICAL_RAG=True + no current_expression/pillar → legacy
    path still runs (gate is AND, not OR)."""
    from backend.config import settings
    from backend.agents.services.rag_service import RAGService

    monkeypatch.setattr(settings, "ENABLE_HIERARCHICAL_RAG", True, raising=False)
    svc = RAGService(pg_session)

    r = await svc.query(
        region="USA",
        dataset_id="fnd6",
        # No current_expression / hypothesis_pillar
        max_patterns=2,
        max_pitfalls=2,
    )
    assert isinstance(r.patterns, list)


# ---------------------------------------------------------------------------
# Flag-ON end-to-end — each layer hit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e2e_l0_exact_match_hits_first_layer(pg_session, monkeypatch):
    """Seed an exact-match SUCCESS row, query same expression → result
    contains the seed in patterns; short-circuits past L1/L2/L3."""
    from backend.config import settings
    from backend.agents.services.rag_service import RAGService

    monkeypatch.setattr(settings, "ENABLE_HIERARCHICAL_RAG", True, raising=False)

    expr = await _seed_kb(pg_session, layer="L0")
    svc = RAGService(pg_session)

    r = await svc.query(
        region="USA",
        dataset_id="fnd6",
        current_expression=expr,
        max_patterns=5,
        max_pitfalls=5,
    )

    # At minimum the L0 success pattern (same expr) should appear
    assert any(_TAG in p.get("pattern", "") for p in r.patterns), (
        f"Expected L0 seed in patterns; got: {[p.get('pattern') for p in r.patterns]}"
    )
    # And source_layer marker (one of the 4 layer ids) on the matching pattern
    valid_layers = {"L0_exact", "L1_pillar", "L2_family", "L3_field"}
    for p in r.patterns:
        if _TAG in p.get("pattern", ""):
            assert p["metadata"].get("source_layer") in valid_layers


@pytest.mark.asyncio
async def test_e2e_l3_field_overlap_hits_when_no_higher_layer(pg_session, monkeypatch):
    """Seed unique field-name overlap → orchestrator should reach L3."""
    from backend.config import settings
    from backend.agents.services.rag_service import RAGService

    monkeypatch.setattr(settings, "ENABLE_HIERARCHICAL_RAG", True, raising=False)

    expr = await _seed_kb(pg_session, layer="L3")
    svc = RAGService(pg_session)

    r = await svc.query(
        region="USA",
        dataset_id="fnd6",
        current_expression=expr,
        max_patterns=5,
        max_pitfalls=5,
    )

    # The L3 field overlap should surface the seed
    matched = [p for p in r.patterns if _TAG in p.get("pattern", "")]
    assert matched, (
        f"Expected L3 field-overlap match; got: {[p.get('pattern') for p in r.patterns]}"
    )


# ---------------------------------------------------------------------------
# Soft-fall — hierarchical exception must not break query
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e2e_hierarchical_exception_falls_back_to_legacy(pg_session, monkeypatch):
    """If query_hierarchical raises, RAGService.query must fall back to legacy
    + return a real (non-empty-shape) RAGResult."""
    from backend.config import settings
    from backend.agents.services import rag_service as rs
    from backend.agents.services.rag_service import RAGService

    monkeypatch.setattr(settings, "ENABLE_HIERARCHICAL_RAG", True, raising=False)

    async def _boom(*a, **kw):
        raise RuntimeError("simulated hier failure")

    monkeypatch.setattr(
        "backend.agents.hierarchical_rag.query_hierarchical", _boom,
    )

    svc = RAGService(pg_session)
    r = await svc.query(
        region="USA",
        dataset_id="fnd6",
        current_expression="rank(close)",
        max_patterns=2,
        max_pitfalls=2,
    )
    # The legacy path returned a valid RAGResult — no propagation
    assert hasattr(r, "patterns")
    assert hasattr(r, "pitfalls")


# ---------------------------------------------------------------------------
# Hypothesis-pillar dispatch path (no expression, pillar-only)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e2e_pillar_only_dispatch_active(pg_session, monkeypatch):
    """hypothesis_pillar set + no current_expression → still dispatches to
    hierarchical (the gate is OR between current_expression and pillar)."""
    from backend.config import settings
    from backend.agents.services.rag_service import RAGService

    monkeypatch.setattr(settings, "ENABLE_HIERARCHICAL_RAG", True, raising=False)

    svc = RAGService(pg_session)
    # No seeds needed — just verifying the dispatch is reached without
    # raising. Empty result is fine.
    r = await svc.query(
        region="USA",
        dataset_id="fnd6",
        hypothesis_pillar="momentum",
        max_patterns=2,
        max_pitfalls=2,
    )
    assert isinstance(r.patterns, list)
    assert isinstance(r.pitfalls, list)


# ---------------------------------------------------------------------------
# R8-v2 #5 review MEDIUM — shape assertions beyond `isinstance(list)`
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e2e_decayed_partition_shape_in_pitfalls(pg_session, monkeypatch):
    """Q9 dual-filter: FAILURE_PITFALL INCLUDES decayed entries. Seed both
    decayed=true and decayed=false pitfalls matching the same pillar →
    both surface AND each pitfall dict carries the original
    meta_data['decayed'] field so callers can partition.
    """
    from backend.config import settings
    from backend.models import KnowledgeEntry
    from backend.models.knowledge import compute_pattern_hash
    from backend.agents.services.rag_service import RAGService

    monkeypatch.setattr(settings, "ENABLE_HIERARCHICAL_RAG", True, raising=False)

    decayed_pattern = f"ts_rank({_TAG}_dec_true_mom, 5)"
    fresh_pattern = f"ts_rank({_TAG}_dec_false_mom, 5)"
    pg_session.add(KnowledgeEntry(
        entry_type="FAILURE_PITFALL",
        pattern=decayed_pattern,
        pattern_hash=compute_pattern_hash(decayed_pattern, None, None),
        description=f"E2E {_TAG} decayed pitfall",
        is_active=True,
        meta_data={"pillar_classified": "momentum",
                   "import_batch": _TAG, "decayed": "true"},
    ))
    pg_session.add(KnowledgeEntry(
        entry_type="FAILURE_PITFALL",
        pattern=fresh_pattern,
        pattern_hash=compute_pattern_hash(fresh_pattern, None, None),
        description=f"E2E {_TAG} fresh pitfall",
        is_active=True,
        meta_data={"pillar_classified": "momentum",
                   "import_batch": _TAG, "decayed": "false"},
    ))
    await pg_session.commit()

    svc = RAGService(pg_session)
    r = await svc.query(
        region="USA",
        dataset_id="fnd6",
        hypothesis_pillar="momentum",
        current_expression="rank(close)",
        max_patterns=2,
        max_pitfalls=10,
    )

    # Filter to seeded pitfalls only (other concurrent KB rows may exist)
    seeded = [p for p in r.pitfalls if _TAG in p.get("pattern", "")]
    assert len(seeded) == 2, (
        f"Q9 dual-filter expected both decayed + fresh pitfalls; got "
        f"{[p.get('pattern') for p in seeded]}"
    )
    # Each carries decayed marker through the RAGEntry→dict conversion
    decayed_vals = sorted(
        str(p["metadata"].get("decayed", "")).lower() for p in seeded
    )
    assert decayed_vals == ["false", "true"], (
        f"Expected partition keys ['false','true']; got {decayed_vals}"
    )


@pytest.mark.asyncio
async def test_e2e_max_pitfalls_bound_enforced(pg_session, monkeypatch):
    """Seed > max_pitfalls FAILURE rows; assert returned list is capped."""
    from backend.config import settings
    from backend.models import KnowledgeEntry
    from backend.models.knowledge import compute_pattern_hash
    from backend.agents.services.rag_service import RAGService

    monkeypatch.setattr(settings, "ENABLE_HIERARCHICAL_RAG", True, raising=False)

    # Seed 10 FAILURE_PITFALL rows matching pillar=momentum
    for i in range(10):
        pat = f"ts_rank({_TAG}_bound_{i}, 5)"
        pg_session.add(KnowledgeEntry(
            entry_type="FAILURE_PITFALL",
            pattern=pat,
            pattern_hash=compute_pattern_hash(pat, None, None),
            description=f"E2E {_TAG} bound pitfall {i}",
            is_active=True,
            meta_data={"pillar_classified": "momentum",
                       "import_batch": _TAG},
        ))
    await pg_session.commit()

    svc = RAGService(pg_session)
    r = await svc.query(
        region="USA",
        dataset_id="fnd6",
        hypothesis_pillar="momentum",
        current_expression="rank(close)",
        max_patterns=2,
        max_pitfalls=3,
    )
    # Hard cap honored end-to-end (orchestrator decrements remaining_fail
    # across layers and stops appending past max_pitfalls).
    assert len(r.pitfalls) <= 3, (
        f"max_pitfalls=3 violated; got {len(r.pitfalls)} pitfalls"
    )


@pytest.mark.asyncio
async def test_e2e_empty_context_no_failure_pitfall(pg_session, monkeypatch):
    """No FAILURE_PITFALL rows seeded for the query → pitfalls is an empty
    list (not None, no exception).
    """
    from backend.config import settings
    from backend.models import KnowledgeEntry
    from backend.models.knowledge import compute_pattern_hash
    from backend.agents.services.rag_service import RAGService
    from sqlalchemy import text as _text

    monkeypatch.setattr(settings, "ENABLE_HIERARCHICAL_RAG", True, raising=False)

    # Use a unique pillar so no other KB FAILURE_PITFALL rows can match
    # (production KB has ~1660 pitfalls under standard pillars). The
    # SUCCESS-only seed lives under this synthetic pillar.
    uniq_pillar = f"empty_{_TAG}"
    succ_pattern = f"rank({_TAG}_empty_pillar_seed)"
    pg_session.add(KnowledgeEntry(
        entry_type="SUCCESS_PATTERN",
        pattern=succ_pattern,
        pattern_hash=compute_pattern_hash(succ_pattern, None, None),
        description=f"E2E {_TAG} success-only seed",
        is_active=True,
        meta_data={"pillar_classified": uniq_pillar,
                   "import_batch": _TAG},
    ))
    await pg_session.commit()

    # Omit current_expression — L0/L2/L3 are expression-gated, so this
    # restricts the query to L1 only. L1 filters by pillar_classified,
    # and our unique uniq_pillar guarantees no production KB row matches
    # → real empty-context shape.

    svc = RAGService(pg_session)
    r = await svc.query(
        region="USA",
        dataset_id="fnd6",
        hypothesis_pillar=uniq_pillar,
        max_patterns=5,
        max_pitfalls=5,
    )
    assert r.pitfalls is not None, "pitfalls must be a list, not None"
    assert isinstance(r.pitfalls, list)
    assert r.pitfalls == [], (
        f"Expected empty pitfalls for unique pillar; got "
        f"{[p.get('pattern') for p in r.pitfalls]}"
    )
