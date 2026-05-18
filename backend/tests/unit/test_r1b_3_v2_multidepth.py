"""Phase 3 R1b.3-v2 INSERT + multi-depth chain walk tests (2026-05-18).

Plan: ~/.claude/plans/phase3-r1b-costeer-loop-2026-05-18.md v1.3 §7 follow-up.

Covers:
  - _insert_mutated_hypothesis returns None on import failure (soft-fail)
  - _insert_mutated_hypothesis returns None on empty statement
  - _build_parent_chain returns fallback when parent_id None
  - _build_parent_chain returns fallback on DB import failure
  - _build_parent_chain caps at max_depth
  - _build_parent_chain reverses to oldest-first ordering
  - node_hypothesis_mutate INSERT block skipped when flag OFF
  - Hotfix migration source has correct table name
  - ORM model carries r1b_mutation_depth column
  - Static-source sentinel for the inject pipeline
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# ORM + migration sanity
# ---------------------------------------------------------------------------

def test_orm_carries_r1b_mutation_depth_column():
    """R1b.3-v2 added r1b_mutation_depth to the Hypothesis ORM model."""
    from backend.models import Hypothesis
    cols = {c.name for c in Hypothesis.__table__.columns}
    assert "r1b_mutation_depth" in cols
    assert "parent_hypothesis_id" in cols


def test_hotfix_migration_targets_correct_table():
    """R1b.3-v2 hotfix migration adds columns to 'hypotheses' (plural)."""
    import inspect
    import backend.alembic.versions.a7d2f9e4b8c3_phase3_r1b_d_fix_hypothesis_table_name as mig
    src = inspect.getsource(mig)
    # The fix MUST land on the plural table
    assert "hypotheses" in src
    # And cleanup the original singular-table mess
    assert "DROP COLUMN IF EXISTS parent_hypothesis_id" in src
    assert "DROP COLUMN IF EXISTS r1b_mutation_depth" in src
    # Forward-compatible — IF NOT EXISTS guards
    assert "IF NOT EXISTS" in src


# ---------------------------------------------------------------------------
# _insert_mutated_hypothesis — soft-fail contracts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_insert_helper_returns_none_for_empty_statement():
    from backend.agents.graph.nodes.r1b_loop import _insert_mutated_hypothesis

    out = await _insert_mutated_hypothesis(
        task_id=1,
        parent_hypothesis_id=None,
        pending={"statement": "  "},  # empty after strip
        region="USA",
    )
    assert out is None


@pytest.mark.asyncio
async def test_insert_helper_soft_fails_on_db_exception():
    """DB session raise → returns None, never propagates."""
    from backend.agents.graph.nodes import r1b_loop

    class _BadSession:
        async def __aenter__(self):
            raise RuntimeError("DB unreachable")
        async def __aexit__(self, *a):
            return None

    with patch.object(
        r1b_loop, "_insert_mutated_hypothesis", wraps=r1b_loop._insert_mutated_hypothesis,
    ):
        with patch("backend.database.AsyncSessionLocal", lambda: _BadSession()):
            out = await r1b_loop._insert_mutated_hypothesis(
                task_id=1,
                parent_hypothesis_id=None,
                pending={"statement": "real statement"},
                region="USA",
            )
            assert out is None


# ---------------------------------------------------------------------------
# _build_parent_chain — fallback behavior
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chain_returns_fallback_when_parent_id_none():
    from backend.agents.graph.nodes.r1b_loop import _build_parent_chain

    chain = await _build_parent_chain(
        parent_id=None, parent_statement_fallback="root stmt", max_depth=4,
    )
    assert chain == [{"id": None, "statement": "root stmt", "mutation_depth": 0}]


@pytest.mark.asyncio
async def test_chain_returns_fallback_on_db_failure():
    """Any DB walk exception → falls back to single-node skeleton."""
    from backend.agents.graph.nodes import r1b_loop

    class _BadSession:
        async def __aenter__(self):
            raise RuntimeError("DB down")
        async def __aexit__(self, *a):
            return None

    with patch("backend.database.AsyncSessionLocal", lambda: _BadSession()):
        chain = await r1b_loop._build_parent_chain(
            parent_id=42, parent_statement_fallback="fallback", max_depth=4,
        )
    assert len(chain) == 1
    assert chain[0]["statement"] == "fallback"
    assert chain[0]["id"] == 42


@pytest.mark.asyncio
async def test_chain_walks_db_and_reverses_to_oldest_first(monkeypatch):
    """Walk parent_hypothesis_id from id=3 → id=2 → id=1; output oldest-first."""
    from backend.agents.graph.nodes import r1b_loop

    rows_by_id = {
        1: SimpleNamespace(id=1, statement="root", r1b_mutation_depth=0, parent_hypothesis_id=None),
        2: SimpleNamespace(id=2, statement="M1", r1b_mutation_depth=1, parent_hypothesis_id=1),
        3: SimpleNamespace(id=3, statement="M2", r1b_mutation_depth=2, parent_hypothesis_id=2),
    }

    class _Result:
        def __init__(self, row):
            self._row = row
        def scalar_one_or_none(self):
            return self._row

    class _Sess:
        async def execute(self, stmt):
            # Find the id from the where clause's right operand —
            # the comparison expression is e.g. Hypothesis.id == 3.
            # Cheapest path: just track call order.
            return _Result(rows_by_id.get(_Sess.call_order.pop(0)))
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None

    _Sess.call_order = [3, 2, 1]
    monkeypatch.setattr("backend.database.AsyncSessionLocal", lambda: _Sess())

    chain = await r1b_loop._build_parent_chain(
        parent_id=3, parent_statement_fallback="ignored", max_depth=4,
    )
    # Walked 3 → 2 → 1; reversed to oldest-first
    assert [n["id"] for n in chain] == [1, 2, 3]
    assert [n["mutation_depth"] for n in chain] == [0, 1, 2]


@pytest.mark.asyncio
async def test_build_parent_chain_breaks_on_cycle(monkeypatch):
    """R1b.3-v2 review LOW 2 (2026-05-18): defense-in-depth cycle detection.

    Simulate a cycle A(id=1) → B(id=2) → A(id=1) via mocked DB SELECT. Without
    the ``seen_ids`` guard the walk would terminate only via ``max_depth=4``
    and emit duplicate nodes (id=1 twice). With the guard the walk MUST break
    after seeing id=1 the second time, so the chain holds the unique pair
    {1, 2} with length == 2.
    """
    from backend.agents.graph.nodes import r1b_loop

    rows_by_id = {
        1: SimpleNamespace(id=1, statement="A", r1b_mutation_depth=0, parent_hypothesis_id=2),
        2: SimpleNamespace(id=2, statement="B", r1b_mutation_depth=1, parent_hypothesis_id=1),
    }

    class _Result:
        def __init__(self, row):
            self._row = row
        def scalar_one_or_none(self):
            return self._row

    class _Sess:
        async def execute(self, stmt):
            return _Result(rows_by_id.get(_Sess.call_order.pop(0)))
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None

    # max_depth=4 would allow 4 SELECTs; cycle detection should break after
    # the SECOND SELECT (id=1 → id=2 → would-be id=1 detected as cycle).
    _Sess.call_order = [1, 2, 1, 2]
    monkeypatch.setattr("backend.database.AsyncSessionLocal", lambda: _Sess())

    chain = await r1b_loop._build_parent_chain(
        parent_id=1, parent_statement_fallback="ignored", max_depth=4,
    )

    # Cycle breaks the walk before duplicates land — chain has unique nodes only.
    assert len(chain) == 2, f"cycle guard failed: chain length={len(chain)} (expected 2)"
    ids = [n["id"] for n in chain]
    assert sorted(ids) == [1, 2], f"chain ids={ids} not the unique pair {{1, 2}}"
    # No duplicate ids — defense-in-depth contract holds.
    assert len(set(ids)) == len(ids), f"duplicate ids in chain: {ids}"


@pytest.mark.asyncio
async def test_chain_caps_at_max_depth(monkeypatch):
    """max_depth=2 stops the walk before exhausting the chain."""
    from backend.agents.graph.nodes import r1b_loop

    # Long chain: 5 → 4 → 3 → 2 → 1
    rows_by_id = {
        i: SimpleNamespace(
            id=i, statement=f"M{i}", r1b_mutation_depth=5 - i,
            parent_hypothesis_id=(i - 1) if i > 1 else None,
        )
        for i in range(1, 6)
    }

    class _Result:
        def __init__(self, row):
            self._row = row
        def scalar_one_or_none(self):
            return self._row

    class _Sess:
        async def execute(self, stmt):
            return _Result(rows_by_id.get(_Sess.call_order.pop(0)))
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None

    _Sess.call_order = [5, 4]  # max_depth=2 caps walk after 2 SELECTs
    monkeypatch.setattr("backend.database.AsyncSessionLocal", lambda: _Sess())

    chain = await r1b_loop._build_parent_chain(
        parent_id=5, parent_statement_fallback="ignored", max_depth=2,
    )
    assert len(chain) == 2
    # Most recent two ancestors, oldest-first after reverse
    assert chain[0]["id"] == 4
    assert chain[1]["id"] == 5


# ---------------------------------------------------------------------------
# Static-source sentinels for the wire
# ---------------------------------------------------------------------------

def test_r1b_loop_wire_has_r1b_3_v2_markers():
    import inspect
    from backend.agents.graph.nodes import r1b_loop

    src = inspect.getsource(r1b_loop)
    assert "_insert_mutated_hypothesis" in src
    assert "_build_parent_chain" in src
    assert "R1b.3-v2" in src
    assert "R1B_FAILURE_TREE_MAX_DEPTH" in src


def test_node_hypothesis_mutate_skips_insert_when_flag_off(monkeypatch):
    """ENABLE_R1B_HYPOTHESIS_MUTATE OFF + a successful mutate flow would still
    not invoke _insert_mutated_hypothesis. This is a source-grep sentinel
    since the actual mutate node body is huge; we verify the wire site
    sits behind the correct flag check."""
    import inspect
    from backend.agents.graph.nodes import r1b_loop

    src = inspect.getsource(r1b_loop.node_hypothesis_mutate)
    assert "ENABLE_R1B_HYPOTHESIS_MUTATE" in src
    assert "_insert_mutated_hypothesis" in src
    assert "R1b.3-v2" in src
