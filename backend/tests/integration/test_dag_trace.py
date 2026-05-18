"""Phase 2 R6 PR3 integration tests for cascade + flat DAG integration.

Tests per plan v1.0 §8 (integration test file). Targets the
`_dag_update_after_round` helper + dispatch-time DAG state integration.

Coverage:
  1. _dag_update_after_round soft-fails on missing run / dag_state None
  2. _dag_update_after_round adds children + updates reward + commits
  3. _dag_update_after_round propagates R10 family_capped status
  4. _dag_update_after_round prunes when over cap (uses settings)
  5. _dag_update_after_round handles 0-alpha round (early return)
  6. _run_cascade_phase signature accepts dag_state kwarg
  7. Flag OFF byte-equivalent — _dag_update_after_round with dag_state=None no-op
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.config import _flag_override_cache
from backend.agents.graph.dag_state import init_dag


@pytest.fixture(autouse=True)
def _clear_flag_cache():
    _flag_override_cache.clear()
    yield
    _flag_override_cache.clear()


def _make_alpha(expr: str, composite: float = None, sharpe: float = None,
                family_capped: bool = False):
    metrics = {}
    if composite is not None:
        metrics["composite_score"] = composite
    if sharpe is not None:
        metrics["sharpe"] = sharpe
    if family_capped:
        metrics["_r10_family_cap_dropped"] = True
    return SimpleNamespace(expression=expr, metrics=metrics)


# ---------------------------------------------------------------------------
# Test 1+7: Soft-fail / dag_state None
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dag_update_after_round_none_dag_no_op():
    """dag_state=None → early return, no DB commit."""
    from backend.tasks.mining_tasks import _dag_update_after_round
    db = MagicMock()
    db.commit = AsyncMock()
    run = SimpleNamespace(id=1, runtime_state={})
    # Should not raise, should not commit
    await _dag_update_after_round(
        db, run, None,
        round_result={"all_alphas": [_make_alpha("x")]},
        tier=1, dataset_id="pv1", round_idx=0,
    )
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_dag_update_after_round_none_run_no_op():
    """run=None → early return."""
    from backend.tasks.mining_tasks import _dag_update_after_round
    db = MagicMock()
    db.commit = AsyncMock()
    dag = init_dag(run_id=99)
    await _dag_update_after_round(
        db, None, dag,
        round_result={"all_alphas": []},
        tier=1, dataset_id="pv1", round_idx=0,
    )
    db.commit.assert_not_called()


# ---------------------------------------------------------------------------
# Test 5: 0-alpha round → early return
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dag_update_after_round_empty_alphas_no_op():
    """No alphas in result → early return without commit."""
    from backend.tasks.mining_tasks import _dag_update_after_round
    db = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    run = SimpleNamespace(id=99, runtime_state={})
    dag = init_dag(run_id=99)
    await _dag_update_after_round(
        db, run, dag,
        round_result={"all_alphas": []},
        tier=1, dataset_id="pv1", round_idx=0,
    )
    db.commit.assert_not_called()
    assert dag["node_count"] == 1  # only root, no add


# ---------------------------------------------------------------------------
# Test 2: Adds children + updates reward + commits
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dag_update_after_round_adds_children_and_rewards():
    """Round with 3 alphas → 3 new DAG children + reward populated."""
    from backend.tasks.mining_tasks import _dag_update_after_round
    db = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    run = SimpleNamespace(id=99, runtime_state={})
    dag = init_dag(run_id=99)

    alphas = [
        _make_alpha("rank(close)", composite=0.7),
        _make_alpha("ts_mean(volume, 20)", composite=0.4),
        _make_alpha("zscore(returns)", sharpe=2.0),
    ]

    with patch("backend.tasks.mining_tasks.flag_modified"):
        await _dag_update_after_round(
            db, run, dag,
            round_result={"all_alphas": alphas},
            tier=2, dataset_id="pv1", round_idx=5,
        )

    db.commit.assert_awaited_once()
    # Root + 3 children
    assert dag["node_count"] == 4
    children_of_root = dag["nodes"][dag["root_id"]]["children"]
    assert len(children_of_root) == 3
    # Rewards populated (composite_score = direct reward, sharpe = clipped /4)
    rewards = [dag["nodes"][cid]["reward"] for cid in children_of_root]
    # First two had composite 0.7 and 0.4; third had sharpe=2.0 → 0.5
    assert rewards[0] == pytest.approx(0.7)
    assert rewards[1] == pytest.approx(0.4)
    assert rewards[2] == pytest.approx(0.5)
    # All updated nodes have n_pulls == 1
    for cid in children_of_root:
        assert dag["nodes"][cid]["n_pulls"] == 1


# ---------------------------------------------------------------------------
# Test 3: R10 family-cap propagation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dag_update_after_round_propagates_family_cap():
    from backend.tasks.mining_tasks import _dag_update_after_round
    db = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    run = SimpleNamespace(id=99, runtime_state={})
    dag = init_dag(run_id=99)

    alphas = [
        _make_alpha("ok1", composite=0.6),
        _make_alpha("capped1", composite=0.6, family_capped=True),
        _make_alpha("ok2", composite=0.6),
    ]

    with patch("backend.tasks.mining_tasks.flag_modified"):
        await _dag_update_after_round(
            db, run, dag,
            round_result={"all_alphas": alphas},
            tier=1, dataset_id="x", round_idx=0,
        )

    children = dag["nodes"][dag["root_id"]]["children"]
    statuses = [dag["nodes"][cid]["status"] for cid in children]
    assert statuses == ["active", "family_capped", "active"]


# ---------------------------------------------------------------------------
# Test 4: prune triggers when over cap
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dag_update_after_round_prunes_when_over_cap():
    """Many alphas pushing over DAG_MAX_NODES → prune kicks in."""
    from backend.tasks.mining_tasks import _dag_update_after_round
    from backend.agents.graph.dag_state import add_node, mark_status
    db = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    run = SimpleNamespace(id=99, runtime_state={})
    dag = init_dag(run_id=99)

    # Pre-fill with 8 inactive nodes (room for prune target)
    parent = dag["root_id"]
    for i in range(8):
        n = add_node(dag, parent_id=parent, round_idx=i, tier=1, max_nodes=100)
        mark_status(dag, n, "inactive")
    assert dag["node_count"] == 9

    alphas = [_make_alpha(f"alpha_{i}", composite=0.5) for i in range(5)]

    # DAG_MAX_NODES isn't an ENABLE flag — patch the settings attr directly
    with patch("backend.tasks.mining_tasks.flag_modified"), \
         patch("backend.config.settings.DAG_MAX_NODES", 10):
        await _dag_update_after_round(
            db, run, dag,
            round_result={"all_alphas": alphas},
            tier=1, dataset_id="x", round_idx=10,
        )
    # node_count should respect cap (10)
    assert dag["node_count"] <= 10
    # Root must still be present
    assert dag["root_id"] in dag["nodes"]


# ---------------------------------------------------------------------------
# Test 6: signature acceptance for downstream callers
# ---------------------------------------------------------------------------

def test_run_cascade_phase_accepts_dag_state_kwarg():
    from backend.tasks.mining_tasks import _run_cascade_phase
    sig = inspect.signature(_run_cascade_phase)
    assert "dag_state" in sig.parameters
    # Default None — flag OFF callers unaffected
    assert sig.parameters["dag_state"].default is None


def test_run_continuous_cascade_signature_unchanged():
    """Outer cascade signature unchanged — DAG state init is internal."""
    from backend.tasks.mining_tasks import _run_continuous_cascade
    sig = inspect.signature(_run_continuous_cascade)
    expected = ["db", "task", "run", "celery_task_id", "lock_key", "lock_token"]
    actual = list(sig.parameters.keys())
    assert actual == expected, f"signature drift: {actual}"


def test_run_flat_iteration_signature_unchanged():
    """flat-F1 signature preserved — DAG init is internal."""
    from backend.tasks.mining_tasks import _run_flat_iteration
    sig = inspect.signature(_run_flat_iteration)
    expected = ["db", "task", "run", "celery_task_id", "lock_key", "lock_token"]
    actual = list(sig.parameters.keys())
    assert actual == expected, f"signature drift: {actual}"
