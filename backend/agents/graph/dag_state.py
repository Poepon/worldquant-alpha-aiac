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

# Re-expansion threshold (plan §4.2 hybrid intent): an internal (non-leaf)
# active node remains eligible for `select_next_parent` while its n_pulls
# is below this threshold. Otherwise the original leaf-only filter caused
# every parent to be expanded exactly once → DAG grew wide-only and UCB1
# n_pulls on internal nodes was meaningless.
N_PULLS_RE_EXPAND_THRESHOLD = 3

# MEDIUM-N3 re-review fix (2026-05-18) — reward propagation + per-parent cap.
# Without these two, N_PULLS_RE_EXPAND_THRESHOLD is vacuous in production
# because update_reward only bumped the freshly-added child, so internal
# nodes' n_pulls stayed at 0 forever → broadened filter always allowed them.
#
# REWARD_PROPAGATION_DECAY: MCTS-style backprop decay per ancestor level
# (root sees reward * 0.5^depth so deep leaves don't dominate shallow ones).
REWARD_PROPAGATION_DECAY = 0.5
# Same defensive bound as `path_to_root` / `_depth_of` — stops a corrupt
# parent chain from infinite-looping during reward backprop.
REWARD_PROPAGATION_MAX_DEPTH = 10

# MAX_CHILDREN_PER_PARENT: forces UCB1 to actually re-explore alternative
# parents instead of letting one hot internal monopolize the DAG until the
# global DAG_MAX_NODES prune kicks in.
MAX_CHILDREN_PER_PARENT = 8


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

    # MEDIUM-N3 fix (2026-05-18) — per-parent child cap. Prevents one hot
    # internal from monopolizing the DAG until global DAG_MAX_NODES prune
    # kicks in. Raised BEFORE the global cap so callers see the more
    # specific failure first; `add_children_for_phase` soft-fails per alpha.
    parent_children = dag["nodes"][parent_id].get("children") or []
    if len(parent_children) >= MAX_CHILDREN_PER_PARENT:
        raise ValueError(
            f"parent at child cap: parent_id={parent_id!r} already has "
            f"{len(parent_children)} children (max={MAX_CHILDREN_PER_PARENT})"
        )

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
    """Online running-average reward update + n_pulls increment, with
    MCTS-style backprop to ancestors (MEDIUM-N3 fix, 2026-05-18).

    Reward is clipped to [0.01, 0.99] to avoid Beta(0, *) or Beta(*, 0)
    updates per plan §4.5. Mean-weighted on the leaf:
        new_reward = (old_reward * n_pulls + r) / (n_pulls + 1)

    Each ancestor (parent, grandparent, …) gets ``n_pulls += 1`` and a
    decayed reward credit ``r * REWARD_PROPAGATION_DECAY ** distance``
    (distance=1 for direct parent). This makes the
    ``N_PULLS_RE_EXPAND_THRESHOLD`` gate in :func:`select_next_parent`
    actually meaningful: internal nodes accumulate n_pulls as their
    subtree is explored, so they eventually fall out of the re-expand
    eligible pool and UCB1 picks new branches.

    Bounded by ``REWARD_PROPAGATION_MAX_DEPTH`` to defend against cycles
    (matches `path_to_root` / `_depth_of`).
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

    # MEDIUM-N3: propagate up the parent chain with geometric decay.
    cur_id: Optional[str] = node.get("parent_id")
    distance = 1
    visited: set = {node_id}
    while cur_id is not None and distance <= REWARD_PROPAGATION_MAX_DEPTH:
        if cur_id in visited:
            logger.warning(f"[dag_state] update_reward cycle at {cur_id!r}, breaking")
            break
        visited.add(cur_id)
        ancestor = dag["nodes"].get(cur_id)
        if ancestor is None:
            # Dangling parent_id (shouldn't happen post-prune invariant) — stop.
            break
        decayed = r * (REWARD_PROPAGATION_DECAY ** distance)
        an = int(ancestor.get("n_pulls", 0) or 0)
        aprev = float(ancestor.get("reward", 0.5) or 0.5)
        ancestor["reward"] = (aprev * an + decayed) / (an + 1)
        ancestor["n_pulls"] = an + 1
        cur_id = ancestor.get("parent_id")
        distance += 1


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
    """Pick the next parent to expand. Returns node_id or None when no eligible.

    Algorithm per plan §4.2 (hybrid UCB1 + Thompson cold-start):
      1. Collect eligible parents: active AND (leaf OR n_pulls<N_PULLS_RE_EXPAND_THRESHOLD)
         — leaf-only would cause every internal node to be expanded exactly once,
         making UCB1 n_pulls on internal nodes meaningless (Bug #9 review fix).
      2. Partition by warmth: cold = n_pulls < cold_threshold; warm = rest
      3. only-cold: Thompson sample each Beta(α + r*n, β + (1-r)*n) → argmax
      4. only-warm: UCB1 score = (r/n) + c * sqrt(ln(total_pulls)/n) → argmax
      5. mixed: compute both winners, pick larger (UCB tanh-clipped to [0,1])
    """
    rng = rng or random
    leaves = [
        n for n in dag["nodes"].values()
        if (n.get("status") or "active") == "active"
        and (
            not n.get("children")
            or int(n.get("n_pulls", 0) or 0) < N_PULLS_RE_EXPAND_THRESHOLD
        )
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
      2. Process bottom-up (deepest leaves first) so intermediate inactive
         nodes become eligible once their descendants are gone [M3 fix]
      3. Preserve parent chain invariant: every remaining node's parent_id
         either is None (root) or still in nodes [V1.0-S6]

    Bug M3 fix (2026-05-18): the original implementation walked candidates
    with a linear `candidates.index(nid)` lookup (O(n^2)) AND skipped any
    node that still had a child in `dag["nodes"]`. The combined effect was
    that intermediate inactive nodes were never pruned even when their
    descendants were also candidates — so `add_node` would raise at the
    hard cap instead of `prune_to_cap` quietly making room. Now sort
    bottom-up (depth DESC, created_at ASC) and iterate until under cap;
    children are always processed before their parents, so the
    "has remaining child" skip is no longer needed (the descendants are
    either already gone or protected for other reasons like active /
    family-capped / on the current_selection path).

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

    # Candidates for pruning: everything not protected. Sort bottom-up
    # (deepest first, oldest first within same depth) so leaves are always
    # considered before their parents.
    def _prune_key(nid: str) -> tuple:
        n = dag["nodes"][nid]
        depth = _depth_of(dag, nid)
        created = n.get("created_at", "") or ""
        # depth DESC → negate; created_at ASC (oldest first → LRU)
        return (-depth, created)

    candidates = sorted(
        (nid for nid in dag["nodes"] if nid not in protected),
        key=_prune_key,
    )
    if not candidates:
        return 0

    to_drop_count = dag["node_count"] - max_nodes
    dropped = 0
    for nid in candidates:
        if dropped >= to_drop_count:
            break
        # Bottom-up order guarantees descendants are processed first, so a
        # surviving child of `nid` is necessarily protected (active path /
        # root) or eligible-but-not-yet-dropped only because the cap was
        # already met. Either way, dropping `nid` would orphan it, so skip.
        # Cheap dict-membership check (no linear search).
        node = dag["nodes"][nid]
        has_remaining_child = any(
            child_id in dag["nodes"]
            for child_id in (node.get("children") or [])
        )
        if has_remaining_child:
            continue
        # Remove from parent's children list
        parent_id = node.get("parent_id")
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
    "N_PULLS_RE_EXPAND_THRESHOLD",
    "REWARD_PROPAGATION_DECAY",
    "REWARD_PROPAGATION_MAX_DEPTH",
    "MAX_CHILDREN_PER_PARENT",
    "init_dag",
    "add_node",
    "update_reward",
    "mark_status",
    "select_next_parent",
    "path_to_root",
    "prune_to_cap",
    "to_dict",
    "from_dict",
    # === PR2 cascade integration helpers ===
    "load_or_init",
    "add_children_for_phase",
    "compute_reward_for_node",
    "mark_family_capped_children",
]


# ---------------------------------------------------------------------------
# PR2: cascade/flat integration helpers (plan v1.0 §5.1 + §5.2)
# ---------------------------------------------------------------------------

def load_or_init(
    d: Optional[Dict[str, Any]],
    *,
    run_id: int,
    root_tier: int = 1,
    root_dataset_id: str = "",
) -> Dict[str, Any]:
    """Convenience: load existing DAG from runtime_state["dag"] OR init fresh.

    Used by `_run_continuous_cascade` / `_run_flat_iteration` at session
    start to get a guaranteed-valid DAG dict without branching the call site.
    """
    parsed = from_dict(d)
    if parsed and parsed.get("root_id") and parsed.get("nodes"):
        return parsed
    return init_dag(run_id=run_id, root_tier=root_tier, root_dataset_id=root_dataset_id)


def _alpha_score(alpha: Any) -> float:
    """Resolve alpha's composite/sharpe score for DAG reward. Plan §4.5.

    Tries metrics["composite_score"] (R5+R1a combined) → metrics["sharpe"]/4
    → 0.5. Clip to [0.01, 0.99] applied by update_reward caller.
    """
    metrics = getattr(alpha, "metrics", None)
    if not isinstance(metrics, dict):
        return 0.5
    comp = metrics.get("composite_score") or metrics.get("_r5_composite_score")
    if isinstance(comp, (int, float)):
        return float(comp)
    sharpe = metrics.get("sharpe")
    if isinstance(sharpe, (int, float)):
        return max(0.0, min(1.0, float(sharpe) / 4.0))
    return 0.5


def compute_reward_for_node(node_or_alpha: Any) -> float:
    """Reward signal for a DAG node from an alpha (or node already populated).

    Per plan §4.5 3-tier fallback:
        Tier 1: composite_score (R5+R1a) > Tier 2: sharpe/4 > Tier 3: 0.5

    Accepts either an Alpha/AlphaCandidate (reads .metrics) OR a DAG node
    dict (reads node["expression_signature"] / fallback 0.5).
    """
    # If it's a node dict (no .metrics attr), fall back to 0.5
    if isinstance(node_or_alpha, dict):
        return float(node_or_alpha.get("reward", 0.5) or 0.5)
    return _alpha_score(node_or_alpha)


def add_children_for_phase(
    dag: Dict[str, Any],
    *,
    parent_id: str,
    round_idx: int,
    tier: int,
    dataset_id: str,
    loop_id: int,
    alphas: List[Any],
    max_nodes: int = 100,
) -> List[str]:
    """Bulk-add one DAG child per alpha produced in a round.

    Per plan §4.7 [V1.0-A2-1] lock: alpha-as-node, not round-as-node.
    Returns list of new node ids (sorted by insertion order). Soft-fails
    on individual alpha errors (skip with warning) so one bad alpha
    doesn't drop the whole batch.
    """
    import hashlib
    child_ids: List[str] = []
    for alpha in alphas or []:
        # Defensive: skip None / non-alpha-shaped entries (e.g. error placeholders)
        if alpha is None or not hasattr(alpha, "expression"):
            logger.debug(f"[dag_state] add_children_for_phase skip non-alpha entry: {type(alpha).__name__}")
            continue
        try:
            expr = (getattr(alpha, "expression", "") or "")[:200]
            sig = hashlib.sha256(expr.encode("utf-8")).hexdigest()[:16] if expr else ""
            cid = add_node(
                dag,
                parent_id=parent_id,
                round_idx=round_idx,
                tier=tier,
                dataset_id=dataset_id,
                loop_id=loop_id,
                expression_signature=sig,
                max_nodes=max_nodes,
            )
            child_ids.append(cid)
        except Exception as e:
            logger.warning(f"[dag_state] add_children_for_phase skip alpha (non-fatal): {e}")
    return child_ids


def mark_family_capped_children(
    dag: Dict[str, Any],
    child_ids: List[str],
    alphas: List[Any],
) -> int:
    """R10 propagation: alphas marked _r10_family_cap_dropped=True →
    corresponding DAG node status='family_capped' (per plan §2.5).

    child_ids and alphas must be parallel lists (same order). Returns count
    of nodes marked family_capped. Defensive on length mismatch — uses min.
    """
    marked = 0
    n = min(len(child_ids), len(alphas or []))
    for i in range(n):
        alpha = alphas[i]
        metrics = getattr(alpha, "metrics", None) or {}
        if isinstance(metrics, dict) and metrics.get("_r10_family_cap_dropped") is True:
            mark_status(dag, child_ids[i], "family_capped")
            marked += 1
    return marked
