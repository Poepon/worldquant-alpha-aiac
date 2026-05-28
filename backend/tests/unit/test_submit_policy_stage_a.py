"""StageASubmitPolicy — the HARD "never auto-submit" invariant.

This is the safety gate behind the global NEVER auto-submit constraint.
Stage A's SubmitPolicy MUST return "queue" for every persisted winner,
regardless of input shape. A regression here would silently start
submitting alphas to BRAIN automatically — irreversible per submission
+ contradicts the entire Stage A → Stage B GO/STOP design (auto-submit
is the explicit Stage B unlock).

These tests are intentionally paranoid — over-test rather than under-test.
"""
from __future__ import annotations

import pytest

from backend.services.optimization.submit_policy import StageASubmitPolicy


@pytest.mark.asyncio
async def test_empty_input_returns_empty_list():
    p = StageASubmitPolicy()
    assert await p.decide([]) == []


@pytest.mark.asyncio
async def test_single_winner_returns_single_queue():
    p = StageASubmitPolicy()
    assert await p.decide([42]) == ["queue"]


@pytest.mark.asyncio
async def test_n_winners_return_n_queues():
    p = StageASubmitPolicy()
    out = await p.decide([1, 2, 3, 4, 5])
    assert out == ["queue", "queue", "queue", "queue", "queue"]


@pytest.mark.asyncio
async def test_none_entries_still_queue():
    """ON CONFLICT skips appear as None in the persisted_pks list. They
    still get a "queue" verdict so SubmitPolicy's output stays 1:1 in
    length with input (downstream telemetry depends on this)."""
    p = StageASubmitPolicy()
    out = await p.decide([10, None, 12, None])
    assert out == ["queue", "queue", "queue", "queue"]


@pytest.mark.asyncio
async def test_never_returns_submit():
    """The defining invariant. If this test ever passes by accident
    against a class returning `submit`, the global NEVER auto-submit
    constraint has been violated."""
    p = StageASubmitPolicy()
    for n in (0, 1, 5, 100):
        out = await p.decide(list(range(n)))
        assert "submit" not in out, (
            f"StageASubmitPolicy MUST NOT auto-submit (got {out})"
        )


@pytest.mark.asyncio
async def test_never_returns_skip():
    """skip is also off-table for Stage A — every winner goes to backlog
    for human review, even if Persister flagged it as suboptimal. skip
    only enters the vocabulary in Stage B+ when SubmitPolicy gains
    quality awareness."""
    p = StageASubmitPolicy()
    for n in (0, 1, 5, 100):
        out = await p.decide(list(range(n)))
        assert "skip" not in out, (
            f"StageASubmitPolicy MUST NOT use 'skip' yet (got {out})"
        )


@pytest.mark.asyncio
async def test_output_length_equals_input_length():
    """Index alignment invariant. SubmitPolicy.decide() MUST return a
    list with the same length as persisted_pks so callers can zip them
    1:1 for telemetry."""
    p = StageASubmitPolicy()
    for n in (0, 1, 3, 17, 100):
        out = await p.decide(list(range(n)))
        assert len(out) == n
