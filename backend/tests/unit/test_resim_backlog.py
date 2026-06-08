"""Unit tests for backend/resim_backlog.py — the pure current-data re-sim verdict.

Covers the v2 (post-review) classification: relative-to-baseline bands, margin
economic gate, dedup/cache → unmeasurable, can_submit → hold_gated, error.
"""
import pytest

from backend.resim_backlog import (
    build_resim_verdict,
    is_stale_resim,
    VERDICT_STABLE,
    VERDICT_HOLD_GATED,
    VERDICT_SOFT_DECAY,
    VERDICT_HARD_DECAY,
    VERDICT_MARGIN_KILLED,
    VERDICT_UNMEASURABLE,
    VERDICT_ERROR,
)


def _v(**kw):
    base = dict(baseline_sharpe=1.5, resim_sharpe=1.5, resim_margin_bps=20.0, can_submit=True)
    base.update(kw)
    return build_resim_verdict(**base)


def test_is_stale_resim():
    assert is_stale_resim(1.35, 1.35) is True
    assert is_stale_resim(1.35, 1.3505, stale_eps=1e-3) is True
    assert is_stale_resim(1.35, 1.34) is False
    assert is_stale_resim(None, 1.35) is False
    assert is_stale_resim(1.35, None) is False


def test_error_when_no_resim():
    out = _v(resim_sharpe=None, error=None)
    assert out["verdict"] == VERDICT_ERROR
    out2 = _v(resim_sharpe=1.0, error="sim_timeout(600s)")
    assert out2["verdict"] == VERDICT_ERROR


def test_dedup_cache_is_unmeasurable():
    # resim == baseline (within eps) → BRAIN returned stored value, not current data.
    out = _v(baseline_sharpe=1.35, resim_sharpe=1.35, resim_margin_bps=91.9)
    assert out["verdict"] == VERDICT_UNMEASURABLE
    assert "无法测" in out["reason"]


def test_margin_killed_takes_priority_over_sharpe():
    # Sharpe holds (1.5→1.5-ish) but margin below 5bps floor → economically dead.
    out = build_resim_verdict(baseline_sharpe=1.5, resim_sharpe=1.45,
                              resim_margin_bps=3.0, can_submit=True)
    assert out["verdict"] == VERDICT_MARGIN_KILLED


def test_stable_relative_band():
    # resim/baseline = 1.34/1.35 = 0.99 ≥ 0.9 → stable.
    out = build_resim_verdict(baseline_sharpe=1.35, resim_sharpe=1.34,
                              resim_margin_bps=95.0, can_submit=True)
    assert out["verdict"] == VERDICT_STABLE
    assert out["resim_pct"] == pytest.approx(1.34 / 1.35)


def test_soft_decay_band():
    # 1.05/1.5 = 0.70 → between 0.6 and 0.9 → soft.
    out = build_resim_verdict(baseline_sharpe=1.5, resim_sharpe=1.05,
                              resim_margin_bps=20.0, can_submit=True)
    assert out["verdict"] == VERDICT_SOFT_DECAY


def test_hard_decay_band():
    # 2.01 → -0.74 (real mLxlen69) → ratio < 0.6 → hard.
    out = build_resim_verdict(baseline_sharpe=2.01, resim_sharpe=-0.74,
                              resim_margin_bps=10.0, can_submit=True)
    assert out["verdict"] == VERDICT_HARD_DECAY


def test_hold_gated_when_stable_but_not_submittable():
    # Held on current data BUT can_submit False → held-but-gated (held ≠ submit).
    out = build_resim_verdict(baseline_sharpe=1.35, resim_sharpe=1.34,
                              resim_margin_bps=95.0, can_submit=False)
    assert out["verdict"] == VERDICT_HOLD_GATED


def test_margin_none_does_not_trigger_killed():
    # When margin is unknown, do not classify margin_killed; fall through to band.
    out = build_resim_verdict(baseline_sharpe=1.5, resim_sharpe=1.45,
                              resim_margin_bps=None, can_submit=True)
    assert out["verdict"] == VERDICT_STABLE


def test_baseline_near_zero_falls_back_to_absolute():
    out = build_resim_verdict(baseline_sharpe=0.05, resim_sharpe=1.4,
                              resim_margin_bps=20.0, can_submit=True)
    assert out["verdict"] == VERDICT_STABLE  # abs ≥ 1.25
    out2 = build_resim_verdict(baseline_sharpe=0.05, resim_sharpe=0.9,
                               resim_margin_bps=20.0, can_submit=True)
    assert out2["verdict"] == VERDICT_HARD_DECAY


def test_basis_is_always_is():
    assert _v()["basis"] == "IS"
