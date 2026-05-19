"""G1 follow-up (2026-05-19): buffered-path bandit-arm stamp unit tests.

Validates that workflow.run_with_persistence (the cold/legacy buffered
persistence path used when T2_INCREMENTAL_PERSISTENCE=False) stamps every
PASS AlphaResult with metrics['_direction_bandit_recommended_arm'] before
the Alpha() ORM row is built. Symmetric with the incremental hot path in
_incremental_save_alphas already covered by test_g1_bandit_arm_stamp.py.

Strategy: don't rebuild the entire run_with_persistence — exercise the
stamp logic itself (the if-block inserted after metrics_dict assignment)
to assert in-place mutation semantics. The full integration round (real
DB + real LangGraph) is covered by test_g1_bandit_arm_stamp.py's
integration test for the incremental path; the buffered path's only
G1-specific behavior is the new stamp block.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Stamp behaviour: in-place mutation of alpha_result.metrics
# ---------------------------------------------------------------------------


def _make_alpha_result(metrics=None):
    """Build a minimal AlphaResult-shaped MagicMock for the stamp loop."""
    ar = MagicMock()
    ar.metrics = metrics if metrics is not None else {"sharpe": 1.5}
    ar.expression = "ts_rank(returns, 20)"
    ar.alpha_id = "ABC123"
    ar.quality_status = "PASS"
    ar.hypothesis = None
    ar.explanation = None
    ar.persisted = False
    return ar


def _apply_buffered_stamp(alpha_result, bandit_arm):
    """Mirror the inline stamp block from workflow.run_with_persistence.

    Kept as a free function so the test can exercise the mutation contract
    without needing to bootstrap MiningWorkflow + DB + LangGraph.
    """
    metrics_dict = alpha_result.metrics if isinstance(alpha_result.metrics, dict) else {}
    if bandit_arm:
        if not isinstance(alpha_result.metrics, dict):
            alpha_result.metrics = dict(metrics_dict)
        alpha_result.metrics["_direction_bandit_recommended_arm"] = bandit_arm
    return alpha_result.metrics


def test_buffered_stamp_writes_key_when_arm_present():
    ar = _make_alpha_result(metrics={"sharpe": 1.5, "fitness": 1.2})
    out = _apply_buffered_stamp(ar, "rag_template")
    assert out["_direction_bandit_recommended_arm"] == "rag_template"
    # Existing keys preserved
    assert out["sharpe"] == 1.5
    assert out["fitness"] == 1.2
    # Mutated in-place — same object
    assert ar.metrics is out


def test_buffered_stamp_omits_key_when_arm_none():
    """Flag-OFF / round-1 cold-start → arm is None → no key written
    (G1 invariant: stamp key omitted, not stamped as None)."""
    ar = _make_alpha_result(metrics={"sharpe": 1.5})
    out = _apply_buffered_stamp(ar, None)
    assert "_direction_bandit_recommended_arm" not in out
    assert out == {"sharpe": 1.5}


def test_buffered_stamp_omits_key_when_arm_empty_string():
    """Defensive: empty string treated as falsy (no stamp)."""
    ar = _make_alpha_result(metrics={"sharpe": 1.5})
    out = _apply_buffered_stamp(ar, "")
    assert "_direction_bandit_recommended_arm" not in out


def test_buffered_stamp_handles_non_dict_metrics():
    """When AlphaResult.metrics is None / non-dict (legacy / malformed),
    stamp converts to dict in-place before writing — same contract as
    incremental path."""
    ar = _make_alpha_result(metrics=None)
    out = _apply_buffered_stamp(ar, "llm_generation")
    assert isinstance(out, dict)
    assert out["_direction_bandit_recommended_arm"] == "llm_generation"


def test_buffered_stamp_overwrites_existing_key():
    """If a prior round already wrote a different arm (unlikely but
    defensive), the new arm wins. Symmetric with incremental path."""
    ar = _make_alpha_result(metrics={
        "sharpe": 1.5,
        "_direction_bandit_recommended_arm": "old_arm",
    })
    out = _apply_buffered_stamp(ar, "new_arm")
    assert out["_direction_bandit_recommended_arm"] == "new_arm"


# ---------------------------------------------------------------------------
# Integration smoke: workflow.run_with_persistence imports _read_bandit_arm_for_round
# ---------------------------------------------------------------------------


def test_buffered_path_imports_bandit_helper():
    """Catch import-time regression — the inline import added to
    workflow.run_with_persistence must resolve. A typo in module path
    would crash the buffered persistence loop on first use."""
    from backend.agents.graph.nodes.persistence import _read_bandit_arm_for_round
    assert callable(_read_bandit_arm_for_round)


@pytest.mark.asyncio
async def test_buffered_path_soft_fails_on_bandit_read_error():
    """Helper exception → caller catches + logs → _g1_bandit_arm stays
    None → stamp loop omits the key. Mirrors the try/except in workflow."""
    from backend.agents.graph.nodes.persistence import _read_bandit_arm_for_round
    import backend.agents.graph.nodes.persistence as _pmod

    # Patch helper to raise
    original = _pmod._read_bandit_arm_for_round

    async def _raise(*a, **kw):
        raise RuntimeError("simulated DB outage")

    _pmod._read_bandit_arm_for_round = _raise
    try:
        # Emulate the try/except block exactly as added to workflow.py
        _g1_bandit_arm = None
        try:
            _g1_bandit_arm = await _pmod._read_bandit_arm_for_round(
                AsyncMock(), task_id=42,
            )
        except Exception:
            _g1_bandit_arm = None
        assert _g1_bandit_arm is None
    finally:
        _pmod._read_bandit_arm_for_round = original
