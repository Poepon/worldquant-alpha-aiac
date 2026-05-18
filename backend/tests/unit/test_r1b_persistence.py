"""Phase 3 R1b.2c: cross-round persistence helper tests (2026-05-18).

Plan: ~/.claude/plans/phase3-r1b-costeer-loop-2026-05-18.md v1.3 §3.5 + §4.2.

R1b.2c closes the mutate sub-phase by adding the cross-round persistence
helper. The actual mining_tasks.py wire site is deferred to a follow-up
PR; these tests verify the helper behavior against mocked task/db.

Covers:
  - persist_after_round writes pending hypothesis + accumulates budget ledger
  - persist_after_round soft-fails (returns False, never raises) on errors
  - consume_pending_hypothesis pops + clears + returns dict
  - consume_pending_hypothesis returns None when slot is empty
  - get_budget_ledger sync read
  - Round-trip: persist N times then consume returns latest, ledger
    accumulates retries + mutations + cost
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agents.graph.nodes.r1b_persistence import (
    CONFIG_KEY_BUDGET_CONSUMED,
    CONFIG_KEY_PENDING_HYP,
    consume_pending_hypothesis,
    get_budget_ledger,
    persist_after_round,
)


def _mk_task(config=None):
    return SimpleNamespace(id=42, config=dict(config or {}))


def _mk_db():
    db = SimpleNamespace()
    db.commit = AsyncMock(return_value=None)
    db.rollback = AsyncMock(return_value=None)
    return db


def _mk_state(*, pending=None, retries=0, mutations=0, cost=0.0):
    return SimpleNamespace(
        r1b_pending_new_hypothesis=pending,
        r1b_retries_attempted_this_alpha=retries,
        r1b_mutations_attempted_this_cycle=mutations,
        r1b_token_cost_this_alpha=cost,
    )


# ---------------------------------------------------------------------------
# persist_after_round
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_persist_writes_pending_hypothesis():
    pending = {
        "statement": "new thesis",
        "rationale": "economic mechanism",
        "expected_signal": "momentum",
        "key_fields": ["close"],
        "parent_hypothesis_statement": "old thesis",
    }
    task = _mk_task()
    state = _mk_state(pending=pending)
    db = _mk_db()
    ok = await persist_after_round(state, task, db)
    assert ok is True
    assert task.config[CONFIG_KEY_PENDING_HYP]["statement"] == "new thesis"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_persist_writes_budget_ledger_accumulates():
    """Two consecutive rounds → ledger sums retries / mutations / cost."""
    task = _mk_task()
    db = _mk_db()
    await persist_after_round(_mk_state(retries=2, cost=0.01), task, db)
    await persist_after_round(_mk_state(retries=1, mutations=1, cost=0.02), task, db)
    ledger = task.config[CONFIG_KEY_BUDGET_CONSUMED]
    assert ledger["retries_total"] == 3
    assert ledger["mutations_total"] == 1
    assert abs(ledger["cost_usd_total"] - 0.03) < 1e-6


@pytest.mark.asyncio
async def test_persist_returns_false_when_no_changes():
    """No pending + no counters → no write, returns False."""
    task = _mk_task()
    state = _mk_state()  # all zeros, pending None
    db = _mk_db()
    ok = await persist_after_round(state, task, db)
    assert ok is False
    assert CONFIG_KEY_PENDING_HYP not in task.config
    assert CONFIG_KEY_BUDGET_CONSUMED not in task.config
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_persist_skips_pending_when_empty_statement():
    """Empty statement → don't persist as pending (treated as no-op)."""
    pending = {"statement": "", "rationale": "x"}
    task = _mk_task()
    state = _mk_state(pending=pending)
    db = _mk_db()
    ok = await persist_after_round(state, task, db)
    assert ok is False  # neither pending nor budget changed


@pytest.mark.asyncio
async def test_persist_soft_fail_on_commit_exception():
    """DB commit raises → helper returns False, rollback attempted, no raise."""
    task = _mk_task()
    state = _mk_state(retries=1, cost=0.01)
    db = _mk_db()
    db.commit.side_effect = RuntimeError("DB unreachable")
    try:
        ok = await persist_after_round(state, task, db)
    except Exception as e:
        pytest.fail(f"persist_after_round must never raise; got: {e}")
    assert ok is False
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_persist_soft_fail_on_missing_inputs():
    assert await persist_after_round(None, _mk_task(), _mk_db()) is False
    assert await persist_after_round(_mk_state(), None, _mk_db()) is False
    assert await persist_after_round(_mk_state(), _mk_task(), None) is False


# ---------------------------------------------------------------------------
# consume_pending_hypothesis
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_consume_returns_and_clears_pending():
    pending = {"statement": "thesis A", "rationale": "x"}
    task = _mk_task({CONFIG_KEY_PENDING_HYP: pending, "other_key": "kept"})
    db = _mk_db()
    out = await consume_pending_hypothesis(task, db)
    assert out is not None
    assert out["statement"] == "thesis A"
    # Slot cleared
    assert CONFIG_KEY_PENDING_HYP not in task.config
    # Other config preserved
    assert task.config["other_key"] == "kept"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_consume_returns_none_when_no_pending():
    task = _mk_task()
    db = _mk_db()
    out = await consume_pending_hypothesis(task, db)
    assert out is None
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_consume_skips_invalid_pending_dict():
    """Pending without a statement field → treated as empty."""
    task = _mk_task({CONFIG_KEY_PENDING_HYP: {"rationale": "no statement"}})
    db = _mk_db()
    out = await consume_pending_hypothesis(task, db)
    assert out is None


@pytest.mark.asyncio
async def test_consume_soft_fail_on_commit_exception():
    task = _mk_task({CONFIG_KEY_PENDING_HYP: {"statement": "x"}})
    db = _mk_db()
    db.commit.side_effect = RuntimeError("DB unreachable")
    try:
        out = await consume_pending_hypothesis(task, db)
    except Exception as e:
        pytest.fail(f"consume must never raise; got: {e}")
    assert out is None
    db.rollback.assert_awaited_once()


# ---------------------------------------------------------------------------
# get_budget_ledger
# ---------------------------------------------------------------------------

def test_get_budget_ledger_returns_dict():
    task = _mk_task({CONFIG_KEY_BUDGET_CONSUMED: {"retries_total": 5, "cost_usd_total": 0.05}})
    ledger = get_budget_ledger(task)
    assert ledger == {"retries_total": 5, "cost_usd_total": 0.05}


def test_get_budget_ledger_empty_when_missing():
    assert get_budget_ledger(_mk_task()) == {}


def test_get_budget_ledger_handles_none_task():
    assert get_budget_ledger(None) == {}


# ---------------------------------------------------------------------------
# Round-trip integration smoke
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_round_trip_persist_then_consume():
    """Full lifecycle: round 1 mutates → persist → round 2 consumes."""
    pending = {"statement": "round-1 mutated thesis"}
    task = _mk_task()
    db = _mk_db()

    # Round 1 — mutate fires
    state1 = _mk_state(pending=pending, mutations=1, cost=0.012)
    await persist_after_round(state1, task, db)
    assert task.config[CONFIG_KEY_PENDING_HYP]["statement"] == "round-1 mutated thesis"

    # Round 2 — outer loop pops the pending hypothesis to feed propose
    consumed = await consume_pending_hypothesis(task, db)
    assert consumed["statement"] == "round-1 mutated thesis"
    # Pending cleared but budget ledger retained
    assert CONFIG_KEY_PENDING_HYP not in task.config
    assert task.config[CONFIG_KEY_BUDGET_CONSUMED]["mutations_total"] == 1
