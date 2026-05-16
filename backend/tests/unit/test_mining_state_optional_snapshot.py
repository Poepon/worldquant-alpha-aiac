"""Unit tests: MiningState.brain_consultant_mode_at_start + effective_* fields
default to None and don't break the 30+ existing MiningState(task_id=...)
construction sites scattered across tests.

Plan §8.2 + R3-C1: fields must be Optional + default=None to maintain
backward compatibility with constructors that don't pass effective_*.
"""
from __future__ import annotations

from backend.agents.graph.state import MiningState


def test_minimal_construction_defaults_to_none():
    """Calling MiningState(task_id=1) with no role fields must succeed and
    yield None for all four snapshot fields."""
    state = MiningState(task_id=1)
    assert state.brain_consultant_mode_at_start is None
    assert state.effective_default_test_period is None
    assert state.effective_sharpe_submit_min is None
    assert state.effective_region_universes_at_start is None


def test_explicit_snapshot_round_trip():
    state = MiningState(
        task_id=42,
        brain_consultant_mode_at_start=True,
        effective_default_test_period="P0Y",
        effective_sharpe_submit_min=1.58,
        effective_region_universes_at_start={"USA": "TOP3000", "CHN": "TOP2000A"},
    )
    assert state.brain_consultant_mode_at_start is True
    assert state.effective_default_test_period == "P0Y"
    assert state.effective_sharpe_submit_min == 1.58
    assert state.effective_region_universes_at_start == {
        "USA": "TOP3000",
        "CHN": "TOP2000A",
    }


def test_partial_snapshot_other_fields_remain_none():
    """Setting only one field leaves others as None — supports gradual
    rollout where old workflow.py might pass only some fields."""
    state = MiningState(
        task_id=1,
        effective_sharpe_submit_min=1.5,
    )
    assert state.effective_sharpe_submit_min == 1.5
    assert state.brain_consultant_mode_at_start is None
    assert state.effective_default_test_period is None


def test_getattr_pattern_works_for_caller_safety():
    """Downstream callers use getattr(state, "X", default) to tolerate older
    state objects loaded from a checkpoint that predates v5."""
    state = MiningState(task_id=1)
    # simulate caller defending against missing attribute (Pydantic BaseModel
    # has no .get; getattr is the right pattern)
    sharpe = getattr(state, "effective_sharpe_submit_min", None)
    assert sharpe is None
    test_period = getattr(state, "effective_default_test_period", None) or "P2Y0M"
    assert test_period == "P2Y0M"
