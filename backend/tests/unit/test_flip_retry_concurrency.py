"""opt A (2026-05-25): flip-retry sims run CONCURRENTLY, not in a serial await
loop.

The serial `for orig in flip_candidates: await brain.simulate_alpha(...)` made
news12 EVALUATE ~11min (it6: ~4 sims × ~2.7min each), tipping rounds over the
1200s round timeout. Each simulate_alpha already holds a cross-process BRAIN sim
slot (_acquire_sim_slot, role-aware USER=3 / CONSULTANT=80), so gathering is safe
— the slot counter caps real concurrency and never hits 429.

Source-level guard, mirroring v27_81's test_flip_retry_wires_dedup_lock (the
flip-retry block lives deep inside the large node_evaluate and is not unit-
isolatable; functional equivalence is covered by v27_81 + v19_3 + the regression
suite).
"""
import inspect

from backend.agents.graph.nodes import evaluation


def _node_evaluate_src() -> str:
    return inspect.getsource(evaluation.node_evaluate)


def test_flip_retry_sims_run_concurrently():
    src = _node_evaluate_src()
    assert "_sim_one_flip" in src, "flip-retry concurrent sim helper _sim_one_flip is gone"
    assert "asyncio.gather(" in src, (
        "flip-retry sim no longer gathered — opt A regressed to a serial await loop "
        "(this is what made news12 EVALUATE ~11min)"
    )


def test_flip_retry_still_claims_and_releases_slot():
    """opt A must NOT drop the V-27.81 in-flight slot dedup/release."""
    src = _node_evaluate_src()
    assert "claim_simulate_slot" in src, "flip-retry lost V-27.81 slot claim"
    assert "release_simulate_slot" in src, "flip-retry lost V-27.81 slot release (leak risk)"
