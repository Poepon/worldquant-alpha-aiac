"""Phase 3 R1b.3a: failure_tree helpers + record_failure_tree tests (2026-05-18).

Plan: ~/.claude/plans/phase3-r1b-costeer-loop-2026-05-18.md v1.3 §7.

R1b.3 sub-phase first PR. These tests verify:
  - Pure tree-build helpers (_aggregate_attributions / _build_tree_from_chain /
    _walk_tree / _extract_root_skeleton) with synthetic in-memory data
  - record_failure_tree flag-gating (default OFF returns False)
  - record_failure_tree DB UPSERT (insert when missing, update + flag_modified
    when existing) — uses mock AsyncSession + scalar() chain
  - Soft-fail on DB exception
  - Max-depth cap truncates deep chains
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.knowledge_extraction import (
    _aggregate_attributions,
    _build_tree_from_chain,
    _extract_root_skeleton,
    _walk_tree,
    record_failure_tree,
)


# ---------------------------------------------------------------------------
# _aggregate_attributions
# ---------------------------------------------------------------------------

def test_aggregate_attributions_empty_returns_zero_counts():
    out = _aggregate_attributions([])
    assert out == {"hypothesis": 0, "implementation": 0, "both": 0, "unknown": 0}


def test_aggregate_attributions_mixed_counts_correctly():
    rows = [
        {"triggering_attribution": "hypothesis"},
        {"triggering_attribution": "hypothesis"},
        {"triggering_attribution": "implementation"},
        {"triggering_attribution": "both"},
        {"triggering_attribution": "not-a-known-value"},  # falls into unknown
        {"triggering_attribution": None},  # falls into unknown
        None,  # defensive against None rows
    ]
    out = _aggregate_attributions(rows)
    assert out["hypothesis"] == 2
    assert out["implementation"] == 1
    assert out["both"] == 1
    assert out["unknown"] == 3


# ---------------------------------------------------------------------------
# _build_tree_from_chain
# ---------------------------------------------------------------------------

def test_build_tree_empty_chain_returns_none():
    assert _build_tree_from_chain([]) is None


def test_build_tree_single_node_chain():
    tree = _build_tree_from_chain([
        {"id": 1, "statement": "root thesis", "mutation_depth": 0},
    ])
    assert tree["hypothesis_id"] == 1
    assert tree["statement"] == "root thesis"
    assert tree["mutation_depth"] == 0
    assert tree["children"] == []
    assert tree["total_alphas_tried"] == 0
    assert tree["total_pass"] == 0


def test_build_tree_nests_children_correctly():
    chain = [
        {"id": 1, "statement": "root", "mutation_depth": 0},
        {"id": 2, "statement": "first mutation", "mutation_depth": 1,
         "diff_from_parent": "narrowed scope"},
        {"id": 3, "statement": "second mutation", "mutation_depth": 2,
         "diff_from_parent": "added time decay"},
    ]
    tree = _build_tree_from_chain(chain)
    assert tree["hypothesis_id"] == 1
    assert len(tree["children"]) == 1
    assert tree["children"][0]["hypothesis_id"] == 2
    assert tree["children"][0]["diff_from_parent"] == "narrowed scope"
    assert tree["children"][0]["children"][0]["hypothesis_id"] == 3
    assert tree["children"][0]["children"][0]["mutation_depth"] == 2


def test_build_tree_respects_max_depth_cap():
    """6 nodes with max_depth=3 → truncate to 4 nodes (root + 3 levels)."""
    chain = [{"id": i, "statement": f"node {i}"} for i in range(6)]
    tree = _build_tree_from_chain(chain, max_depth=3)
    # Count nodes via walk
    nodes = list(_walk_tree(tree))
    assert len(nodes) == 4
    assert nodes[0]["hypothesis_id"] == 0
    assert nodes[-1]["hypothesis_id"] == 3  # truncated at depth 3


# ---------------------------------------------------------------------------
# _walk_tree
# ---------------------------------------------------------------------------

def test_walk_tree_empty_yields_nothing():
    assert list(_walk_tree(None)) == []
    assert list(_walk_tree({})) == []


def test_walk_tree_dfs_order():
    tree = _build_tree_from_chain([
        {"id": 1, "statement": "root"},
        {"id": 2, "statement": "child"},
        {"id": 3, "statement": "grandchild"},
    ])
    ids = [n["hypothesis_id"] for n in _walk_tree(tree)]
    assert ids == [1, 2, 3]


# ---------------------------------------------------------------------------
# _extract_root_skeleton
# ---------------------------------------------------------------------------

def test_extract_skeleton_includes_root_statement():
    tree = _build_tree_from_chain([{"id": 1, "statement": "momentum thesis"}])
    sk = _extract_root_skeleton(tree)
    assert "momentum thesis" in sk
    assert sk.startswith("R1B_FAILURE_TREE:")


def test_extract_skeleton_handles_empty():
    assert _extract_root_skeleton(None) == "R1B_FAILURE_TREE: <empty>"
    assert _extract_root_skeleton({}) == "R1B_FAILURE_TREE: <empty>"


def test_extract_skeleton_truncates_long_statement():
    long_stmt = "x" * 500
    tree = _build_tree_from_chain([{"id": 1, "statement": long_stmt}])
    sk = _extract_root_skeleton(tree)
    # 200-char cap + prefix
    assert len(sk) <= 250


# ---------------------------------------------------------------------------
# record_failure_tree — DB orchestration with mocks
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_record_failure_tree_flag_off_returns_false(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_FAILURE_TREE", False, raising=False)
    db = SimpleNamespace(
        execute=AsyncMock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
        add=MagicMock(),
    )
    ok = await record_failure_tree(
        hypothesis_chain=[{"id": 1, "statement": "x"}],
        retry_log_rows=[],
        db=db,
    )
    assert ok is False
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_record_failure_tree_empty_chain_returns_false(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_FAILURE_TREE", True, raising=False)
    db = SimpleNamespace(
        execute=AsyncMock(), commit=AsyncMock(),
        rollback=AsyncMock(), add=MagicMock(),
    )
    ok = await record_failure_tree(
        hypothesis_chain=[], retry_log_rows=[], db=db,
    )
    assert ok is False


@pytest.mark.asyncio
async def test_record_failure_tree_inserts_new_entry_when_missing(monkeypatch):
    """Pattern not in KB → db.add() called + db.commit() awaited."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_FAILURE_TREE", True, raising=False)

    # Mock execute returning a scalars() chain that yields None (no existing row)
    scalars_chain = MagicMock()
    scalars_chain.first = MagicMock(return_value=None)
    result = MagicMock()
    result.scalars = MagicMock(return_value=scalars_chain)
    db = SimpleNamespace(
        execute=AsyncMock(return_value=result),
        commit=AsyncMock(),
        rollback=AsyncMock(),
        add=MagicMock(),
    )
    ok = await record_failure_tree(
        hypothesis_chain=[{"id": 1, "statement": "root thesis"}],
        retry_log_rows=[
            {"original_hypothesis_id": 1, "triggering_attribution": "hypothesis"},
        ],
        db=db,
    )
    assert ok is True
    db.add.assert_called_once()
    inserted = db.add.call_args[0][0]
    assert inserted.entry_type == "FAILURE_PITFALL"
    assert "failure_tree" in inserted.meta_data
    assert inserted.meta_data["source"] == "r1b_loop"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_record_failure_tree_updates_existing_entry(monkeypatch):
    """Existing pattern row → meta_data['failure_tree'] updated + flag_modified."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_FAILURE_TREE", True, raising=False)

    existing = SimpleNamespace(
        meta_data={"failure_tree": {"old": True}, "source": "r1b_loop"},
    )
    scalars_chain = MagicMock()
    scalars_chain.first = MagicMock(return_value=existing)
    result = MagicMock()
    result.scalars = MagicMock(return_value=scalars_chain)
    db = SimpleNamespace(
        execute=AsyncMock(return_value=result),
        commit=AsyncMock(),
        rollback=AsyncMock(),
        add=MagicMock(),
    )
    ok = await record_failure_tree(
        hypothesis_chain=[{"id": 1, "statement": "root thesis"}],
        retry_log_rows=[],
        db=db,
    )
    assert ok is True
    db.add.assert_not_called()
    assert "old" not in existing.meta_data["failure_tree"]
    assert "regenerated_at" in existing.meta_data
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_record_failure_tree_soft_fails_on_db_exception(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_FAILURE_TREE", True, raising=False)
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=RuntimeError("DB unreachable")),
        commit=AsyncMock(),
        rollback=AsyncMock(),
        add=MagicMock(),
    )
    try:
        ok = await record_failure_tree(
            hypothesis_chain=[{"id": 1, "statement": "x"}],
            retry_log_rows=[],
            db=db,
        )
    except Exception as e:
        pytest.fail(f"record_failure_tree must never raise; got: {e}")
    assert ok is False
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_record_failure_tree_aggregates_attributions_per_node(monkeypatch):
    """Each tree node's fail_attributions reflects only that node's rows."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_R1B_FAILURE_TREE", True, raising=False)

    captured_entries = []
    def _add(entry):
        captured_entries.append(entry)
    scalars_chain = MagicMock()
    scalars_chain.first = MagicMock(return_value=None)
    result = MagicMock()
    result.scalars = MagicMock(return_value=scalars_chain)
    db = SimpleNamespace(
        execute=AsyncMock(return_value=result),
        commit=AsyncMock(),
        rollback=AsyncMock(),
        add=MagicMock(side_effect=_add),
    )
    await record_failure_tree(
        hypothesis_chain=[
            {"id": 1, "statement": "root"},
            {"id": 2, "statement": "child"},
        ],
        retry_log_rows=[
            {"original_hypothesis_id": 1, "triggering_attribution": "hypothesis"},
            {"original_hypothesis_id": 1, "triggering_attribution": "hypothesis"},
            {"original_hypothesis_id": 2, "triggering_attribution": "implementation"},
        ],
        db=db,
    )
    tree = captured_entries[0].meta_data["failure_tree"]
    # Root (id=1) has 2 hypothesis-attributions
    assert tree["fail_attributions"]["hypothesis"] == 2
    assert tree["fail_attributions"]["implementation"] == 0
    # Child (id=2) has 1 implementation-attribution
    child = tree["children"][0]
    assert child["fail_attributions"]["implementation"] == 1
    assert child["fail_attributions"]["hypothesis"] == 0
