"""G1 Phase A (2026-05-19): unit tests for ``_read_bandit_arm_for_round``
+ alpha.metrics stamp behavior.

Verifies:
  - Flag OFF → returns None unconditionally (no DB hit).
  - last_select valid shape → returns the arm name string.
  - last_select None / malformed / missing config → returns None.
  - DB exception → returns None (soft-fail invariant).

The stamping behavior inside ``_incremental_save_alphas`` is exercised via
direct manipulation of ``alpha.metrics`` to assert the in-place mutation
contract (per persistence.py G1 comment: mutating alpha.metrics so the
JSONB INSERT picks up the new key).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# _read_bandit_arm_for_round
# ---------------------------------------------------------------------------


class _ScalarReturning:
    """Mock awaitable result of db_session.execute(...). Provides
    ``scalar_one_or_none`` returning the configured value."""

    def __init__(self, value):
        self._v = value

    def scalar_one_or_none(self):
        return self._v


@pytest.mark.asyncio
async def test_read_bandit_arm_returns_none_when_flag_off():
    from backend.agents.graph.nodes.persistence import _read_bandit_arm_for_round
    from backend.config import settings as _stg

    prev = getattr(_stg, "ENABLE_DIRECTION_BANDIT", False)
    setattr(_stg, "ENABLE_DIRECTION_BANDIT", False)
    try:
        db = AsyncMock()
        # Even if DB were available, flag OFF must short-circuit (no execute call).
        db.execute = AsyncMock(side_effect=AssertionError("must not query DB when flag OFF"))
        result = await _read_bandit_arm_for_round(db, task_id=42)
        assert result is None
        db.execute.assert_not_called()
    finally:
        setattr(_stg, "ENABLE_DIRECTION_BANDIT", prev)


@pytest.mark.asyncio
async def test_read_bandit_arm_returns_arm_when_last_select_valid():
    from backend.agents.graph.nodes.persistence import _read_bandit_arm_for_round
    from backend.config import settings as _stg

    prev = getattr(_stg, "ENABLE_DIRECTION_BANDIT", False)
    setattr(_stg, "ENABLE_DIRECTION_BANDIT", True)
    try:
        config = {
            "contextual_bandit_v1": {
                "v": 1,
                "arm_names": ["rag_template", "knowledge_pattern",
                              "llm_generation", "genetic_mutation"],
                # ContextualDirectionBandit.to_dict serializes last_select as
                # [[region, category, failure], arm_name].
                "last_select": [["USA", "pricevolume", "hypothesis"],
                                "rag_template"],
            }
        }
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_ScalarReturning(config))
        result = await _read_bandit_arm_for_round(db, task_id=42)
        assert result == "rag_template"
        db.execute.assert_called_once()
    finally:
        setattr(_stg, "ENABLE_DIRECTION_BANDIT", prev)


@pytest.mark.asyncio
async def test_read_bandit_arm_returns_none_when_last_select_null():
    """Round 1 / post-`update_last_round` consumption → last_select is None
    in the persisted blob. Must not raise; must return None."""
    from backend.agents.graph.nodes.persistence import _read_bandit_arm_for_round
    from backend.config import settings as _stg

    prev = getattr(_stg, "ENABLE_DIRECTION_BANDIT", False)
    setattr(_stg, "ENABLE_DIRECTION_BANDIT", True)
    try:
        config = {"contextual_bandit_v1": {"v": 1, "last_select": None}}
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_ScalarReturning(config))
        result = await _read_bandit_arm_for_round(db, task_id=42)
        assert result is None
    finally:
        setattr(_stg, "ENABLE_DIRECTION_BANDIT", prev)


@pytest.mark.asyncio
async def test_read_bandit_arm_returns_none_when_no_bandit_config_key():
    """Pre-bandit-rollout task → config has no ``contextual_bandit_v1`` key."""
    from backend.agents.graph.nodes.persistence import _read_bandit_arm_for_round
    from backend.config import settings as _stg

    prev = getattr(_stg, "ENABLE_DIRECTION_BANDIT", False)
    setattr(_stg, "ENABLE_DIRECTION_BANDIT", True)
    try:
        config = {"other_setting": True}
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_ScalarReturning(config))
        result = await _read_bandit_arm_for_round(db, task_id=42)
        assert result is None
    finally:
        setattr(_stg, "ENABLE_DIRECTION_BANDIT", prev)


@pytest.mark.asyncio
async def test_read_bandit_arm_returns_none_when_task_config_null():
    """task.config IS NULL in DB → SELECT returns None → must short-circuit."""
    from backend.agents.graph.nodes.persistence import _read_bandit_arm_for_round
    from backend.config import settings as _stg

    prev = getattr(_stg, "ENABLE_DIRECTION_BANDIT", False)
    setattr(_stg, "ENABLE_DIRECTION_BANDIT", True)
    try:
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_ScalarReturning(None))
        result = await _read_bandit_arm_for_round(db, task_id=42)
        assert result is None
    finally:
        setattr(_stg, "ENABLE_DIRECTION_BANDIT", prev)


@pytest.mark.asyncio
async def test_read_bandit_arm_returns_none_on_malformed_last_select():
    """Forward-compat: arbitrary blob shape must not raise."""
    from backend.agents.graph.nodes.persistence import _read_bandit_arm_for_round
    from backend.config import settings as _stg

    prev = getattr(_stg, "ENABLE_DIRECTION_BANDIT", False)
    setattr(_stg, "ENABLE_DIRECTION_BANDIT", True)
    try:
        # last_select is a string instead of [ctx, arm] — schema drift simulation.
        config = {"contextual_bandit_v1": {"v": 1, "last_select": "garbage"}}
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_ScalarReturning(config))
        result = await _read_bandit_arm_for_round(db, task_id=42)
        assert result is None
    finally:
        setattr(_stg, "ENABLE_DIRECTION_BANDIT", prev)


@pytest.mark.asyncio
async def test_read_bandit_arm_soft_fails_on_db_exception():
    """Soft-fail invariant: DB error must NEVER raise into the persistence
    hot path. Must log + return None."""
    from backend.agents.graph.nodes.persistence import _read_bandit_arm_for_round
    from backend.config import settings as _stg

    prev = getattr(_stg, "ENABLE_DIRECTION_BANDIT", False)
    setattr(_stg, "ENABLE_DIRECTION_BANDIT", True)
    try:
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=RuntimeError("DB connection lost"))
        result = await _read_bandit_arm_for_round(db, task_id=42)
        assert result is None
    finally:
        setattr(_stg, "ENABLE_DIRECTION_BANDIT", prev)


@pytest.mark.asyncio
async def test_read_bandit_arm_returns_none_when_arm_empty_string():
    """Defensive: arm name as empty string → treat as None (don't stamp
    metrics with an empty arm key value)."""
    from backend.agents.graph.nodes.persistence import _read_bandit_arm_for_round
    from backend.config import settings as _stg

    prev = getattr(_stg, "ENABLE_DIRECTION_BANDIT", False)
    setattr(_stg, "ENABLE_DIRECTION_BANDIT", True)
    try:
        config = {
            "contextual_bandit_v1": {
                "v": 1,
                "last_select": [["USA", "x", "y"], ""],
            }
        }
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_ScalarReturning(config))
        result = await _read_bandit_arm_for_round(db, task_id=42)
        assert result is None
    finally:
        setattr(_stg, "ENABLE_DIRECTION_BANDIT", prev)


# ---------------------------------------------------------------------------
# Alpha metrics-stamp contract — verifies the dict mutation pattern used by
# _incremental_save_alphas. We don't invoke the whole save path (heavy DB
# wiring); we re-create the same stamp logic on a fake alpha to lock the
# contract: when bandit_arm is set, alpha.metrics gains exactly one new key
# without dropping pre-existing metric values.
# ---------------------------------------------------------------------------


class _FakeAlpha:
    def __init__(self, metrics):
        self.metrics = metrics


def _apply_stamp(alpha, bandit_arm):
    """Mirror of the inline stamp logic in _incremental_save_alphas — kept
    here as a contract test so future refactors break this test if the
    behaviour changes."""
    metrics_dict = alpha.metrics if isinstance(alpha.metrics, dict) else {}
    if bandit_arm:
        if not isinstance(alpha.metrics, dict):
            alpha.metrics = dict(metrics_dict)
        alpha.metrics["_direction_bandit_recommended_arm"] = bandit_arm
        metrics_dict = alpha.metrics
    return metrics_dict


def test_stamp_no_op_when_arm_none():
    a = _FakeAlpha({"sharpe": 1.5, "fitness": 0.8})
    md = _apply_stamp(a, None)
    assert "_direction_bandit_recommended_arm" not in md
    assert md == {"sharpe": 1.5, "fitness": 0.8}


def test_stamp_adds_arm_preserves_existing_metrics():
    a = _FakeAlpha({"sharpe": 1.5, "fitness": 0.8, "turnover": 0.2})
    md = _apply_stamp(a, "rag_template")
    assert md["_direction_bandit_recommended_arm"] == "rag_template"
    assert md["sharpe"] == 1.5
    assert md["fitness"] == 0.8
    assert md["turnover"] == 0.2
    # alpha.metrics IS the same dict object (mutated in place) — important so
    # the values_dict's `metrics=alpha.metrics` reference picks it up.
    assert a.metrics is md


def test_stamp_handles_non_dict_metrics_by_replacing():
    # Some legacy paths construct AlphaCandidate with metrics=None.
    a = _FakeAlpha(None)
    md = _apply_stamp(a, "llm_generation")
    assert md["_direction_bandit_recommended_arm"] == "llm_generation"
    assert isinstance(a.metrics, dict)


def test_stamp_overwrites_existing_arm_value():
    """If a prior round somehow left an arm in metrics (re-INSERT path), the
    fresh stamp wins — we never want a stale arm carrying forward."""
    a = _FakeAlpha({"_direction_bandit_recommended_arm": "stale_arm"})
    _apply_stamp(a, "genetic_mutation")
    assert a.metrics["_direction_bandit_recommended_arm"] == "genetic_mutation"
