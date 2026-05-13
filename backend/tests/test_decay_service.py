"""Unit tests for the decay-snapshot helper.

The helper is pure (no DB, no BRAIN) — tests pass simple stand-in objects
with the attributes the real Alpha model exposes. This keeps the dedup
logic + days_since_submit math tightly covered without bringing in the
async session machinery.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.services.decay_service import (
    MIN_SNAPSHOT_GAP_DAYS,
    build_decay_snapshot,
    maybe_append_decay_snapshot,
    should_append_snapshot,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _alpha(**overrides):
    """SimpleNamespace mirroring the columns the helper reads from."""
    defaults = dict(
        id=1,
        alpha_id="abc123",
        is_sharpe=1.5,
        is_fitness=1.1,
        is_turnover=0.45,
        is_returns=0.021,
        is_drawdown=0.08,
        is_margin=0.00015,
        metrics={},
        date_submitted=datetime(2026, 1, 1, tzinfo=timezone.utc),
        created_at=datetime(2025, 12, 1, tzinfo=timezone.utc),
        decay_curve=[],
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# build_decay_snapshot
# ---------------------------------------------------------------------------

def test_build_snapshot_uses_date_submitted_for_days_alive():
    alpha = _alpha(date_submitted=datetime(2026, 1, 1, tzinfo=timezone.utc))
    now = datetime(2026, 5, 14, 6, 30, tzinfo=timezone.utc)
    snap = build_decay_snapshot(alpha, now)
    assert snap["snapshot_date"] == "2026-05-14"
    assert snap["days_since_submit"] == 133  # Jan 1 → May 14


def test_build_snapshot_falls_back_to_created_at():
    alpha = _alpha(
        date_submitted=None,
        created_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )
    now = datetime(2026, 5, 14, tzinfo=timezone.utc)
    snap = build_decay_snapshot(alpha, now)
    assert snap["days_since_submit"] == 43


def test_build_snapshot_days_since_submit_none_when_no_anchor():
    alpha = _alpha(date_submitted=None, created_at=None)
    snap = build_decay_snapshot(alpha, datetime(2026, 5, 14, tzinfo=timezone.utc))
    assert snap["days_since_submit"] is None


def test_build_snapshot_returns_none_when_sharpe_missing():
    """Don't pollute the curve with empty rows for alphas BRAIN hasn't filled in."""
    alpha = _alpha(is_sharpe=None)
    assert build_decay_snapshot(alpha, datetime(2026, 5, 14, tzinfo=timezone.utc)) is None


def test_build_snapshot_captures_all_metrics():
    alpha = _alpha()
    snap = build_decay_snapshot(alpha, datetime(2026, 5, 14, tzinfo=timezone.utc))
    assert snap["sharpe"] == 1.5
    assert snap["fitness"] == 1.1
    assert snap["turnover"] == 0.45
    assert snap["returns"] == 0.021
    assert snap["drawdown"] == 0.08
    assert snap["margin"] == 0.00015


def test_build_snapshot_falls_back_to_metrics_blob():
    """Legacy alphas may only have unflattened metrics."""
    alpha = _alpha(
        is_sharpe=1.5,  # presence is_sharpe needed to escape the early return
        is_fitness=None,
        is_turnover=None,
        is_returns=None,
        is_drawdown=None,
        is_margin=None,
        metrics={"fitness": 0.9, "turnover": "0.3", "drawdown": 0.05},
    )
    snap = build_decay_snapshot(alpha, datetime(2026, 5, 14, tzinfo=timezone.utc))
    assert snap["fitness"] == 0.9
    assert snap["turnover"] == 0.3  # coerced from string
    assert snap["drawdown"] == 0.05
    assert snap["returns"] is None  # neither flat nor in blob


# ---------------------------------------------------------------------------
# should_append_snapshot
# ---------------------------------------------------------------------------

def test_should_append_when_curve_empty():
    assert should_append_snapshot([], datetime(2026, 5, 14)) is True


def test_should_skip_when_last_snapshot_recent():
    """< MIN_SNAPSHOT_GAP_DAYS means the daily beat already snapped this week."""
    curve = [{"snapshot_date": "2026-05-12"}]
    now = datetime(2026, 5, 14)  # only 2 days later
    assert should_append_snapshot(curve, now) is False


def test_should_append_when_gap_exceeds_floor():
    curve = [{"snapshot_date": "2026-05-07"}]
    now = datetime(2026, 5, 14)  # 7 days
    assert should_append_snapshot(curve, now) is True


def test_should_append_at_exact_boundary():
    curve = [{"snapshot_date": "2026-05-08"}]  # exactly MIN_SNAPSHOT_GAP_DAYS later
    now = datetime(2026, 5, 14)
    assert (now.date() - __import__("datetime").date(2026, 5, 8)).days == MIN_SNAPSHOT_GAP_DAYS
    assert should_append_snapshot(curve, now) is True


def test_should_append_when_last_entry_malformed():
    """If the prior entry is garbage, append rather than silently skip forever."""
    assert should_append_snapshot([{"bogus": "row"}], datetime(2026, 5, 14)) is True
    assert should_append_snapshot(["not-a-dict"], datetime(2026, 5, 14)) is True


# ---------------------------------------------------------------------------
# maybe_append_decay_snapshot (integration of build + dedup + mutation)
# ---------------------------------------------------------------------------

def test_maybe_append_appends_first_entry():
    alpha = _alpha(decay_curve=[])
    now = datetime(2026, 5, 14, tzinfo=timezone.utc)
    assert maybe_append_decay_snapshot(alpha, now) is True
    assert len(alpha.decay_curve) == 1
    assert alpha.decay_curve[0]["snapshot_date"] == "2026-05-14"


def test_maybe_append_skips_when_recent():
    alpha = _alpha(decay_curve=[{"snapshot_date": "2026-05-13", "sharpe": 1.5}])
    now = datetime(2026, 5, 14, tzinfo=timezone.utc)
    assert maybe_append_decay_snapshot(alpha, now) is False
    assert len(alpha.decay_curve) == 1


def test_maybe_append_appends_after_week():
    alpha = _alpha(decay_curve=[{"snapshot_date": "2026-05-07", "sharpe": 1.3}])
    now = datetime(2026, 5, 14, tzinfo=timezone.utc)
    assert maybe_append_decay_snapshot(alpha, now) is True
    assert len(alpha.decay_curve) == 2
    assert alpha.decay_curve[-1]["snapshot_date"] == "2026-05-14"
    assert alpha.decay_curve[-1]["sharpe"] == 1.5


def test_maybe_append_no_op_when_no_metrics():
    alpha = _alpha(is_sharpe=None, decay_curve=[])
    now = datetime(2026, 5, 14, tzinfo=timezone.utc)
    assert maybe_append_decay_snapshot(alpha, now) is False
    assert alpha.decay_curve == []


def test_maybe_append_reassigns_list_for_sqlalchemy_change_detection():
    """SQLAlchemy doesn't detect in-place JSONB list mutations — the helper
    must reassign so the column is flagged dirty."""
    original = []
    alpha = _alpha(decay_curve=original)
    now = datetime(2026, 5, 14, tzinfo=timezone.utc)
    maybe_append_decay_snapshot(alpha, now)
    assert alpha.decay_curve is not original


def test_maybe_append_handles_iso_string_dates_in_prior_curve():
    """Survive a row deserialized from JSONB where snapshot_date is a string."""
    alpha = _alpha(decay_curve=[{"snapshot_date": "2026-05-01"}])
    now = datetime(2026, 5, 14, tzinfo=timezone.utc)
    assert maybe_append_decay_snapshot(alpha, now) is True
