"""Unit tests for RAGService.get_recent_pass_examples cascade fallback.

Post tier-system removal (2026-05-18) the tier-minus-one cold-start trick
is gone. Replacement cascade in agents/services/rag_service.py:

  L1: pillar + dataset + window  (most specific)
  L2: relax pillar               (still dataset + window)
  L3: relax dataset              (region + window; effective_dataset_id=None)
  L4: relax window               (global by usage_count)

Each level appends rows; if cumulative len < 3 we step down. The
post-query dataset filter is gated by effective_dataset_id so L3/L4
truly surface cross-dataset rows (Round 6 nit fix in Ship #1).

The test class mocks db.execute so it doesn't need a real PG (avoids the
@requires_postgres mark) and verifies the call order + filter relaxation.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.agents.services.rag_service import RAGService


def _mk_kb_row(
    *,
    row_id: int,
    pattern: str,
    pillar: str = "momentum",
    dataset_id: str | None = None,
    usage_count: int = 1,
    created_by: str = "SYSTEM",
) -> SimpleNamespace:
    meta = {"hypothesis_pillar": pillar}
    if dataset_id:
        meta["dataset_id"] = dataset_id
    return SimpleNamespace(
        id=row_id,
        pattern=pattern,
        description="",
        meta_data=meta,
        usage_count=usage_count,
        created_by=created_by,
        created_at=datetime.utcnow(),
        entry_type="SUCCESS_PATTERN",
        is_active=True,
    )


@pytest.fixture
def mock_db_with_rows():
    """Build an AsyncSession whose .execute() returns a fixed list per call.

    Internal _filter_hallucinated + HITL-count make extra execute() calls
    we don't control here, so the mock returns the per-cascade-level
    SELECT result on every execute call cycle from the cascade. We use a
    "default" pattern: each batch is yielded as needed; once exhausted,
    a falsy/empty result is returned.

    The 0-arg scalar() call (HITL count + hallucination filter) gets a
    safe int(0); the .scalars().all() call gets the queued batch.
    """
    def _factory(batches: list):
        # Build one mock result per cascade query — each carries:
        #   .scalar() → 0 (for the HITL count and other internal aggregations)
        #   .scalars().all() → the per-level row batch
        # Excess execute() calls (e.g. _filter_hallucinated) reuse a safe
        # empty result via the side_effect callable.
        prepared = []
        for batch in batches:
            scalars_result = MagicMock()
            scalars_result.all = MagicMock(return_value=batch)
            res = MagicMock()
            res.scalars = MagicMock(return_value=scalars_result)
            res.scalar = MagicMock(return_value=0)
            prepared.append(res)
        empty = MagicMock()
        empty_scalars = MagicMock()
        empty_scalars.all = MagicMock(return_value=[])
        empty.scalars = MagicMock(return_value=empty_scalars)
        empty.scalar = MagicMock(return_value=0)

        # We yield empty for the FIRST call (HITL count), then walk
        # cascade batches, then empty forever for internal calls.
        sequence = [empty] + prepared
        iterator = iter(sequence)

        def _side_effect(*_args, **_kwargs):
            try:
                return next(iterator)
            except StopIteration:
                return empty

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_side_effect)
        return db
    return _factory


@pytest.mark.asyncio
async def test_l1_hit_returns_results(mock_db_with_rows):
    """When L1 (pillar + dataset + window) returns ≥3 rows, the method
    returns those rows without further work."""
    rows = [
        _mk_kb_row(row_id=i, pattern=f"ts_rank(close, {i})", pillar="momentum",
                   dataset_id="ds1")
        for i in range(1, 6)
    ]
    db = mock_db_with_rows([rows])
    svc = RAGService(db)
    out = await svc.get_recent_pass_examples(
        region="USA", dataset_id="ds1", limit=5,
        hypothesis_pillar="momentum",
    )
    assert len(out) >= 3
    # Each emitted row carries the pillar key (renamed from factor_tier in
    # plan §4.B), and each pattern came from the L1 batch.
    for r in out:
        assert r["hypothesis_pillar"] == "momentum"


@pytest.mark.asyncio
async def test_l1_empty_steps_to_l2(mock_db_with_rows):
    """L1 returns 0 → step to L2 (drop pillar filter)."""
    rows_l2 = [
        _mk_kb_row(row_id=i, pattern=f"ts_zscore(returns, {i})",
                   pillar="value", dataset_id="ds1")
        for i in range(1, 4)
    ]
    db = mock_db_with_rows([[], rows_l2])
    svc = RAGService(db)
    out = await svc.get_recent_pass_examples(
        region="USA", dataset_id="ds1", limit=5,
        hypothesis_pillar="momentum",
    )
    assert len(out) >= 3
    # All emitted rows came from the L2 batch (pillar=value).
    assert all(r["hypothesis_pillar"] == "value" for r in out)


@pytest.mark.asyncio
async def test_l1_l2_empty_steps_to_l3_dropping_dataset(mock_db_with_rows):
    """L1 + L2 empty → L3 relaxes pillar AND the post-filter dataset
    constraint (effective_dataset_id → None). Cross-dataset rows must
    survive the post-filter."""
    rows_l3 = [
        _mk_kb_row(row_id=i, pattern=f"group_rank(ts_rank(close, {i}), industry)",
                   pillar="quality", dataset_id="ds_OTHER")
        for i in range(1, 4)
    ]
    db = mock_db_with_rows([[], [], rows_l3])
    svc = RAGService(db)
    out = await svc.get_recent_pass_examples(
        region="USA", dataset_id="ds1", limit=5,
        hypothesis_pillar="momentum",
    )
    # ds_OTHER rows must survive — proves effective_dataset_id was nulled at L3.
    assert len(out) >= 1
    assert any(r["pattern"].startswith("group_rank") for r in out)


@pytest.mark.asyncio
async def test_all_empty_returns_empty_list(mock_db_with_rows):
    """All 4 cascade levels return empty → method returns []."""
    db = mock_db_with_rows([[], [], [], []])
    svc = RAGService(db)
    out = await svc.get_recent_pass_examples(
        region="USA", dataset_id="ds1", limit=5,
        hypothesis_pillar="momentum",
    )
    assert out == []
