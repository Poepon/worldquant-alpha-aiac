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
    # Bug #9 fix: root (internal, n_pulls<3) is now also eligible for re-expansion.
    # Eligible set = {root, n2}; n1 (inactive) excluded. Either valid; assert no inactive.
    sel = select_next_parent(d, cold_threshold=3)
    assert sel in (d["root_id"], n2)
    assert sel != n1


def test_select_next_parent_skips_family_capped():
    d = init_dag(run_id=1)
    n1 = add_node(d, parent_id=d["root_id"], round_idx=1, tier=1)
    n2 = add_node(d, parent_id=d["root_id"], round_idx=1, tier=1)
    mark_status(d, n1, "family_capped")
    # Bug #9 fix: root also eligible (internal + n_pulls<3); n1 (family_capped) excluded.
    sel = select_next_parent(d, cold_threshold=3)
    assert sel in (d["root_id"], n2)
    assert sel != n1


def test_select_next_parent_no_eligible_returns_none():
    d = init_dag(run_id=1)
    mark_status(d, d["root_id"], "inactive")
    assert select_next_parent(d) is None


def test_select_next_parent_internal_eligible_below_threshold():
    """Bug #9 fix: internal node with n_pulls<N_PULLS_RE_EXPAND_THRESHOLD
    remains eligible so promising subtrees can re-expand (plan §4.2 hybrid)."""
    from backend.agents.graph.dag_state import N_PULLS_RE_EXPAND_THRESHOLD
    assert N_PULLS_RE_EXPAND_THRESHOLD == 3
    d = init_dag(run_id=1)
    # Add one child → root is now internal with n_pulls=0 (below threshold).
    child = add_node(d, parent_id=d["root_id"], round_idx=1, tier=1)
    # Mark child inactive so root is the only active eligible candidate.
    mark_status(d, child, "inactive")
    sel = select_next_parent(d, cold_threshold=3)
    assert sel == d["root_id"]  # internal node re-eligible


def test_select_next_parent_internal_excluded_above_threshold():
    """Bug #9 fix boundary: once internal n_pulls >= threshold, only its
    descendant leaves are eligible (legacy leaf-only semantics restored)."""
    d = init_dag(run_id=1)
    child = add_node(d, parent_id=d["root_id"], round_idx=1, tier=1)
    # Push root past N_PULLS_RE_EXPAND_THRESHOLD=3
    for _ in range(3):
        update_reward(d, d["root_id"], 0.5)
    assert d["nodes"][d["root_id"]]["n_pulls"] == 3
    sel = select_next_parent(d, cold_threshold=3)
    assert sel == child  # root no longer eligible (internal + n_pulls>=3)


def test_select_next_parent_warm_picks_high_score():
    """All warm — UCB1 picks high-reward leaf (deterministic since no rng).

    Bug #9 fix: root is now internal+cold (eligible for re-expansion); warm
    root above the re-expand threshold to keep this test about pure UCB1.
    """
    d = init_dag(run_id=1)
    # Make root non-leaf
    n_low = add_node(d, parent_id=d["root_id"], round_idx=1, tier=1)
    n_high = add_node(d, parent_id=d["root_id"], round_idx=1, tier=1)
    # Warm both up
    for _ in range(5):
        update_reward(d, n_low, 0.2)
        update_reward(d, n_high, 0.8)
    # Warm root too (push past N_PULLS_RE_EXPAND_THRESHOLD=3) with low reward
    # so it's no longer eligible as an internal node.
    for _ in range(5):
        update_reward(d, d["root_id"], 0.1)
    sel = select_next_parent(d, cold_threshold=3, ucb_c=0.0)  # c=0 → exploit only
    assert sel == n_high


def test_select_next_parent_cold_uses_thompson():
    """Cold path uses Beta sampling — deterministic via seeded rng.

    Bug #9 fix: root is also a cold internal node, so it joins the eligible
    pool. Assert that some cold node is selected (root, n1, or n2 all valid).
    """
    d = init_dag(run_id=1)
    n1 = add_node(d, parent_id=d["root_id"], round_idx=1, tier=1)
    n2 = add_node(d, parent_id=d["root_id"], round_idx=1, tier=1)
    update_reward(d, n1, 0.9)  # 1 pull, cold
    update_reward(d, n2, 0.1)  # 1 pull, cold
    rng = random.Random(42)
    sel = select_next_parent(d, cold_threshold=3, rng=rng)
    # Selected one of the three eligible cold nodes (root joined post-Bug-#9 fix)
    assert sel in (d["root_id"], n1, n2)


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
    # Spread 20 nodes across distinct subtrees to respect the MEDIUM-N3
    # per-parent cap (MAX_CHILDREN_PER_PARENT=8). Build 4 chains of depth 5
    # off the root → 20 nodes, no single parent exceeds 5 children.
    for c in range(4):
        cur = d["root_id"]
        for i in range(5):
            cur = add_node(d, parent_id=cur, round_idx=c * 5 + i + 1, tier=1)
            update_reward(d, cur, 0.9)
    prune_to_cap(d, max_nodes=5)
    assert d["root_id"] in d["nodes"]


def test_prune_to_cap_preserves_current_selection_path():
    """[V1.0-S6] active path-to-root from current_selection preserved."""
    d = init_dag(run_id=1)
    n1 = add_node(d, parent_id=d["root_id"], round_idx=1, tier=1)
    n2 = add_node(d, parent_id=n1, round_idx=2, tier=2)
    n3 = add_node(d, parent_id=n2, round_idx=3, tier=3)
    d["current_selection"] = n3
    # Add many sibling subtrees off root to force prune. Per-parent cap
    # (MEDIUM-N3, MAX_CHILDREN_PER_PARENT=8) constrains direct children of
    # any one node, so spread across multiple second-level parents.
    parents = [d["root_id"]]
    for i in range(20):
        try:
            sib = add_node(d, parent_id=parents[-1], round_idx=i + 10, tier=1)
        except ValueError:
            # Per-parent cap reached: promote latest sib as new parent
            parents.append(sib)
            sib = add_node(d, parent_id=parents[-1], round_idx=i + 10, tier=1)
        mark_status(d, sib, "inactive")
    prune_to_cap(d, max_nodes=5)
    # path n3 → n2 → n1 → root all preserved
    for nid in (d["root_id"], n1, n2, n3):
        assert nid in d["nodes"], f"path node {nid} got pruned!"


def test_prune_to_cap_parent_chain_invariant():
    """[V1.0-S6] every remaining node's parent_id either None or in nodes."""
    d = init_dag(run_id=1)
    n1 = add_node(d, parent_id=d["root_id"], round_idx=1, tier=1)
    # Per-parent cap (MEDIUM-N3 fix) limits n1 to 8 direct children; build a
    # bushier 2-level structure to still produce ~15 inactive nodes for prune.
    cur_parent = n1
    count = 0
    for i in range(15):
        try:
            sib = add_node(d, parent_id=cur_parent, round_idx=i + 2, tier=1)
        except ValueError:
            cur_parent = sib  # promote latest sib once cap hits
            sib = add_node(d, parent_id=cur_parent, round_idx=i + 2, tier=1)
        mark_status(d, sib, "inactive")
        count += 1
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


# ===========================================================================
# PR2 integration helpers
# ===========================================================================

from types import SimpleNamespace

from backend.agents.graph.dag_state import (
    add_children_for_phase,
    compute_reward_for_node,
    load_or_init,
    mark_family_capped_children,
)


def _mk_alpha(expr: str, composite: float = None, sharpe: float = None,
              family_capped: bool = False):
    """Build a SimpleNamespace mimicking AlphaCandidate for DAG tests."""
    metrics = {}
    if composite is not None:
        metrics["composite_score"] = composite
    if sharpe is not None:
        metrics["sharpe"] = sharpe
    if family_capped:
        metrics["_r10_family_cap_dropped"] = True
    return SimpleNamespace(expression=expr, metrics=metrics)


# --- load_or_init ---

def test_load_or_init_none_creates_fresh():
    d = load_or_init(None, run_id=42)
    assert d["root_id"] == "n_42_0_0"
    assert d["node_count"] == 1


def test_load_or_init_empty_dict_creates_fresh():
    d = load_or_init({}, run_id=42)
    assert d["root_id"] == "n_42_0_0"


def test_load_or_init_existing_preserved():
    existing = init_dag(run_id=42)
    n1 = add_node(existing, parent_id=existing["root_id"], round_idx=1, tier=2)
    update_reward(existing, n1, 0.7)
    snap = to_dict(existing)
    d = load_or_init(snap, run_id=42)
    assert d["node_count"] == 2
    assert d["nodes"][n1]["reward"] == pytest.approx(0.7)


def test_load_or_init_partial_falls_back_to_init():
    """Malformed (no root_id) → init fresh."""
    d = load_or_init({"v": 1, "nodes": {}}, run_id=42)
    assert d["root_id"] == "n_42_0_0"


# --- compute_reward_for_node ---

def test_compute_reward_composite_preferred():
    a = _mk_alpha("rank(close)", composite=0.8, sharpe=3.0)
    assert compute_reward_for_node(a) == pytest.approx(0.8)


def test_compute_reward_falls_back_to_sharpe():
    a = _mk_alpha("rank(close)", sharpe=2.0)  # no composite
    assert compute_reward_for_node(a) == pytest.approx(0.5)  # 2.0/4.0 = 0.5


def test_compute_reward_sharpe_clipped():
    a_high = _mk_alpha("x", sharpe=10.0)
    assert compute_reward_for_node(a_high) == pytest.approx(1.0)
    a_neg = _mk_alpha("x", sharpe=-2.0)
    assert compute_reward_for_node(a_neg) == pytest.approx(0.0)


def test_compute_reward_no_metrics_default():
    a = SimpleNamespace(expression="x", metrics=None)
    assert compute_reward_for_node(a) == 0.5


def test_compute_reward_node_dict_uses_existing_reward():
    """When passed a DAG node dict directly, read node['reward']."""
    n = {"reward": 0.65}
    assert compute_reward_for_node(n) == pytest.approx(0.65)


# --- add_children_for_phase ---

def test_add_children_for_phase_bulk_insert():
    d = init_dag(run_id=42)
    alphas = [
        _mk_alpha("rank(close)"),
        _mk_alpha("ts_mean(volume, 20)"),
        _mk_alpha("zscore(returns)"),
    ]
    ids = add_children_for_phase(
        d, parent_id=d["root_id"], round_idx=1, tier=2,
        dataset_id="pv1", loop_id=42, alphas=alphas,
    )
    assert len(ids) == 3
    for cid in ids:
        n = d["nodes"][cid]
        assert n["parent_id"] == d["root_id"]
        assert n["tier"] == 2
        assert n["dataset_id"] == "pv1"
        assert n["expression_signature"]  # non-empty sha prefix
    assert d["node_count"] == 4  # root + 3


def test_add_children_for_phase_empty_alphas():
    d = init_dag(run_id=42)
    ids = add_children_for_phase(d, parent_id=d["root_id"], round_idx=1, tier=1,
                                 dataset_id="pv1", loop_id=42, alphas=[])
    assert ids == []
    assert d["node_count"] == 1


def test_add_children_for_phase_signature_uniqueness():
    """Different expressions produce different signatures."""
    d = init_dag(run_id=42)
    alphas = [_mk_alpha("rank(close)"), _mk_alpha("rank(volume)")]
    ids = add_children_for_phase(d, parent_id=d["root_id"], round_idx=1, tier=1,
                                 dataset_id="x", loop_id=42, alphas=alphas)
    sigs = {d["nodes"][cid]["expression_signature"] for cid in ids}
    assert len(sigs) == 2


def test_add_children_for_phase_skips_bad_alpha():
    """Non-Alpha-like object → skip with warning (defensive soft-fail)."""
    d = init_dag(run_id=42)
    alphas = [_mk_alpha("rank(close)"), None, _mk_alpha("zscore(x)")]
    ids = add_children_for_phase(d, parent_id=d["root_id"], round_idx=1, tier=1,
                                 dataset_id="x", loop_id=42, alphas=alphas)
    # None entry skipped; 2 valid alphas → 2 ids
    assert len(ids) == 2


# --- mark_family_capped_children ---

def test_mark_family_capped_children_marks_dropped():
    d = init_dag(run_id=42)
    alphas = [
        _mk_alpha("ok1"),
        _mk_alpha("dropped1", family_capped=True),
        _mk_alpha("dropped2", family_capped=True),
        _mk_alpha("ok2"),
    ]
    ids = add_children_for_phase(d, parent_id=d["root_id"], round_idx=1, tier=1,
                                 dataset_id="x", loop_id=42, alphas=alphas)
    marked = mark_family_capped_children(d, ids, alphas)
    assert marked == 2
    assert d["nodes"][ids[0]]["status"] == "active"
    assert d["nodes"][ids[1]]["status"] == "family_capped"
    assert d["nodes"][ids[2]]["status"] == "family_capped"
    assert d["nodes"][ids[3]]["status"] == "active"


def test_mark_family_capped_children_length_mismatch_safe():
    """Defensive: child_ids and alphas different lengths → use min."""
    d = init_dag(run_id=42)
    ids = [add_node(d, parent_id=d["root_id"], round_idx=1, tier=1)]
    alphas = [_mk_alpha("a", family_capped=True), _mk_alpha("b", family_capped=True)]
    marked = mark_family_capped_children(d, ids, alphas)
    assert marked == 1  # only 1 id, 2 alphas → uses min


# ===========================================================================
# MEDIUM-N3 re-review fix (2026-05-18): reward propagation + per-parent cap
# ===========================================================================

from backend.agents.graph.dag_state import (
    MAX_CHILDREN_PER_PARENT,
    N_PULLS_RE_EXPAND_THRESHOLD,
    REWARD_PROPAGATION_DECAY,
)


def test_update_reward_propagates_to_ancestors():
    """MEDIUM-N3: update_reward(leaf, r) bumps n_pulls on every ancestor
    and credits decayed reward r * DECAY**distance per level (root sees
    smallest credit, parent biggest)."""
    d = init_dag(run_id=1)
    # Build chain A (root) → B → C
    b = add_node(d, parent_id=d["root_id"], round_idx=1, tier=1)
    c = add_node(d, parent_id=b, round_idx=2, tier=2)
    root_id = d["root_id"]

    # Fire one reward at the leaf
    update_reward(d, c, 1.0)

    # Reward clipped to 0.99 by update_reward (not 1.0), so use that as base.
    base = 0.99
    # Leaf: n_pulls=1, reward=0.99
    assert d["nodes"][c]["n_pulls"] == 1
    assert d["nodes"][c]["reward"] == pytest.approx(base)
    # B (distance=1): n_pulls=1, reward = (0.5*0 + base*0.5) / 1 = 0.495
    assert d["nodes"][b]["n_pulls"] == 1
    assert d["nodes"][b]["reward"] == pytest.approx(base * REWARD_PROPAGATION_DECAY)
    # Root A (distance=2): n_pulls=1, reward = base * 0.25 = 0.2475
    assert d["nodes"][root_id]["n_pulls"] == 1
    assert d["nodes"][root_id]["reward"] == pytest.approx(base * REWARD_PROPAGATION_DECAY ** 2)


def test_add_node_rejects_above_per_parent_cap():
    """MEDIUM-N3: per-parent cap MAX_CHILDREN_PER_PARENT enforced; the
    add_node call that would push a parent over the cap raises ValueError
    (same exception class as the global DAG_MAX_NODES guard)."""
    d = init_dag(run_id=1)
    # Add exactly MAX_CHILDREN_PER_PARENT children — all should succeed.
    for i in range(MAX_CHILDREN_PER_PARENT):
        add_node(d, parent_id=d["root_id"], round_idx=i + 1, tier=1)
    # Confirm parent is at cap.
    assert len(d["nodes"][d["root_id"]]["children"]) == MAX_CHILDREN_PER_PARENT
    # The next add must raise.
    with pytest.raises(ValueError, match="parent at child cap"):
        add_node(d, parent_id=d["root_id"], round_idx=99, tier=1)


def test_select_next_parent_internal_becomes_ineligible_after_n_pulls_reaches_threshold():
    """MEDIUM-N3: with reward propagation, firing enough rewards in the
    subtree pushes the internal node's n_pulls to N_PULLS_RE_EXPAND_THRESHOLD,
    making the FixF threshold actually gate re-expansion."""
    d = init_dag(run_id=1)
    # Build: root → mid → (leaf1, leaf2)
    mid = add_node(d, parent_id=d["root_id"], round_idx=1, tier=1)
    leaf1 = add_node(d, parent_id=mid, round_idx=2, tier=2)
    leaf2 = add_node(d, parent_id=mid, round_idx=2, tier=2)
    # Mark root inactive so it can't be picked — focus the assertion on `mid`.
    mark_status(d, d["root_id"], "inactive")

    # Fire N_PULLS_RE_EXPAND_THRESHOLD rewards across leaves; propagation
    # bumps mid.n_pulls each time.
    assert N_PULLS_RE_EXPAND_THRESHOLD == 3
    update_reward(d, leaf1, 0.6)
    update_reward(d, leaf2, 0.4)
    update_reward(d, leaf1, 0.5)
    assert d["nodes"][mid]["n_pulls"] == 3  # threshold reached

    # `mid` is now internal (has children) AND n_pulls >= threshold →
    # eligible filter excludes it. Only leaf1/leaf2 should be candidates.
    # Run select_next_parent many times with fresh RNG to enumerate the set.
    seen: set = set()
    rng = random.Random(0)
    for _ in range(50):
        sel = select_next_parent(d, cold_threshold=3, rng=rng)
        seen.add(sel)
    assert mid not in seen
    assert seen.issubset({leaf1, leaf2})
