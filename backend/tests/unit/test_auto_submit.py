"""Unit tests for the auto-submit fail-closed guard stack (2026-06-04).

Covers ``auto_submit_selector.evaluate_guard_stack`` (the per-candidate garbage
filter) + helpers. Thresholds are read from the live ``settings`` so the tests
stay correct if the eval band is retuned. The candidate SQL +
``compute_auto_submit_candidates`` use PG-only JSONB and are verified against
live Postgres, not here (mirrors the repo's "SQL endpoints need live PG" rule).
"""
from datetime import datetime, timedelta, timezone

import pytest

from backend.config import settings
from backend.auto_submit_selector import evaluate_guard_stack, _cs_age_hours
from backend.tasks.auto_submit_tasks import _verdict_ok


def _passing_candidate():
    """A candidate comfortably above every gate (fields set relative to the live
    thresholds so the test is band-agnostic)."""
    th = settings.eval_thresholds(1)
    fresh = datetime.now(timezone.utc) - timedelta(hours=1)
    turn_lo, turn_hi = th["turnover_min"], th["turnover_max"]
    return {
        "id": 1,
        "_brain_id": "xYz123",
        "_delay": 1,
        "_sharpe": th["sharpe_min"] + 0.5,
        "_fitness": th["fitness_min"] + 0.3,
        "_turnover": (turn_lo + turn_hi) / 2.0,
        "_margin": (settings.AUTO_SUBMIT_MARGIN_BPS_MIN / 10000.0) + 0.0003,
        "_composite": 0.4,
        "_recommendation": "SUBMIT",
        "value_tier": 0,
        "self_corr": 0.3,
        "max_corr_to_selected": 0.45,
        "_in_ordered": True,
        "_cs_snapshot": fresh,
        "_pnl_covered": True,
        "rank": 1,
    }


def test_passing_candidate_passes_all_gates():
    ev = evaluate_guard_stack(_passing_candidate(), sign_routing_ok=True, settings=settings)
    assert ev["passed"] is True, ev
    assert all(ev["gates"].values()), ev["gates"]
    assert ev["skip_reason"] is None


@pytest.mark.parametrize("field,value,expect_gate", [
    ("_sharpe", 0.1, "G5_sharpe"),
    ("_sharpe", None, "G5_sharpe"),
    ("_fitness", 0.1, "G5_fitness"),
    ("_fitness", None, "G5_fitness"),
    ("_turnover", 0.999, "G5_turnover"),       # above band
    ("_turnover", 0.0, "G5_turnover"),         # below band
    ("_turnover", None, "G5_turnover"),
    ("_margin", 0.0001, "G6_margin"),          # < 5bps
    ("_margin", -0.01, "G6_margin"),           # negative
    ("_margin", None, "G6_margin"),
    ("_recommendation", "NEUTRAL", "G7_recommendation"),
    ("_recommendation", "SKIP", "G7_recommendation"),
    ("_recommendation", None, "G7_recommendation"),
    ("_composite", 0.0, "G7_recommendation"),  # composite must be > 0
    ("_composite", -0.5, "G7_recommendation"),
    ("value_tier", 1, "G8_value_tier"),        # neutral, not additive
    ("value_tier", 2, "G8_value_tier"),        # dilutive
    ("value_tier", None, "G8_value_tier"),
    ("_in_ordered", False, "G9_orthogonal"),   # correlation-blocked
    ("max_corr_to_selected", 0.99, "G9_orthogonal"),
    ("max_corr_to_selected", None, "G9_orthogonal"),
    ("self_corr", None, "G3b_self_corr"),       # un-measured self_corr → not submittable
    ("self_corr", 0.99, "G3b_self_corr"),       # self_corr above threshold
])
def test_each_gate_fails_closed(field, value, expect_gate):
    cand = _passing_candidate()
    cand[field] = value
    ev = evaluate_guard_stack(cand, sign_routing_ok=True, settings=settings)
    assert ev["passed"] is False
    assert ev["gates"][expect_gate] is False, (field, value, ev["gates"])


def test_sign_routing_off_fails_value_tier():
    # If the recon verdict didn't validate the sign, G8 can't pass even with
    # value_tier=0 — routing on an unvalidated sign is the audited mistake.
    ev = evaluate_guard_stack(_passing_candidate(), sign_routing_ok=False, settings=settings)
    assert ev["passed"] is False
    assert ev["gates"]["G8_value_tier"] is False


def test_freshness_stale_fails_when_required():
    cand = _passing_candidate()
    cand["_cs_snapshot"] = datetime.now(timezone.utc) - timedelta(
        hours=settings.AUTO_SUBMIT_CANSUBMIT_MAX_AGE_H + 5
    )
    ev = evaluate_guard_stack(cand, sign_routing_ok=True, settings=settings)
    assert ev["passed"] is False
    assert ev["gates"]["G4_freshness"] is False


def test_freshness_unknown_fails_closed_when_required():
    cand = _passing_candidate()
    cand["_cs_snapshot"] = None  # no refresh timestamp → unknown → fail-closed
    ev = evaluate_guard_stack(cand, sign_routing_ok=True, settings=settings)
    assert ev["passed"] is False
    assert ev["gates"]["G4_freshness"] is False


def test_signals_recorded_for_audit():
    ev = evaluate_guard_stack(_passing_candidate(), sign_routing_ok=True, settings=settings)
    sig = ev["signals"]
    for key in ("sharpe", "fitness", "turnover", "margin", "margin_bps",
                "composite", "recommendation", "value_tier", "self_corr",
                "max_corr_to_selected", "can_submit_age_h", "sign_routing_ok"):
        assert key in sig


def test_cs_age_hours():
    assert _cs_age_hours(None) is None
    now = datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc)
    five_h_ago = now - timedelta(hours=5)
    assert abs(_cs_age_hours(five_h_ago, now_utc=now) - 5.0) < 1e-6
    # naive snapshot treated as UTC (defensive)
    naive = datetime(2026, 6, 4, 7, 0, 0)
    assert abs(_cs_age_hours(naive, now_utc=now) - 5.0) < 1e-6


@pytest.mark.parametrize("verdict,require,expected", [
    ("supported", "supported", True),
    ("weak", "supported", False),
    ("FALSIFIED", "supported", False),
    ("insufficient_sample", "supported", False),
    ("supported", "weak", True),
    ("weak", "weak", True),
    ("FALSIFIED", "weak", False),
    (None, "supported", False),
])
def test_verdict_gate(verdict, require, expected):
    assert _verdict_ok(verdict, require) is expected
