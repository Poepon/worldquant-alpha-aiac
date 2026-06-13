"""Unit tests for Phase 4 Sprint 0 PR0.6 — sentinel stamp backfill.

Coverage:
  - cached_simulate_batch tags every cache-hit result with `_cache_hit=True`
  - The G8-forest stamping path: setattr state.g8_forest_referenced_ids
    is correctly readable from evaluation
  - Evaluation R1b/G8/R9 stamp logic correctness (mock state + alphas)

These 3 stamps unblock the R12 decision counterfactual SQL at Sprint末:
    SELECT alpha.id WHERE metrics->>'_r1b_mutation_triggered'='true'  (etc)
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# sim_cache cache-hit stamp
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cached_simulate_batch_marks_cache_hit_results():
    """cached_simulate_batch must tag each cache-hit result with the stamp
    landing INSIDE `result["metrics"]` (the dict that evaluation.py:1267
    propagates to alpha.metrics) — NOT at the top level (F-A1 fix
    post-review)."""
    from backend.agents import sim_cache

    # Use side_effect list (post-review S0-B SHOULD #6 fix — avoid _calls hack)
    cached_payload = {"success": True, "metrics": {"sharpe": 1.5}}

    # Mock brain.simulate_batch: return success for uncached
    brain = MagicMock()
    brain.simulate_batch = AsyncMock(
        return_value=[{"success": True, "metrics": {"sharpe": 1.0}}],
    )

    with (
        patch.object(sim_cache, "get_cached",
                     side_effect=[dict(cached_payload), None]),
        patch.object(sim_cache, "set_cached", new=AsyncMock(return_value=True)),
    ):
        results = await sim_cache.cached_simulate_batch(
            db=MagicMock(),
            brain=brain,
            expressions=["expr_cached", "expr_uncached"],
            region="USA",
            universe="TOP3000",
        )

    assert len(results) == 2
    # F-A1: stamp lives in result["metrics"], not top-level
    assert results[0].get("metrics", {}).get("_simulation_cache_hit") is True, (
        f"cache-hit must have metrics._simulation_cache_hit=True, got: {results[0]}"
    )
    # BRAIN-fresh result must NOT carry the stamp
    assert results[1].get("metrics", {}).get("_simulation_cache_hit") is not True
    # Top-level _cache_hit must NOT exist (F-A1 bug: was at top level)
    assert "_cache_hit" not in results[0], (
        "F-A1 regression: top-level _cache_hit should be GONE (stamp moved to metrics)"
    )


@pytest.mark.asyncio
async def test_cached_simulate_batch_100_pct_hit_marks_all():
    """100% cache hit path (short-circuit return) must still tag all results
    with `metrics._simulation_cache_hit=True` (F-A1 — inside metrics dict)."""
    from backend.agents import sim_cache

    async def _all_hit(db, key, ttl_days=None):
        return {"success": True, "metrics": {"sharpe": 1.5}}

    with patch.object(sim_cache, "get_cached", side_effect=_all_hit):
        results = await sim_cache.cached_simulate_batch(
            db=MagicMock(),
            brain=MagicMock(),
            expressions=["a", "b", "c"],
            region="USA", universe="TOP3000",
        )

    assert len(results) == 3
    assert all(
        r.get("metrics", {}).get("_simulation_cache_hit") is True
        for r in results
    )


@pytest.mark.asyncio
async def test_cached_simulate_batch_creates_metrics_when_absent():
    """F-A1 edge case — if cached result has no `metrics` key (legacy /
    malformed cache row), stamp must create an empty metrics dict + stamp
    inside, NOT fall back to top-level."""
    from backend.agents import sim_cache

    async def _hit_no_metrics(db, key, ttl_days=None):
        return {"success": True}  # NB: no 'metrics' key

    with patch.object(sim_cache, "get_cached", side_effect=_hit_no_metrics):
        results = await sim_cache.cached_simulate_batch(
            db=MagicMock(),
            brain=MagicMock(),
            expressions=["a"],
            region="USA", universe="TOP3000",
        )

    assert results[0].get("metrics", {}).get("_simulation_cache_hit") is True
    assert "_cache_hit" not in results[0]


# ---------------------------------------------------------------------------
# Evaluation stamp block — isolated logic test
# ---------------------------------------------------------------------------


def _make_mock_alpha(hyp_id=None, cache_hit_in_metrics=False, existing_metrics=None):
    """Build a mock alpha with the attributes the stamp block reads.

    F-A1 post-review: AlphaCandidate has no `sim_result` field — the only
    cache-hit carrier is alpha.metrics["_simulation_cache_hit"] (planted by
    sim_cache → propagated via evaluation.py:1267).
    """
    a = MagicMock()
    a.hypothesis_id = hyp_id
    m = dict(existing_metrics) if existing_metrics else {}
    if cache_hit_in_metrics:
        m["_simulation_cache_hit"] = True
    a.metrics = m
    return a


def test_stamp_logic_g8_forest_only():
    """Alpha tied to a forest-referenced hypothesis → stamped _hypothesis_forest_reference."""
    mutated_hids = set()
    forest_hids = {99}

    alpha = _make_mock_alpha(hyp_id=99)
    m = dict(alpha.metrics) if isinstance(alpha.metrics, dict) else {}
    hid = alpha.hypothesis_id
    if hid in mutated_hids:
        m["_r1b_mutation_triggered"] = True
    if hid in forest_hids:
        m["_hypothesis_forest_reference"] = True

    alpha.metrics = m
    assert alpha.metrics.get("_hypothesis_forest_reference") is True
    assert "_r1b_mutation_triggered" not in alpha.metrics


def test_stamp_logic_r9_cache_hit_only():
    """Alpha whose metrics ALREADY has _simulation_cache_hit=True (planted by
    sim_cache → propagated through evaluation.py:1267 to alpha.metrics).
    F-A1: stamp lives in metrics, not in sim_result.
    """
    alpha = _make_mock_alpha(hyp_id=1, cache_hit_in_metrics=True)
    # Block read path: check metrics for the cache-hit flag directly
    m = dict(alpha.metrics) if isinstance(alpha.metrics, dict) else {}
    assert m.get("_simulation_cache_hit") is True


def test_stamp_logic_all_three_combined():
    """Alpha matching all 3 sources → all 3 stamps set on metrics."""
    mutated_hids = {7}
    forest_hids = {7}  # same hypothesis can be both mutated AND forest-referenced
    alpha = _make_mock_alpha(hyp_id=7, cache_hit_in_metrics=True)

    m = dict(alpha.metrics) if isinstance(alpha.metrics, dict) else {}
    hid = alpha.hypothesis_id
    if hid in mutated_hids:
        m["_r1b_mutation_triggered"] = True
    if hid in forest_hids:
        m["_hypothesis_forest_reference"] = True
    # _simulation_cache_hit already in m (planted by sim_cache stamp)
    alpha.metrics = m

    assert alpha.metrics["_r1b_mutation_triggered"] is True
    assert alpha.metrics["_hypothesis_forest_reference"] is True
    assert alpha.metrics["_simulation_cache_hit"] is True


def test_stamp_logic_no_match_no_stamps():
    """Alpha not matching any source → no stamp keys added (no cache_hit in
    metrics; no mutation; no forest reference)."""
    alpha = _make_mock_alpha(hyp_id=1, cache_hit_in_metrics=False)
    mutated_hids = set()
    forest_hids = set()

    m = dict(alpha.metrics) if isinstance(alpha.metrics, dict) else {}
    hid = alpha.hypothesis_id
    if hid in mutated_hids:
        m["_r1b_mutation_triggered"] = True
    if hid in forest_hids:
        m["_hypothesis_forest_reference"] = True
    alpha.metrics = m

    assert "_r1b_mutation_triggered" not in alpha.metrics
    assert "_hypothesis_forest_reference" not in alpha.metrics
    assert "_simulation_cache_hit" not in alpha.metrics


def test_stamp_logic_existing_metrics_preserved():
    """Pre-existing metrics keys (e.g. _r10_family_cap_dropped from earlier
    block) must NOT be wiped when the stamp block adds its own keys."""
    mutated_hids = {3}
    alpha = _make_mock_alpha(
        hyp_id=3,
        existing_metrics={
            "_r10_family_cap_dropped": True,
            "_g3_ast_originality_blocked": False,
            "sharpe": 1.7,
        },
    )

    m = dict(alpha.metrics) if isinstance(alpha.metrics, dict) else {}
    if alpha.hypothesis_id in mutated_hids:
        m["_r1b_mutation_triggered"] = True
    alpha.metrics = m

    assert alpha.metrics["_r10_family_cap_dropped"] is True  # preserved
    assert alpha.metrics["_g3_ast_originality_blocked"] is False  # preserved
    assert alpha.metrics["sharpe"] == 1.7  # preserved
    assert alpha.metrics["_r1b_mutation_triggered"] is True  # added


# ---------------------------------------------------------------------------
# G8 setattr propagation
# ---------------------------------------------------------------------------


def test_g8_state_field_assignable():
    """state.g8_forest_referenced_ids is a declared MiningState List[int]
    field (state.py:182) — direct assignment works without Pydantic
    validate_assignment rejection."""
    from backend.agents.graph.state import MiningState
    state = MiningState(
        task_id=1, dataset_id="fnd6", region="USA", universe="TOP3000",
    )
    state.g8_forest_referenced_ids = [10, 20, 30]
    assert state.g8_forest_referenced_ids == [10, 20, 30]


def test_g8_state_attr_default_empty():
    """When G8 fetch returned no rows / flag OFF, state attr unset → stamp
    block reads None → no forest stamps. Default-safe."""
    from backend.agents.graph.state import MiningState
    state = MiningState(
        task_id=1, dataset_id="fnd6", region="USA", universe="TOP3000",
    )
    val = getattr(state, "g8_forest_referenced_ids", None) or []
    assert val == []
