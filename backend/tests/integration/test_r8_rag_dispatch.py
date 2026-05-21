"""Phase 3 R8 PR3 R8-v2: RAGService.query() flag dispatch (2026-05-18).

Verifies the additive opt-in dispatch:
  - Flag OFF → byte-equivalent legacy retrieval (no current_expression
    parameter usage)
  - Flag ON + current_expression → hierarchical path (RAGResult patterns
    have 'source_layer' in metadata)
  - Flag ON + no current_expression/pillar → legacy path (opt-in only)
  - Hierarchical exception → graceful fall to legacy
"""
from __future__ import annotations

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

from backend.config import _flag_override_cache  # noqa: E402
from backend.models.knowledge import KnowledgeEntry, compute_pattern_hash  # noqa: E402
from backend.family_classifier import family_signature  # noqa: E402


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


_TAG = f"r8_disp_{uuid.uuid4().hex[:8]}"


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


@pytest.fixture(autouse=True)
def _clear_flag_cache():
    _flag_override_cache.clear()
    yield
    _flag_override_cache.clear()


# ---------------------------------------------------------------------------
# Dispatch tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_flag_off_uses_legacy_path(pg_session):
    """Flag OFF → legacy path (no source_layer metadata on results)."""
    from backend.agents.services.rag_service import RAGService
    pattern = f"rank({_TAG}_legacy_test)"
    pg_session.add(KnowledgeEntry(
        entry_type="SUCCESS_PATTERN", pattern=pattern,
        pattern_hash=compute_pattern_hash(pattern, None, None),
        description="legacy match",
        meta_data={"pillar_classified": "momentum",
                    "family_signature": family_signature(pattern)},
        is_active=True, created_by="TEST",
    ))
    await pg_session.commit()
    svc = RAGService(pg_session)
    # Flag OFF — legacy path, no R8 metadata expected
    r = await svc.query(
        region="USA", max_patterns=10,
        current_expression=pattern,  # passed but flag OFF → ignored
    )
    # Legacy results don't have source_layer in metadata
    has_layer_tag = any(
        "source_layer" in (p.get("metadata") or {}) for p in r.patterns
    )
    assert not has_layer_tag, "flag OFF should NOT dispatch to hierarchical"


@pytest.mark.asyncio
async def test_dispatch_no_signal_uses_legacy(pg_session):
    """Flag ON but NO signal at all (no expression, no region→no G4 pillar,
    no dataset_id) → legacy path. 2026-05-21: the opt-in signals widened to
    include dataset_id (pillar-decoupled category retrieval) and a region-driven
    G4 pillar hint, so a genuine legacy fallback requires NONE of those."""
    _flag_override_cache["ENABLE_HIERARCHICAL_RAG"] = True
    from backend.agents.services.rag_service import RAGService
    svc = RAGService(pg_session)
    # No expression, no region (→ G4 skipped), no dataset_id → nothing to
    # dispatch on → legacy.
    r = await svc.query(max_patterns=10)
    has_layer_tag = any(
        "source_layer" in (p.get("metadata") or {}) for p in r.patterns
    )
    assert not has_layer_tag, "no signal → legacy regardless of flag"


@pytest.mark.asyncio
async def test_dispatch_flag_on_with_expression_uses_hierarchical(pg_session):
    """Flag ON + current_expression → hierarchical path; results carry
    source_layer in metadata."""
    _flag_override_cache["ENABLE_HIERARCHICAL_RAG"] = True
    from backend.agents.services.rag_service import RAGService

    pattern = f"rank({_TAG}_hier_dispatch)"
    pg_session.add(KnowledgeEntry(
        entry_type="SUCCESS_PATTERN", pattern=pattern,
        pattern_hash=compute_pattern_hash(pattern, None, None),
        description="hier match",
        meta_data={"pillar_classified": "momentum",
                    "family_signature": family_signature(pattern)},
        is_active=True, created_by="TEST",
    ))
    await pg_session.commit()

    svc = RAGService(pg_session)
    r = await svc.query(
        region="USA", max_patterns=10,
        current_expression=pattern,  # opt-in trigger
    )
    # At least one returned pattern should have source_layer (from R8)
    has_layer_tag = any(
        "source_layer" in (p.get("metadata") or {}) for p in r.patterns
    )
    assert has_layer_tag, "flag ON + expression → hierarchical, expect source_layer"


@pytest.mark.asyncio
async def test_dispatch_hierarchical_exception_falls_back_to_legacy(pg_session):
    """If hierarchical_rag raises → graceful fall to legacy (no crash)."""
    _flag_override_cache["ENABLE_HIERARCHICAL_RAG"] = True
    from backend.agents.services.rag_service import RAGService
    svc = RAGService(pg_session)

    # Patch query_hierarchical to raise
    with patch(
        "backend.agents.hierarchical_rag.query_hierarchical",
        side_effect=RuntimeError("simulated hierarchical failure"),
    ):
        # Should NOT raise; should return a RAGResult from legacy path
        r = await svc.query(
            region="USA", max_patterns=5,
            current_expression="rank(close)",
        )
    # Did not crash + returned a result (empty or legacy patterns)
    assert hasattr(r, "patterns")
    assert hasattr(r, "pitfalls")


@pytest.mark.asyncio
async def test_dispatch_pillar_only_triggers_hierarchical(pg_session):
    """Hypothesis_pillar alone (without expression) triggers hierarchical."""
    _flag_override_cache["ENABLE_HIERARCHICAL_RAG"] = True
    from backend.agents.services.rag_service import RAGService

    pattern = f"rank({_TAG}_pillar_only)"
    pg_session.add(KnowledgeEntry(
        entry_type="SUCCESS_PATTERN", pattern=pattern,
        pattern_hash=compute_pattern_hash(pattern, None, None),
        description="pillar-only match",
        meta_data={"pillar_classified": "momentum"},
        is_active=True, created_by="TEST",
    ))
    await pg_session.commit()

    svc = RAGService(pg_session)
    r = await svc.query(
        region="USA", max_patterns=10,
        hypothesis_pillar="momentum",
    )
    has_layer_tag = any(
        "source_layer" in (p.get("metadata") or {}) for p in r.patterns
    )
    assert has_layer_tag, "pillar-only opt-in → hierarchical path"
