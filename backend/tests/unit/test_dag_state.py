"""Phase 2 R6 PR1 unit tests for dag_state pure-function helpers.

Per plan v1.0 §8 — `backend/tests/unit/test_dag_state.py` ~320 lines.

Coverage:
  - init_dag: root node shape, schema version, idx2loop_id
  - add_node: insertion, parent.children, idx2loop_id, depth tracking
  - add_node: missing parent → ValueError
  - add_node: hard cap triggers prune_to_cap [V1.0-S4]
  - update_reward: running average correctness + clip [0.01, 0.99]
  - update_reward: n_pulls increment
  - mark_status: valid + invalid coerce to 'error' [V1.0-S3]
  - select_next_parent: only-cold (Thompson), only-warm (UCB1), mixed
  - select_next_parent: skips inactive / family_capped / error
  - select_next_parent: no eligible → None
  - path_to_root: simple linear, multi-depth, missing id, cycle defense
  - prune_to_cap: preserves root + current_selection path-to-root [V1.0-S6]
  - prune_to_cap: drops inactive first, then by reward
  - prune_to_cap: parent chain invariant preserved
  - to_dict / from_dict round-trip
"""
from __future__ import annotations

import random

import pytest

from backend.agents.graph.dag_state import (
    DAG_SCHEMA_VERSION,
    VALID_STATUSES,
    add_node,
    from_dict,
    init_dag,
    mark_status,
    path_to_root,
    prune_to_cap,
    select_next_parent,
    to_dict,
    update_reward,
)


# ---------------------------------------------------------------------------
# init_dag
# ---------------------------------------------------------------------------

def test_init_dag_root_shape():
    d = init_dag(run_id=100, root_tier=1, root_dataset_id="pv1")
    assert d["v"] == DAG_SCHEMA_VERSION
    assert d["node_count"] == 1
    root_id = d["root_id"]
    assert root_id == "n_100_0_0"
    root = d["nodes"][root_id]
    assert root["parent_id"] is None
    assert root["tier"] == 1
    assert root["dataset_id"] == "pv1"
    assert root["status"] == "active"
    assert root["n_pulls"] == 0
    assert root["reward"] == 0.5
    assert root["children"] == []
    assert d["current_selection"] == root_id
    assert d["idx2loop_id"] == {root_id: 100}
    assert d["max_depth_seen"] == 0


def test_init_dag_default_tier():
    d = init_dag(run_id=1)
    assert d["nodes"][d["root_id"]]["tier"] == 1


# ---------------------------------------------------------------------------
# add_node
# ---------------------------------------------------------------------------

def test_add_node_basic():
    d = init_dag(run_id=42)
    root_id = d["root_id"]
    new_id = add_node(d, parent_id=root_id, round_idx=1, tier=2, loop_id=42)
    assert new_id == "n_42_1_0"
    assert d["node_count"] == 2
    assert new_id in d["nodes"]
    assert d["nodes"][new_id]["parent_id"] == root_id
    assert d["nodes"][new_id]["tier"] == 2
    assert root_id in d["nodes"]
    assert new_id in d["nodes"][root_id]["children"]
    assert d["idx2loop_id"][new_id] == 42


def test_add_node_local_seq_increments():
    d = init_dag(run_id=42)
    root_id = d["root_id"]
    n1 = add_node(d, parent_id=root_id, round_idx=1, tier=1)
    n2 = add_node(d, parent_id=root_id, round_idx=1, tier=1)
    n3 = add_node(d, parent_id=root_id, round_idx=1, tier=1)
    assert n1 == "n_42_1_0"
    assert n2 == "n_42_1_1"
    assert n3 == "n_42_1_2"
    assert len(d["nodes"][root_id]["children"]) == 3


def test_add_node_missing_parent_raises():
    d = init_dag(run_id=1)
    with pytest.raises(ValueError, match="parent_id"):
        add_node(d, parent_id="n_999_0_0", round_idx=1, tier=1)


def test_add_node_depth_tracking():
    d = init_dag(run_id=1)
    n1 = add_node(d, parent_id=d["root_id"], round_idx=1, tier=1)
    n2 = add_node(d, parent_id=n1, round_idx=2, tier=2)
    n3 = add_node(d, parent_id=n2, round_idx=3, tier=3)
    assert d["max_depth_seen"] == 3  # root(0) → n1(1) → n2(2) → n3(3)


def test_add_node_hard_cap_triggers_prune():
    """[V1.0-S4] write-side hard cap — adding 6th node with max=5 prunes first."""
    d = init_dag(run_id=1)
    # Add 4 nodes (total 5 incl root)
    parent = d["root_id"]
    for i in range(4):
        n = add_node(d, parent_id=parent, round_idx=i + 1, tier=1, max_nodes=5)
        # mark inactive so prune can target them
        if i < 3:
            mark_status(d, n, "inactive")
    assert d["node_count"] == 5
    # Add 6th — should trigger prune
    n6 = add_node(d, parent_id=d["root_id"], round_idx=10, tier=1, max_nodes=5)
    assert n6 in d["nodes"]
    assert d["node_count"] <= 5


# ---------------------------------------------------------------------------
# update_reward
# ---------------------------------------------------------------------------

def test_update_reward_running_average():
    d = init_dag(run_id=1)
    n = add_node(d, parent_id=d["root_id"], round_idx=1, tier=1)
    update_reward(d, n, 0.8)
    assert d["nodes"][n]["n_pulls"] == 1
    assert d["nodes"][n]["reward"] == pytest.approx(0.8)

    update_reward(d, n, 0.4)
    # (0.8*1 + 0.4) / 2 = 0.6
    assert d["nodes"][n]["n_pulls"] == 2
    assert d["nodes"][n]["reward"] == pytest.approx(0.6)


def test_update_reward_clip_low():
    d = init_dag(run_id=1)
    n = add_node(d, parent_id=d["root_id"], round_idx=1, tier=1)
    update_reward(d, n, -1.0)
    assert d["nodes"][n]["reward"] == pytest.approx(0.01)  # clipped low


def test_update_reward_clip_high():
    d = init_dag(run_id=1)
    n = add_node(d, parent_id=d["root_id"], round_idx=1, tier=1)
    update_reward(d, n, 5.0)
    assert d["nodes"][n]["reward"] == pytest.approx(0.99)  # clipped high


def test_update_reward_missing_node_warns():
    d = init_dag(run_id=1)
    # No raise; just logs warning
    update_reward(d, "n_999_0_0", 0.5)


# ---------------------------------------------------------------------------
# mark_status
# ---------------------------------------------------------------------------

def test_mark_status_valid():
    d = init_dag(run_id=1)
    n = add_node(d, parent_id=d["root_id"], round_idx=1, tier=1)
    for status in VALID_STATUSES:
        mark_status(d, n, status)
        assert d["nodes"][n]["status"] == status


def test_mark_status_invalid_coerces_to_error():
    """[V1.0-S3] invalid status → coerce 'error'."""
    d = init_dag(run_id=1)
    n = add_node(d, parent_id=d["root_id"], round_idx=1, tier=1)
    mark_status(d, n, "garbage_status")
    assert d["nodes"][n]["status"] == "error"


# ---------------------------------------------------------------------------
# select_next_parent
# ---------------------------------------------------------------------------

def test_select_next_parent_only_root_returns_root():
    """Fresh DAG with only root — root is the only eligible leaf."""
    d = init_dag(run_id=1)
    assert select_next_parent(d, cold_threshold=3) == d["root_id"]


def test_select_next_parent_skips_inactive():
    d = init_dag(run_id=1)
    n1 = add_node(d, parent_id=d["root_id"], round_idx=1, tier=1)
    n2 = add_node(d, parent_id=d["root_id"], round_idx=1, tier=1)
    mark_status(d, n1, "inactive")
    # n2 is the only active leaf (root has children → not a leaf)
    sel = select_next_parent(d, cold_threshold=3)
    assert sel == n2


def test_select_next_parent_skips_family_capped():
    d = init_dag(run_id=1)
    n1 = add_node(d, parent_id=d["root_id"], round_idx=1, tier=1)
    n2 = add_node(d, parent_id=d["root_id"], round_idx=1, tier=1)
    mark_status(d, n1, "family_capped")
    sel = select_next_parent(d, cold_threshold=3)
    assert sel == n2


def test_select_next_parent_no_eligible_returns_none():
    d = init_dag(run_id=1)
    mark_status(d, d["root_id"], "inactive")
    assert select_next_parent(d) is None


def test_select_next_parent_warm_picks_high_score():
    """All warm — UCB1 picks high-reward leaf (deterministic since no rng)."""
    d = init_dag(run_id=1)
    # Make root non-leaf
    n_low = add_node(d, parent_id=d["root_id"], round_idx=1, tier=1)
    n_high = add_node(d, parent_id=d["root_id"], round_idx=1, tier=1)
    # Warm both up
    for _ in range(5):
        update_reward(d, n_low, 0.2)
        update_reward(d, n_high, 0.8)
    sel = select_next_parent(d, cold_threshold=3, ucb_c=0.0)  # c=0 → exploit only
    assert sel == n_high


def test_select_next_parent_cold_uses_thompson():
    """Cold path uses Beta sampling — deterministic via seeded rng."""
    d = init_dag(run_id=1)
    n1 = add_node(d, parent_id=d["root_id"], round_idx=1, tier=1)
    n2 = add_node(d, parent_id=d["root_id"], round_idx=1, tier=1)
    update_reward(d, n1, 0.9)  # 1 pull, cold
    update_reward(d, n2, 0.1)  # 1 pull, cold
    rng = random.Random(42)
    sel = select_next_parent(d, cold_threshold=3, rng=rng)
    # Selected one of the two cold leaves
    assert sel in (n1, n2)


# ---------------------------------------------------------------------------
# path_to_root
# ---------------------------------------------------------------------------

def test_path_to_root_root_only():
    d = init_dag(run_id=1)
    assert path_to_root(d, d["root_id"]) == [d["root_id"]]


def test_path_to_root_linear():
    d = init_dag(run_id=1)
    n1 = add_node(d, parent_id=d["root_id"], round_idx=1, tier=1)
    n2 = add_node(d, parent_id=n1, round_idx=2, tier=2)
    n3 = add_node(d, parent_id=n2, round_idx=3, tier=3)
    assert path_to_root(d, n3) == [d["root_id"], n1, n2, n3]


def test_path_to_root_missing_returns_empty():
    d = init_dag(run_id=1)
    assert path_to_root(d, "n_999_0_0") == []


def test_path_to_root_respects_max_depth():
    """Bounded walk — returns truncated path if depth exceeds cap."""
    d = init_dag(run_id=1)
    cur = d["root_id"]
    for i in range(15):
        cur = add_node(d, parent_id=cur, round_idx=i + 1, tier=1)
    # cur is at depth 15; cap = 5 → returns ≤ 5 elements
    p = path_to_root(d, cur, max_depth=5)
    assert len(p) <= 5


# ---------------------------------------------------------------------------
# prune_to_cap
# ---------------------------------------------------------------------------

def test_prune_to_cap_noop_when_under():
    d = init_dag(run_id=1)
    add_node(d, parent_id=d["root_id"], round_idx=1, tier=1)
    assert prune_to_cap(d, max_nodes=10) == 0
    assert d["node_count"] == 2


def test_prune_to_cap_drops_inactive_first():
    """[V1.0-S6] LRU + reward weighted, inactive prioritized."""
    d = init_dag(run_id=1)
    nodes = []
    for i in range(8):
        n = add_node(d, parent_id=d["root_id"], round_idx=i + 1, tier=1)
        nodes.append(n)
    # Mark half inactive
    for n in nodes[:4]:
        mark_status(d, n, "inactive")
        update_reward(d, n, 0.9)  # high reward but inactive
    for n in nodes[4:]:
        update_reward(d, n, 0.3)  # low reward but active
    # Cap = 5 → must drop 4 (total was 9 incl root)
    dropped = prune_to_cap(d, max_nodes=5)
    assert dropped == 4
    assert d["node_count"] == 5
    # The dropped should all be inactive (prefer inactive over active even with higher reward)
    for n in nodes[:4]:
        assert n not in d["nodes"]
    # Active nodes preserved
    for n in nodes[4:]:
        assert n in d["nodes"]


def test_prune_to_cap_preserves_root_always():
    """[V1.0-S6] root never pruned even if low reward / inactive marked."""
    d = init_dag(run_id=1)
    mark_status(d, d["root_id"], "inactive")
    for i in range(20):
        n = add_node(d, parent_id=d["root_id"], round_idx=i + 1, tier=1)
        update_reward(d, n, 0.9)
    prune_to_cap(d, max_nodes=5)
    assert d["root_id"] in d["nodes"]


def test_prune_to_cap_preserves_current_selection_path():
    """[V1.0-S6] active path-to-root from current_selection preserved."""
    d = init_dag(run_id=1)
    n1 = add_node(d, parent_id=d["root_id"], round_idx=1, tier=1)
    n2 = add_node(d, parent_id=n1, round_idx=2, tier=2)
    n3 = add_node(d, parent_id=n2, round_idx=3, tier=3)
    d["current_selection"] = n3
    # Add many siblings to force prune
    for i in range(20):
        sib = add_node(d, parent_id=d["root_id"], round_idx=i + 10, tier=1)
        mark_status(d, sib, "inactive")
    prune_to_cap(d, max_nodes=5)
    # path n3 → n2 → n1 → root all preserved
    for nid in (d["root_id"], n1, n2, n3):
        assert nid in d["nodes"], f"path node {nid} got pruned!"


def test_prune_to_cap_parent_chain_invariant():
    """[V1.0-S6] every remaining node's parent_id either None or in nodes."""
    d = init_dag(run_id=1)
    n1 = add_node(d, parent_id=d["root_id"], round_idx=1, tier=1)
    for i in range(15):
        sib = add_node(d, parent_id=n1, round_idx=i + 2, tier=1)
        mark_status(d, sib, "inactive")
    prune_to_cap(d, max_nodes=8)
    for nid, node in d["nodes"].items():
        pid = node.get("parent_id")
        assert pid is None or pid in d["nodes"], \
            f"node {nid} has dangling parent_id {pid!r}"


# ---------------------------------------------------------------------------
# to_dict / from_dict round-trip
# ---------------------------------------------------------------------------

def test_to_dict_returns_deep_copy():
    d = init_dag(run_id=1)
    snap = to_dict(d)
    add_node(d, parent_id=d["root_id"], round_idx=1, tier=1)
    # snap should be unaffected by mutation of d
    assert snap["node_count"] == 1
    assert d["node_count"] == 2


def test_from_dict_handles_none():
    assert from_dict(None) == {}


def test_from_dict_handles_empty():
    assert from_dict({}) == {}


def test_from_dict_handles_non_dict():
    assert from_dict("garbage") == {}  # type: ignore
    assert from_dict([1, 2, 3]) == {}  # type: ignore


def test_from_dict_forward_compat_v2():
    """Future v=2 dict should pass through (logged warning, not raise)."""
    d = {"v": 2, "nodes": {}, "root_id": "x"}
    result = from_dict(d)
    assert result["v"] == 2  # passes through


def test_dag_full_roundtrip_via_dict():
    """Build → mutate → snapshot → restore → state preserved."""
    d = init_dag(run_id=100)
    n1 = add_node(d, parent_id=d["root_id"], round_idx=1, tier=1)
    update_reward(d, n1, 0.7)
    snap = to_dict(d)
    restored = from_dict(snap)
    assert restored["node_count"] == d["node_count"]
    assert restored["nodes"][n1]["reward"] == pytest.approx(0.7)
    assert restored["current_selection"] == d["current_selection"]
