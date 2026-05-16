"""Tests for P3-Brain snapshot pathway in tier_thresholds (review M4).

Two behaviors that must hold:

1. **Running-task isolation** — when a caller passes ``sharpe_submit_min_override``
   (read from MiningTask.config["brain_role_snapshot"]), it wins over the
   current global ``settings.effective_sharpe_submit_min``. This is what
   keeps a User-mode task started before a Consultant flip from being
   re-judged with the 1.58 bar mid-round.

2. **Legacy alpha fallback** — when ``sharpe_submit_min_override=None``
   (no snapshot — alpha created before v5 or task_id=NULL),
   tier_thresholds walks ``settings.effective_sharpe_submit_min`` (current
   global). This is the documented one-time-side-effect path: legacy
   alphas may briefly demote on the Consultant switch — see
   ``backend/tasks/_role_helpers.py`` docstring.

These complement test_role_snapshot_helper.py (which only tests
``read_role_snapshot`` itself) by exercising the **next** hop in the
pipeline — the actual gating math.
"""
from __future__ import annotations

import pytest

from backend.config import _flag_override_cache


@pytest.fixture(autouse=True)
def _clear_cache():
    _flag_override_cache.clear()
    yield
    _flag_override_cache.clear()


def test_task_snapshot_override_wins_over_global_consultant_flip():
    """Running task in USER mode (override=1.5) must NOT see Consultant 1.58
    after global flag flip."""
    # Simulate operator flipping global flag mid-run
    _flag_override_cache["ENABLE_BRAIN_CONSULTANT_MODE"] = True

    from backend.agents.graph.tier_thresholds import get_tier_thresholds

    cfg = get_tier_thresholds(
        tier=None,
        sharpe_submit_min_override=1.5,  # task started in USER mode
    )
    assert cfg["sharpe_min"] == 1.5, (
        "running task must keep its startup snapshot, not pick up Consultant 1.58"
    )


def test_legacy_alpha_no_snapshot_falls_back_to_current_settings():
    """task_id=NULL alpha → read_role_snapshot returns {} → override=None →
    tier_thresholds uses settings.effective_sharpe_submit_min (current global).
    """
    _flag_override_cache["ENABLE_BRAIN_CONSULTANT_MODE"] = True

    from backend.agents.graph.tier_thresholds import get_tier_thresholds

    cfg = get_tier_thresholds(tier=None, sharpe_submit_min_override=None)
    # Consultant mode → max(SHARPE_MIN=1.5, CONSULTANT_SHARPE_SUBMIT_MIN=1.58)
    assert cfg["sharpe_min"] == 1.58


def test_no_override_user_mode_uses_sharpe_min():
    """USER mode + no snapshot → falls back to settings.SHARPE_MIN."""
    # Default: ENABLE_BRAIN_CONSULTANT_MODE=False
    from backend.agents.graph.tier_thresholds import get_tier_thresholds

    cfg = get_tier_thresholds(tier=None, sharpe_submit_min_override=None)
    assert cfg["sharpe_min"] == 1.5  # SHARPE_MIN default


def test_t1_t2_t3_internal_sharpe_unaffected_by_override():
    """T1/T2/T3 internal PROVISIONAL labels must NOT pick up the submission
    override — they're tier-internal classification, not the submit gate."""
    from backend.agents.graph.tier_thresholds import get_tier_thresholds

    for tier in (1, 2, 3):
        without = get_tier_thresholds(tier, sharpe_submit_min_override=None)
        with_override = get_tier_thresholds(tier, sharpe_submit_min_override=999.0)
        # Internal thresholds identical (override only affects fallback path)
        assert without == with_override, (
            f"tier {tier} internals must not depend on sharpe_submit_min_override"
        )
