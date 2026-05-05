"""Phase 2 B8 — KB hypothesis-keyed learning unit tests.

Verifies:
1. record_failure_pattern + record_success_pattern write hypothesis_id and
   experiment_variant into KnowledgeEntry.meta_data
2. Repeated calls accumulate hypothesis_ids list (one pattern can be
   produced/hit by multiple hypotheses)
3. get_recent_pass_examples filters by hypothesis_id (lineage-keyed RAG)
4. get_recent_pass_examples filters by experiment_variant (F-5 isolation)

Runs against live PG since KnowledgeEntry uses JSONB. expression_to_skeleton
normalizes field names → FIELD and numbers → NUM, so per-test uniqueness is
achieved by varying the outer OPERATOR (different op = different skeleton).
"""
from __future__ import annotations

import socket
import uuid
from datetime import datetime, timezone
import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from backend.models import KnowledgeEntry


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


# Each test grabs its own dedicated operator from this list to keep skeletons
# disjoint across tests (avoids merging into one shared row).
_OP_VARIANTS = [
    "ts_rank({f}, 5)",      # 0
    "ts_zscore({f}, 7)",    # 1
    "ts_mean({f}, 9)",      # 2
    "ts_std_dev({f}, 11)",  # 3
    "ts_delta({f}, 13)",    # 4
    "ts_sum({f}, 15)",      # 5
    "ts_arg_max({f}, 17)",  # 6
    "ts_av_diff({f}, 19)",  # 7
    "ts_quantile({f}, 21)", # 8
    "ts_decay_linear({f}, 23)",  # 9
]


@pytest_asyncio.fixture
async def session():
    """Live PG session. Pre-test cleanup deletes any KnowledgeEntry whose
    pattern matches our test skeleton set (so a previous failed test run
    doesn't leave merge-targets behind). Post-test cleanup repeats that
    cleanup."""
    skeletons_to_cleanup = [
        # Pre-compute every skeleton we'll create (skeleton normalization
        # collapses field names + numbers, so our 10 operator variants give
        # exactly 10 distinct skeletons).
        "ts_rank(FIELD, NUM)",
        "ts_zscore(FIELD, NUM)",
        "ts_mean(FIELD, NUM)",
        "ts_std_dev(FIELD, NUM)",
        "ts_delta(FIELD, NUM)",
        "ts_sum(FIELD, NUM)",
        "ts_arg_max(FIELD, NUM)",
        "ts_av_diff(FIELD, NUM)",
        "ts_quantile(FIELD, NUM)",
        "ts_decay_linear(FIELD, NUM)",
    ]
    engine = create_async_engine(
        "postgresql+asyncpg://postgres:postgres@localhost:5433/alpha_gpt",
        echo=False,
    )
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _cleanup(s):
        try:
            await s.execute(
                delete(KnowledgeEntry).where(
                    KnowledgeEntry.pattern.in_(skeletons_to_cleanup),
                )
            )
            await s.commit()
        except Exception:
            await s.rollback()

    async with maker() as s:
        await _cleanup(s)
        yield s
        await _cleanup(s)
    await engine.dispose()


def _expr(operator_idx: int) -> str:
    return _OP_VARIANTS[operator_idx % len(_OP_VARIANTS)].format(f="dummy_field")


# No-op kept so existing test bodies still compile; cleanup now via fixture.
async def _stamp_marker(session, marker: str):
    return None


# =============================================================================
# Write path — hypothesis_id + variant land in meta_data
# =============================================================================

async def _find_by_skeleton(session, skeleton: str, entry_type: str):
    r = await session.execute(
        select(KnowledgeEntry).where(
            KnowledgeEntry.pattern == skeleton,
            KnowledgeEntry.entry_type == entry_type,
        )
    )
    return list(r.scalars().all())


@pytest.mark.asyncio
async def test_record_failure_writes_hypothesis_id_to_meta(session):
    from backend.agents.services.rag_service import RAGService
    from backend.alpha_semantic_validator import compute_expression_hash  # noqa
    from backend.knowledge_extraction import expression_to_skeleton
    rag = RAGService(session)

    expr = _expr(0)
    skel = expression_to_skeleton(expr)
    ok = await rag.record_failure_pattern(
        expression=expr,
        error_type="LOW_SHARPE",
        metrics={"sharpe": 0.4, "fitness": 0.2, "turnover": 0.5},
        region="USA",
        dataset_id="pv1",
        hypothesis_id=12345,
        experiment_variant="b8-test",
    )
    assert ok is True

    rows = await _find_by_skeleton(session, skel, "FAILURE_PITFALL")
    assert len(rows) == 1
    md = rows[0].meta_data or {}
    assert md.get("hypothesis_id") == 12345
    assert md.get("hypothesis_ids") == [12345]
    assert md.get("experiment_variant") == "b8-test"


@pytest.mark.asyncio
async def test_record_success_writes_hypothesis_id_to_meta(session):
    from backend.agents.services.rag_service import RAGService
    from backend.knowledge_extraction import expression_to_skeleton
    rag = RAGService(session)

    expr = _expr(1)
    skel = expression_to_skeleton(expr)
    ok = await rag.record_success_pattern(
        expression=expr,
        metrics={"sharpe": 1.8, "fitness": 0.7, "turnover": 0.3},
        region="USA",
        dataset_id="pv1",
        alpha_id="b8aid01",
        hypothesis_id=67890,
        experiment_variant="b8-test",
    )
    assert ok is True

    rows = await _find_by_skeleton(session, skel, "SUCCESS_PATTERN")
    assert len(rows) == 1
    md = rows[0].meta_data or {}
    assert md.get("hypothesis_id") == 67890
    assert md.get("hypothesis_ids") == [67890]
    assert md.get("experiment_variant") == "b8-test"


@pytest.mark.asyncio
async def test_repeated_record_accumulates_hypothesis_ids(session):
    """Same pattern hit by 2 different hypotheses → meta_data.hypothesis_ids
    grows to include both, deduped on repeat."""
    from backend.agents.services.rag_service import RAGService
    from backend.knowledge_extraction import expression_to_skeleton
    rag = RAGService(session)

    expr = _expr(2)
    skel = expression_to_skeleton(expr)
    await rag.record_failure_pattern(
        expression=expr, error_type="LOW_SHARPE",
        metrics={"sharpe": 0.5}, region="USA", dataset_id="pv1",
        hypothesis_id=100, experiment_variant="v1",
    )
    await rag.record_failure_pattern(
        expression=expr, error_type="LOW_SHARPE",
        metrics={"sharpe": 0.6}, region="USA", dataset_id="pv1",
        hypothesis_id=101, experiment_variant="v1",
    )
    await rag.record_failure_pattern(
        expression=expr, error_type="LOW_SHARPE",
        metrics={"sharpe": 0.7}, region="USA", dataset_id="pv1",
        hypothesis_id=100, experiment_variant="v1",
    )

    rows = await _find_by_skeleton(session, skel, "FAILURE_PITFALL")
    assert len(rows) == 1
    md = rows[0].meta_data or {}
    hids = md.get("hypothesis_ids") or []
    assert sorted(hids) == [100, 101], f"expected dedup [100,101], got {hids}"
    assert md.get("failure_count") == 3


@pytest.mark.asyncio
async def test_record_without_hypothesis_id_keeps_null(session):
    """Backwards compat: legacy callers don't pass hypothesis_id; the entry
    still records with hypothesis_id=None / empty list."""
    from backend.agents.services.rag_service import RAGService
    from backend.knowledge_extraction import expression_to_skeleton
    rag = RAGService(session)

    expr = _expr(3)
    skel = expression_to_skeleton(expr)
    await rag.record_failure_pattern(
        expression=expr, error_type="LOW_SHARPE",
        metrics={"sharpe": 0.3}, region="USA", dataset_id="pv1",
    )

    rows = await _find_by_skeleton(session, skel, "FAILURE_PITFALL")
    assert len(rows) == 1
    md = rows[0].meta_data or {}
    assert md.get("hypothesis_id") is None
    assert md.get("hypothesis_ids") == []
    assert md.get("experiment_variant") is None


# =============================================================================
# Retrieval path — hypothesis_id / variant filters
# =============================================================================

@pytest.mark.asyncio
async def test_get_recent_pass_examples_filters_by_hypothesis_id(session):
    """Provide hypothesis_id → matching entries preferred over non-matching.
    Uses 3 distinct skeletons to avoid the merge-into-one effect."""
    from backend.agents.services.rag_service import RAGService
    from backend.knowledge_extraction import expression_to_skeleton
    rag = RAGService(session)

    # 3 distinct operators → 3 distinct skeletons. Capture skeletons so we
    # can verify retrieval result.
    expr200a, expr200b, expr201 = _expr(4), _expr(5), _expr(6)
    skel200a = expression_to_skeleton(expr200a)
    skel200b = expression_to_skeleton(expr200b)
    skel201 = expression_to_skeleton(expr201)
    assert len({skel200a, skel200b, skel201}) == 3

    await rag.record_success_pattern(
        expression=expr200a, metrics={"sharpe": 1.5, "fitness": 0.6, "turnover": 0.3},
        region="USA", dataset_id="pv1", alpha_id="b8r01", hypothesis_id=200,
    )
    await rag.record_success_pattern(
        expression=expr200b, metrics={"sharpe": 1.6, "fitness": 0.6, "turnover": 0.3},
        region="USA", dataset_id="pv1", alpha_id="b8r02", hypothesis_id=200,
    )
    await rag.record_success_pattern(
        expression=expr201, metrics={"sharpe": 1.7, "fitness": 0.6, "turnover": 0.3},
        region="USA", dataset_id="pv1", alpha_id="b8r03", hypothesis_id=201,
    )

    out_h200 = await rag.get_recent_pass_examples(
        region="USA", dataset_id="pv1", limit=50,
        hypothesis_id=200,
    )
    returned_skels = {e["pattern"] for e in out_h200}
    assert skel200a in returned_skels, "h=200a skeleton missing"
    assert skel200b in returned_skels, "h=200b skeleton missing"
    assert skel201 not in returned_skels, "h=201 leaked through h=200 filter"


@pytest.mark.asyncio
async def test_get_recent_pass_examples_filters_by_variant(session):
    """variant filter drops entries with mismatching experiment_variant
    (Plan v5+ F-5). Entries WITHOUT variant pass through (legacy compat)."""
    from backend.agents.services.rag_service import RAGService
    from backend.knowledge_extraction import expression_to_skeleton
    rag = RAGService(session)

    expr_v1, expr_v2, expr_legacy = _expr(7), _expr(8), _expr(9)
    skel_v1 = expression_to_skeleton(expr_v1)
    skel_v2 = expression_to_skeleton(expr_v2)
    skel_legacy = expression_to_skeleton(expr_legacy)

    await rag.record_success_pattern(
        expression=expr_v1, metrics={"sharpe": 1.5, "fitness": 0.6, "turnover": 0.3},
        region="USA", dataset_id="pv1", alpha_id="b8v01",
        experiment_variant="v1",
    )
    await rag.record_success_pattern(
        expression=expr_v2, metrics={"sharpe": 1.6, "fitness": 0.6, "turnover": 0.3},
        region="USA", dataset_id="pv1", alpha_id="b8v02",
        experiment_variant="v2",
    )
    await rag.record_success_pattern(
        expression=expr_legacy, metrics={"sharpe": 1.7, "fitness": 0.6, "turnover": 0.3},
        region="USA", dataset_id="pv1", alpha_id="b8v03",
        # no variant — legacy passthrough
    )

    out_v1 = await rag.get_recent_pass_examples(
        region="USA", dataset_id="pv1", limit=50,
        experiment_variant="v1",
    )
    returned_skels = {e["pattern"] for e in out_v1}
    assert skel_v1 in returned_skels, "v1 skel missing"
    assert skel_legacy in returned_skels, "legacy (no-variant) skel dropped"
    assert skel_v2 not in returned_skels, "v2 leaked through v1 filter"


@pytest.mark.asyncio
async def test_get_recent_pass_no_filter_returns_all(session):
    """When no hypothesis_id / variant filter is provided, behavior is the
    legacy mixed retrieval (Phase 2 must not regress level<2 callers)."""
    from backend.agents.services.rag_service import RAGService
    from backend.knowledge_extraction import expression_to_skeleton
    rag = RAGService(session)

    seeded = []
    for i, hid in enumerate([300, 301, None]):
        e = _expr(i)
        seeded.append(expression_to_skeleton(e))
        await rag.record_success_pattern(
            expression=e,
            metrics={"sharpe": 1.5, "fitness": 0.6, "turnover": 0.3},
            region="USA", dataset_id="pv1",
            alpha_id=f"b8nf{i}",
            hypothesis_id=hid,
        )

    out = await rag.get_recent_pass_examples(
        region="USA", dataset_id="pv1", limit=50,
    )
    returned_skels = {e["pattern"] for e in out}
    for skel in seeded:
        assert skel in returned_skels, f"seeded skel {skel} missing"
