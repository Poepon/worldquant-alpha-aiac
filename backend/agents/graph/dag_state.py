"""Phase 2 R6 DAG state helpers (MCTS-lite, plan v1.0, 2026-05-18).

Pure-function module for the `runtime_state["dag"]` JSONB sub-key — mirrors
`evolution_strategy.py:ContextualDirectionBandit` pattern (round-trip via
JSONB, no DB / Celery / settings access required for unit tests).

Per plan v1.0 §2-§4:
- DAG schema v=1 with `nodes` dict + `current_selection` (single leaf id,
  not path-tuple per [V1.0-A1-1]) + `idx2loop_id` mapping + root_id + counts
- Node id scheme: `n_<run_id>_<round_idx>_<local_seq>` for human-readable
  SQL forensic queries + JSONB sort stability
- Selection: UCB1-lite for warm leaves + Thompson sampling for cold leaves
  (n_pulls < DAG_COLD_THRESHOLD); hybrid avoids divide-by-zero at n_pulls=0
- Reward 3-tier fallback (composite_score > sharpe/4 > 0.5) clipped [0.01, 0.99]
- Hard cap DAG_MAX_NODES=100 enforced WRITE-SIDE in add_node() per [V1.0-S4]
- Prune LRU + reward weighted; MUST preserve parent chain invariant per [V1.0-S6]
- status enum: active | inactive | family_capped | error; NULL → coerce active [V1.0-S3]

Caller (mining_tasks.py R6 integration, PR2) is responsible for:
- Reading task config + run.runtime_state to load DAG dict
- Calling add_node when new alpha generated
- Calling update_reward when round completes
- Calling select_next_parent before next round expansion
- Writing modified dag back to run.runtime_state["dag"] (flag_modified required)
"""
from __future__ import annotations

import logging
import math
import random
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema version + defaults
# ---------------------------------------------------------------------------

DAG_SCHEMA_VERSION = 1

VALID_STATUSES = ("active", "inactive", "family_capped", "error")


def _now_iso() -> str:
    """ISO-8601 UTC timestamp for created_at fields."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# init_dag — fresh DAG dict for a new run
# ---------------------------------------------------------------------------

def init_dag(*, run_id: int, root_tier: int = 1, root_dataset_id: str = "") -> Dict[str, Any]:
    """Create a new DAG dict with a single root node.

    The root is an active leaf with n_pulls=0, ready to be expanded.
    Returns a dict suitable for direct assignment to
    ``ExperimentRun.runtime_state["dag"]``.
    """
    root_id = f"n_{run_id}_0_0"
    now = _now_iso()
    return {
        "v": DAG_SCHEMA_VERSION,
        "nodes": {
            root_id: {
                "id": root_id,
                "parent_id": None,
                "round_idx": 0,
                "tier": root_tier,
                "dataset_id": root_dataset_id,
                "loop_id": run_id,
                "expression_signature": "",
                "reward": 0.5,           # Beta(1,1) prior mean
                "n_pulls": 0,
                "status": "active",
                "children": [],
                "created_at": now,
            },
        },
        "current_selection": root_id,
        "idx2loop_id": {root_id: run_id},
        "root_id": root_id,
        "node_count": 1,
        "max_depth_seen": 0,
        "last_pruned_at": None,
    }


# ---------------------------------------------------------------------------
# add_node — append a child node + enforce hard cap (per [V1.0-S4])
# ---------------------------------------------------------------------------

def add_node(
    dag: Dict[str, Any],
    *,
    parent_id: str,
    round_idx: int,
    tier: int,
    dataset_id: str = "",
    loop_id: int = 0,
    expression_signature: str = "",
    max_nodes: int = 100,
) -> str:
    """Add a child node under parent_id. Returns the new node_id.

    Enforces hard cap WRITE-SIDE per plan [V1.0-S4]: if adding would push
    node_count over max_nodes, prune_to_cap() is called first. Raises
    ValueError if parent_id missing or pruning can't make room.
    """
    if parent_id not in dag["nodes"]:
        raise ValueError(f"parent_id {parent_id!r} not in dag.nodes")

    # Enforce cap BEFORE insertion (per [V1.0-S4])
    if dag["node_count"] + 1 > max_nodes:
        prune_to_cap(dag, max_nodes=max_nodes - 1)  # leave room for new node
        if dag["node_count"] + 1 > max_nodes:
            raise ValueError(
                f"cannot add_node: after prune still over cap "
                f"(node_count={dag['node_count']}, max={max_nodes})"
            )

    parent = dag["nodes"][parent_id]
    # local_seq = current children count of parent (monotone per parent)
    local_seq = len(parent["children"])
    run_id = parent.get("loop_id", loop_id) or loop_id
    new_id = f"n_{run_id}_{round_idx}_{local_seq}"

    # Collision guard (e.g. parent's children list out-of-sync with nodes dict)
    while new_id in dag["nodes"]:
        local_seq += 1
        new_id = f"n_{run_id}_{round_idx}_{local_seq}"

    now = _now_iso()
    dag["nodes"][new_id] = {
        "id": new_id,
        "parent_id": parent_id,
        "round_idx": round_idx,
        "tier": tier,
        "dataset_id": dataset_id,
        "loop_id": loop_id,
        "expression_signature": expression_signature,
        "reward": 0.5,
        "n_pulls": 0,
        "status": "active",
        "children": [],
        "created_at": now,
    }
    parent["children"].append(new_id)
    dag["idx2loop_id"][new_id] = loop_id
    dag["node_count"] += 1

    # Track max depth
    depth = _depth_of(dag, new_id)
    if depth > dag.get("max_depth_seen", 0):
        dag["max_depth_seen"] = depth

    return new_id


# ---------------------------------------------------------------------------
# update_reward — fold a new reward into a node's running average
# ---------------------------------------------------------------------------

def update_reward(dag: Dict[str, Any], node_id: str, reward: float) -> None:
    """Online running-average reward update + n_pulls increment.

    Reward is clipped to [0.01, 0.99] to avoid Beta(0, *) or Beta(*, 0)
    updates per plan §4.5. Mean-weighted:
        new_reward = (old_reward * n_pulls + r) / (n_pulls + 1)
    """
    if node_id not in dag["nodes"]:
        logger.warning(f"[dag_state] update_reward node_id {node_id!r} missing")
        return
    node = dag["nodes"][node_id]
    r = max(0.01, min(0.99, float(reward)))
    n = int(node.get("n_pulls", 0) or 0)
    prev = float(node.get("reward", 0.5) or 0.5)
    node["reward"] = (prev * n + r) / (n + 1)
    node["n_pulls"] = n + 1


def mark_status(dag: Dict[str, Any], node_id: str, status: str) -> None:
    """Set a node's status. Invalid status → coerce to 'error' + warn."""
    if node_id not in dag["nodes"]:
        logger.warning(f"[dag_state] mark_status node_id {node_id!r} missing")
        return
    if status not in VALID_STATUSES:
        logger.warning(f"[dag_state] invalid status {status!r}, coercing to 'error'")
        status = "error"
    dag["nodes"][node_id]["status"] = status


# ---------------------------------------------------------------------------
# select_next_parent — UCB1-lite + Thompson cold-start (plan §4.2)
# ---------------------------------------------------------------------------

def select_next_parent(
    dag: Dict[str, Any],
    *,
    cold_threshold: int = 3,
    ucb_c: float = 1.4,
    rng: Optional[random.Random] = None,
) -> Optional[str]:
    """Pick the next leaf to expand. Returns node_id or None when no eligible.

    Algorithm per plan §4.2 (hybrid UCB1 + Thompson cold-start):
      1. Collect eligible leaves (status='active', no children)
      2. Partition by warmth: cold = n_pulls < cold_threshold; warm = rest
      3. only-cold: Thompson sample each Beta(α + r*n, β + (1-r)*n) → argmax
      4. only-warm: UCB1 score = (r/n) + c * sqrt(ln(total_pulls)/n) → argmax
      5. mixed: compute both winners, pick larger (UCB tanh-clipped to [0,1])
    """
    rng = rng or random
    leaves = [
        n for n in dag["nodes"].values()
        if (n.get("status") or "active") == "active" and not n.get("children")
    ]
    if not leaves:
        return None

    cold = [n for n in leaves if int(n.get("n_pulls", 0) or 0) < cold_threshold]
    warm = [n for n in leaves if int(n.get("n_pulls", 0) or 0) >= cold_threshold]

    def _thompson_sample(node) -> float:
        n = int(node.get("n_pulls", 0) or 0)
        r = float(node.get("reward", 0.5) or 0.5)
        # Beta(1 + r*n, 1 + (1-r)*n) — uniform Beta(1,1) prior + observed counts
        alpha = 1.0 + r * n
        beta = 1.0 + (1.0 - r) * n
        return rng.betavariate(alpha, beta)

    def _ucb1_score(node, total_pulls: int) -> float:
        n = int(node.get("n_pulls", 0) or 1)  # avoid /0 (warm guaranteed >= cold_threshold>=1)
        r = float(node.get("reward", 0.5) or 0.5)
        if total_pulls <= 0:
            return r
        return r + ucb_c * math.sqrt(math.log(total_pulls) / n)

    total_pulls = sum(int(n.get("n_pulls", 0) or 0) for n in leaves) or 1

    if not warm:
        # only-cold path: Thompson over all
        return max(cold, key=_thompson_sample)["id"]
    if not cold:
        # only-warm path: UCB1
        return max(warm, key=lambda x: _ucb1_score(x, total_pulls))["id"]

    # mixed: pick winners from each, compare normalized scores
    cold_winner = max(cold, key=_thompson_sample)
    warm_winner = max(warm, key=lambda x: _ucb1_score(x, total_pulls))
    cold_score = _thompson_sample(cold_winner)
    warm_score = _ucb1_score(warm_winner, total_pulls)
    # Clip UCB1 score to [0,1] via tanh per plan §4.2 step 5
    warm_score_clipped = math.tanh(warm_score)
    return (cold_winner if cold_score > warm_score_clipped else warm_winner)["id"]


# ---------------------------------------------------------------------------
# path_to_root — reconstruct ancestor chain from a leaf
# ---------------------------------------------------------------------------

def path_to_root(dag: Dict[str, Any], node_id: str, *, max_depth: int = 10) -> List[str]:
    """Return list of node_ids from root → leaf (inclusive).

    Bounded by max_depth to defend against cycles (shouldn't exist by
    construction but defensive). Returns [] if node_id missing.
    """
    if node_id not in dag["nodes"]:
        return []
    path: List[str] = []
    cur: Optional[str] = node_id
    visited = set()
    depth = 0
    while cur is not None and depth < max_depth:
        if cur in visited:
            logger.warning(f"[dag_state] path_to_root cycle at {cur!r}, breaking")
            break
        visited.add(cur)
        path.append(cur)
        node = dag["nodes"].get(cur)
        if node is None:
            break
        cur = node.get("parent_id")
        depth += 1
    path.reverse()
    return path


def _depth_of(dag: Dict[str, Any], node_id: str) -> int:
    """Compute depth (root = 0). Bounded by 1000 to defend against runaway."""
    n = dag["nodes"].get(node_id)
    if n is None:
        return -1
    depth = 0
    cur = n.get("parent_id")
    visited = set()
    while cur is not None and depth < 1000:
        if cur in visited:
            break
        visited.add(cur)
        depth += 1
        parent = dag["nodes"].get(cur)
        if parent is None:
            break
        cur = parent.get("parent_id")
    return depth


# ---------------------------------------------------------------------------
# prune_to_cap — LRU + reward-weighted; preserves parent chain [V1.0-S6]
# ---------------------------------------------------------------------------

def prune_to_cap(dag: Dict[str, Any], *, max_nodes: int = 100) -> int:
    """Drop nodes to bring node_count <= max_nodes.

    Per plan §7.2:
      1. Never prune root, never prune active path-to-root from current_selection
      2. Inactive nodes first (LRU by created_at), then lowest-reward
      3. Preserve parent chain invariant: every remaining node's parent_id
         either is None (root) or still in nodes [V1.0-S6]

    Returns number of nodes dropped.
    """
    if dag["node_count"] <= max_nodes:
        return 0

    # Protected nodes: root + path-to-root from current_selection
    protected: set = {dag.get("root_id")}
    sel = dag.get("current_selection")
    if sel:
        protected.update(path_to_root(dag, sel))
    protected.discard(None)

    # Candidates for pruning: everything not protected
    candidates = [
        nid for nid, n in dag["nodes"].items() if nid not in protected
    ]
    if not candidates:
        return 0

    # Sort: inactive first (status not active), then by reward asc, then by created_at asc
    def _prune_key(nid: str) -> tuple:
        n = dag["nodes"][nid]
        is_inactive = 0 if (n.get("status") or "active") != "active" else 1
        reward = float(n.get("reward", 0.5) or 0.5)
        created = n.get("created_at", "") or ""
        return (is_inactive, reward, created)

    candidates.sort(key=_prune_key)

    to_drop_count = dag["node_count"] - max_nodes
    dropped = 0
    for nid in candidates:
        if dropped >= to_drop_count:
            break
        # Defense in depth: don't drop a node that any other node still points to
        # as parent (preserves parent chain invariant [V1.0-S6])
        has_child = any(
            other.get("parent_id") == nid
            for other_id, other in dag["nodes"].items()
            if other_id != nid and other_id not in candidates[:candidates.index(nid)]
        )
        if has_child:
            continue
        # Remove from parent's children list
        parent_id = dag["nodes"][nid].get("parent_id")
        if parent_id and parent_id in dag["nodes"]:
            try:
                dag["nodes"][parent_id]["children"].remove(nid)
            except ValueError:
                pass
        # Remove from idx2loop_id
        dag.get("idx2loop_id", {}).pop(nid, None)
        # Remove from nodes
        del dag["nodes"][nid]
        dropped += 1

    dag["node_count"] -= dropped
    dag["last_pruned_at"] = _now_iso()
    return dropped


# ---------------------------------------------------------------------------
# Serialization helpers — round-trip safety
# ---------------------------------------------------------------------------

def to_dict(dag: Dict[str, Any]) -> Dict[str, Any]:
    """Return JSONB-safe deep copy (defensive — JSONB stores dict by reference,
    callers that mutate the returned dict could corrupt the underlying
    runtime_state). Used by tests + integration sites needing snapshot."""
    import copy
    return copy.deepcopy(dag)


def from_dict(d: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Parse a runtime_state["dag"] dict. Handles legacy / partial / NULL:
    - None → return empty dict (caller decides whether to init_dag)
    - {} or missing v → return as-is (caller sees empty + falls through)
    - v != 1 → log warn, return as-is (forward-compat)
    """
    if d is None:
        return {}
    if not isinstance(d, dict):
        logger.warning(f"[dag_state] from_dict got non-dict {type(d).__name__}, ignoring")
        return {}
    v = d.get("v")
    if v is not None and v != DAG_SCHEMA_VERSION:
        logger.warning(f"[dag_state] from_dict v={v} != {DAG_SCHEMA_VERSION}, partial support")
    return d


__all__ = [
    "DAG_SCHEMA_VERSION",
    "VALID_STATUSES",
    "init_dag",
    "add_node",
    "update_reward",
    "mark_status",
    "select_next_parent",
    "path_to_root",
    "prune_to_cap",
    "to_dict",
    "from_dict",
]
