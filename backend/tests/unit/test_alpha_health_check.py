"""Unit tests for P1-C alpha health check pure helpers.

来源: docs/alphagbm_skills_research_2026-05-15.md skill `health-check`.

Tests the pure-function helpers in ``backend.services.alpha_health_service``
— no DB, no Celery, no FS. The integration test
(``backend/tests/integration/test_alpha_health_task.py``) covers the DB +
write-file path.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

import pytest

from backend.services.alpha_health_service import (
    SH_TZ,
    _safe_num,
    classify_stale,
    classify_drift_from_decay,
    classify_drift_from_baseline,
    classify_orphan,
    compute_health_score,
    to_band,
    recommend,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _alpha(**kwargs):
    """Build a SimpleNamespace stand-in for a SQLAlchemy Alpha row."""
    defaults = dict(
        id=1, alpha_id="t1", decay_curve=None,
        is_sharpe=None, is_fitness=None, is_turnover=None,
        hypothesis_id=None, region="USA", universe="TOP3000",
        quality_status="PASS", factor_tier=1, dataset_id=None,
        date_created=None, metrics_snapshot_at=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _baseline_stats(mean=1.0, std=0.5, count=50, granularity="fine"):
    """Build a duck-typed BaselineStats stand-in (.usable property)."""
    ns = SimpleNamespace(
        mean=mean, std=std, count=count, cell_key="k",
        granularity=granularity,
    )
    # Match BaselineStats.usable property
    ns.usable = granularity != "insufficient" and std > 1e-9
    return ns


_NOW = datetime(2026, 5, 15, 0, 0, 0, tzinfo=timezone.utc)


# ===========================================================================
# _safe_num
# ===========================================================================

class TestSafeNum:
    def test_passthrough_float(self):
        assert _safe_num(1.5) == 1.5

    def test_passthrough_int_returns_float(self):
        result = _safe_num(3)
        assert result == 3.0
        assert isinstance(result, float)

    def test_none(self):
        assert _safe_num(None) is None

    def test_bool_rejected(self):
        # bool ⊂ int in Python; rejection mirrors backend.tests.unit.test_safe_metric
        assert _safe_num(True) is None
        assert _safe_num(False) is None

    def test_nan(self):
        assert _safe_num(float("nan")) is None

    def test_inf(self):
        assert _safe_num(float("inf")) is None
        assert _safe_num(float("-inf")) is None

    def test_str(self):
        assert _safe_num("1.5") is None


# ===========================================================================
# classify_stale
# ===========================================================================

class TestClassifyStale:
    def test_none_snapshot_returns_red_never_refreshed(self):
        out = classify_stale(None, _NOW)
        assert out == {"stale_days": None, "stale_severity": "red",
                       "reason": "never_refreshed"}

    def test_fresh_returns_green(self):
        snap = _NOW - timedelta(days=1)
        out = classify_stale(snap, _NOW)
        assert out["stale_severity"] == "green"
        assert out["reason"] is None
        assert 0.9 < out["stale_days"] < 1.1

    def test_boundary_7d_green(self):
        # exactly STALE_YELLOW_DAYS = 7d → still green (<=)
        snap = _NOW - timedelta(days=7)
        out = classify_stale(snap, _NOW)
        assert out["stale_severity"] == "green"

    def test_7_01d_yellow(self):
        # just past 7d
        snap = _NOW - timedelta(days=7, hours=1)
        out = classify_stale(snap, _NOW)
        assert out["stale_severity"] == "yellow"
        assert out["reason"] == "stale_7d"

    def test_14d_yellow_boundary(self):
        snap = _NOW - timedelta(days=14)
        out = classify_stale(snap, _NOW)
        assert out["stale_severity"] == "yellow"

    def test_15d_orange(self):
        snap = _NOW - timedelta(days=15)
        out = classify_stale(snap, _NOW)
        assert out["stale_severity"] == "orange"
        assert out["reason"] == "stale_15d"

    def test_30d_orange_boundary(self):
        snap = _NOW - timedelta(days=30)
        out = classify_stale(snap, _NOW)
        assert out["stale_severity"] == "orange"

    def test_31d_red(self):
        snap = _NOW - timedelta(days=31)
        out = classify_stale(snap, _NOW)
        assert out["stale_severity"] == "red"
        assert out["reason"] == "stale_31d"

    def test_naive_datetime_handled(self):
        # naive datetime (no tzinfo) — must not raise
        naive_snap = (_NOW - timedelta(days=5)).replace(tzinfo=None)
        out = classify_stale(naive_snap, _NOW)
        assert out["stale_severity"] == "green"
        assert isinstance(out["stale_days"], float)

    def test_tz_aware_datetime_handled(self):
        sh_snap = (_NOW - timedelta(days=3)).astimezone(SH_TZ)
        out = classify_stale(sh_snap, _NOW)
        assert out["stale_severity"] == "green"

    def test_future_snapshot_clamps_to_zero(self):
        future = _NOW + timedelta(days=1)
        out = classify_stale(future, _NOW)
        assert out["stale_days"] == 0.0
        assert out["stale_severity"] == "green"


# ===========================================================================
# classify_drift_from_decay
# ===========================================================================

class TestClassifyDriftFromDecay:
    def test_empty_curve_returns_none(self):
        a = _alpha(decay_curve=[])
        assert classify_drift_from_decay(a) is None

    def test_null_curve_returns_none(self):
        a = _alpha(decay_curve=None)
        assert classify_drift_from_decay(a) is None

    def test_curve_with_non_dict_head_returns_none(self):
        a = _alpha(decay_curve=["not a dict"])
        assert classify_drift_from_decay(a) is None

    def test_curve_head_missing_sharpe_returns_none(self):
        a = _alpha(decay_curve=[{"fitness": 0.5}])
        assert classify_drift_from_decay(a) is None

    def test_sharpe_down_50_red(self):
        a = _alpha(
            decay_curve=[{"sharpe": 2.0, "fitness": 1.0, "turnover": 0.3}],
            is_sharpe=1.0, is_fitness=0.5, is_turnover=0.6,
        )
        out = classify_drift_from_decay(a)
        assert out["severity"] == "red"
        # delta = (1-2)/2 * 100 = -50 → reason includes 50pct
        assert out["sharpe_delta_pct"] == -50.0
        # Multi-metric reason: both sharpe AND fitness dropped 50%
        assert out["reason"] == "sharpe_down_50pct+fitness_down_50pct"

    def test_sharpe_down_30_orange(self):
        a = _alpha(
            decay_curve=[{"sharpe": 2.0, "fitness": 1.0, "turnover": 0.3}],
            is_sharpe=1.4, is_fitness=0.8, is_turnover=0.4,
        )
        out = classify_drift_from_decay(a)
        assert out["severity"] == "orange"
        assert out["sharpe_delta_pct"] == -30.0

    def test_sharpe_up_green(self):
        a = _alpha(
            decay_curve=[{"sharpe": 1.0, "fitness": 0.5, "turnover": 0.3}],
            is_sharpe=1.5, is_fitness=0.8, is_turnover=0.3,
        )
        out = classify_drift_from_decay(a)
        assert out["severity"] == "green"
        assert out["reason"] is None
        assert out["sharpe_delta_pct"] == 50.0

    def test_zero_baseline_returns_none_in_delta(self):
        a = _alpha(
            decay_curve=[{"sharpe": 0.0, "fitness": 0.0, "turnover": 0.0}],
            is_sharpe=1.5, is_fitness=0.5, is_turnover=0.3,
        )
        out = classify_drift_from_decay(a)
        # All deltas None because abs(base) < 1e-9
        assert out["sharpe_delta_pct"] is None
        assert out["severity"] == "unknown"

    def test_negative_baseline_uses_abs(self):
        # base sharpe -1.0; current 0.0 → delta = (0 - (-1)) / |-1| * 100 = +100
        a = _alpha(
            decay_curve=[{"sharpe": -1.0}],
            is_sharpe=0.0, is_fitness=None, is_turnover=None,
        )
        out = classify_drift_from_decay(a)
        assert out["sharpe_delta_pct"] == 100.0
        assert out["severity"] == "green"

    def test_current_nan_isolated_returns_none(self):
        a = _alpha(
            decay_curve=[{"sharpe": 1.5}],
            is_sharpe=float("nan"),
        )
        out = classify_drift_from_decay(a)
        # sharpe drops to None → delta None → severity unknown
        assert out["sharpe_delta_pct"] is None
        assert out["severity"] == "unknown"

    def test_legacy_decay_no_fitness_key(self):
        # historical decay rows may have only sharpe — must not raise
        a = _alpha(
            decay_curve=[{"sharpe": 1.5}],
            is_sharpe=1.2, is_fitness=0.5, is_turnover=0.3,
        )
        out = classify_drift_from_decay(a)
        # base_fitness None → delta None
        assert out["fitness_delta_pct"] is None
        assert out["turnover_delta_pct"] is None
        # sharpe delta = (1.2-1.5)/1.5*100 = -20 → orange
        assert out["sharpe_delta_pct"] == -20.0
        assert out["severity"] == "yellow"

    def test_severity_independent_of_turnover_direction(self):
        # severity is sharpe+fitness only — turnover sign change shouldn't
        # affect band (sharpe and fitness are constant in both alphas).
        head = {"sharpe": 1.5, "fitness": 1.0, "turnover": 0.5}
        a1 = _alpha(decay_curve=[head], is_sharpe=1.5,
                    is_fitness=1.0, is_turnover=0.3)
        a2 = _alpha(decay_curve=[head], is_sharpe=1.5,
                    is_fitness=1.0, is_turnover=0.8)
        assert (classify_drift_from_decay(a1)["severity"]
                == classify_drift_from_decay(a2)["severity"])

    # P2 fix: drift severity now considers fitness too, not just sharpe.

    def test_fitness_collapse_drives_severity_when_sharpe_holds(self):
        """Sharpe steady but fitness craters → severity must surface red.
        Pre-fix this returned green because severity gated on sharpe only."""
        a = _alpha(
            decay_curve=[{"sharpe": 1.5, "fitness": 1.0, "turnover": 0.4}],
            is_sharpe=1.5,         # 0% delta
            is_fitness=0.4,        # -60% delta → red
            is_turnover=0.4,
        )
        out = classify_drift_from_decay(a)
        assert out["severity"] == "red"
        assert out["sharpe_delta_pct"] == 0.0
        assert out["fitness_delta_pct"] == -60.0
        assert "fitness_down_60pct" in (out["reason"] or "")

    def test_worst_of_sharpe_fitness_used(self):
        """sharpe yellow + fitness orange → severity orange (worst-of)."""
        a = _alpha(
            decay_curve=[{"sharpe": 1.0, "fitness": 1.0, "turnover": 0.4}],
            is_sharpe=0.85,        # -15% → yellow
            is_fitness=0.65,       # -35% → orange
            is_turnover=0.4,
        )
        out = classify_drift_from_decay(a)
        assert out["severity"] == "orange"


# ===========================================================================
# classify_drift_from_baseline
# ===========================================================================

class TestClassifyDriftFromBaseline:
    def test_none_stats_returns_unknown(self):
        a = _alpha(is_sharpe=1.0)
        out = classify_drift_from_baseline(a, None)
        assert out["severity"] == "unknown"
        assert out["baseline_source"] == "none"
        assert out["reason"] == "no_baseline_available"

    def test_not_usable_stats_returns_unknown(self):
        a = _alpha(is_sharpe=1.0)
        out = classify_drift_from_baseline(
            a, _baseline_stats(granularity="insufficient"),
        )
        assert out["severity"] == "unknown"
        assert out["baseline_source"] == "none"

    def test_usable_stats_sharpe_below_mean_red(self):
        a = _alpha(is_sharpe=0.5)
        out = classify_drift_from_baseline(a, _baseline_stats(mean=2.0))
        # delta = (0.5-2)/2*100 = -75 → red
        assert out["severity"] == "red"
        assert out["baseline_source"] == "cluster_baseline"
        assert out["sharpe_delta_pct"] == -75.0
        assert "cluster_mean_75pct" in out["reason"]

    def test_usable_stats_sharpe_above_mean_green(self):
        a = _alpha(is_sharpe=2.0)
        out = classify_drift_from_baseline(a, _baseline_stats(mean=1.0))
        assert out["severity"] == "green"
        assert out["reason"] is None

    def test_current_sharpe_nan_returns_unknown(self):
        a = _alpha(is_sharpe=float("nan"))
        out = classify_drift_from_baseline(a, _baseline_stats(mean=1.0))
        # current None → delta None → severity unknown
        assert out["severity"] == "unknown"


# ===========================================================================
# classify_orphan
# ===========================================================================

class TestClassifyOrphan:
    def test_not_in_kb_index_returns_green_not_orphan(self):
        a = _alpha(id=42)
        out = classify_orphan(a, kb_index={})
        assert out == {"is_kb_referenced": False, "is_orphan": False,
                       "kb_entries": [], "severity": "green"}

    def test_in_kb_with_active_returns_referenced(self):
        a = _alpha(id=42)
        kb = {42: [{"kb_id": 100, "kb_is_active": True}]}
        out = classify_orphan(a, kb)
        assert out["is_kb_referenced"] is True
        assert out["is_orphan"] is False
        assert out["severity"] == "green"

    def test_in_kb_all_inactive_returns_not_referenced(self):
        a = _alpha(id=42)
        kb = {42: [{"kb_id": 100, "kb_is_active": False}]}
        out = classify_orphan(a, kb)
        assert out["is_kb_referenced"] is False
        assert out["is_orphan"] is False  # in-scope: never orphan

    def test_multiple_kb_entries_collected(self):
        a = _alpha(id=42)
        kb = {42: [
            {"kb_id": 100, "kb_is_active": True},
            {"kb_id": 101, "kb_is_active": False},
            {"kb_id": 102, "kb_is_active": True},
        ]}
        out = classify_orphan(a, kb)
        assert len(out["kb_entries"]) == 3
        assert out["is_kb_referenced"] is True

    def test_active_inactive_distinguished_in_payload(self):
        a = _alpha(id=42)
        kb = {42: [
            {"kb_id": 100, "kb_is_active": True},
            {"kb_id": 101, "kb_is_active": False},
        ]}
        out = classify_orphan(a, kb)
        # all entries kept in payload regardless of active flag
        ids = [e["kb_id"] for e in out["kb_entries"]]
        assert ids == [100, 101]


# ===========================================================================
# compute_health_score
# ===========================================================================

class TestComputeHealthScore:
    def _green_stale(self):
        return {"stale_severity": "green", "reason": None}

    def _green_drift(self):
        return {"severity": "green", "reason": None}

    def _green_orphan(self):
        return {"is_orphan": False}

    def test_all_green_returns_100(self):
        out = compute_health_score(
            self._green_stale(), self._green_drift(), self._green_orphan(),
        )
        assert out["score"] == 100.0
        assert out["stale_pen"] == 0
        assert out["drift_pen"] == 0
        assert out["orphan_pen"] == 0

    def test_only_stale_red_calculates(self):
        # stale_pen=90 * 0.35 = 31.5 → score = 100 - 31.5 = 68.5
        out = compute_health_score(
            {"stale_severity": "red", "reason": "stale_60d"},
            self._green_drift(), self._green_orphan(),
        )
        assert out["score"] == pytest.approx(68.5, abs=0.1)
        assert out["stale_pen"] == 90

    def test_only_drift_red_calculates(self):
        # drift_pen=90 * 0.50 = 45 → score = 55
        out = compute_health_score(
            self._green_stale(),
            {"severity": "red", "reason": "sharpe_down_70pct"},
            self._green_orphan(),
        )
        assert out["score"] == pytest.approx(55.0, abs=0.1)
        assert out["drift_pen"] == 90

    def test_orphan_true_dominates(self):
        # orphan_pen=100 * 0.15 = 15
        out = compute_health_score(
            self._green_stale(), self._green_drift(),
            {"is_orphan": True},
        )
        assert out["score"] == pytest.approx(85.0, abs=0.1)
        assert out["orphan_pen"] == 100

    def test_unknown_drift_skipped_not_penalised(self):
        # P2 fix: unknown drift is data deficiency, not a quality problem.
        # Drift weight redistributed across stale + orphan; both green here
        # → score stays at 100 (was 87.5 under the old "25 penalty" semantics).
        out = compute_health_score(
            self._green_stale(),
            {"severity": "unknown", "reason": "no_baseline_available"},
            self._green_orphan(),
        )
        assert out["score"] == pytest.approx(100.0, abs=0.1)
        assert out["drift_pen"] is None  # signal skipped, not "midpoint"

    def test_unknown_drift_renormalises_other_signals(self):
        """Drift skipped → stale red still contributes via renormalised weight.
        Pre-fix: unknown contributed 12.5; now stale's weight scales up to fill."""
        out = compute_health_score(
            {"stale_severity": "red", "reason": "stale_31d"},
            {"severity": "unknown", "reason": "no_baseline_available"},
            self._green_orphan(),
        )
        # weights: stale=0.35, orphan=0.15, applied total = 0.50
        # weighted_pen = (0.35*90 + 0.15*0) / 0.50 = 31.5/0.5 = 63
        # score = 100 - 63 = 37 → RED band
        assert out["score"] == pytest.approx(37.0, abs=0.1)
        assert out["drift_pen"] is None
        assert out["stale_pen"] == 90

    def test_unknown_stale_also_skipped(self):
        """Mirror: a stale_severity not in the band table is also skipped."""
        out = compute_health_score(
            {"stale_severity": "unknown", "reason": None},
            {"severity": "green", "reason": None},
            self._green_orphan(),
        )
        assert out["score"] == pytest.approx(100.0, abs=0.1)
        assert out["stale_pen"] is None

    def test_score_clipped_to_0_100(self):
        # All red + orphan: 0.35*90 + 0.5*90 + 0.15*100 = 31.5+45+15 = 91.5
        # 100 - 91.5 = 8.5 (still within 0-100)
        # Force a degenerate test with a weights override is awkward — instead
        # just verify the clip path exists by checking score never exceeds 100
        # or drops below 0 across the full sev x sev x orphan grid.
        for stale_sev in ("green", "yellow", "orange", "red"):
            for drift_sev in ("green", "yellow", "orange", "red", "unknown"):
                for is_orphan in (False, True):
                    out = compute_health_score(
                        {"stale_severity": stale_sev, "reason": None},
                        {"severity": drift_sev, "reason": None},
                        {"is_orphan": is_orphan},
                    )
                    assert 0.0 <= out["score"] <= 100.0


# ===========================================================================
# to_band
# ===========================================================================

class TestToBand:
    def test_100_green(self):
        assert to_band(100.0) == "GREEN"

    def test_85_green_boundary(self):
        assert to_band(85.0) == "GREEN"

    def test_84_9999_yellow(self):
        assert to_band(84.9999) == "YELLOW"

    def test_70_yellow_boundary(self):
        assert to_band(70.0) == "YELLOW"

    def test_50_orange_boundary(self):
        assert to_band(50.0) == "ORANGE"

    def test_30_red_boundary(self):
        assert to_band(30.0) == "RED"

    def test_0_critical(self):
        assert to_band(0.0) == "CRITICAL"
        assert to_band(29.999) == "CRITICAL"


# ===========================================================================
# recommend
# ===========================================================================

class TestRecommend:
    def _signals(self, stale=None, drift=None, orphan=None):
        return {
            "stale": {"reason": stale},
            "drift": {"reason": drift},
            "orphan": {"reason": orphan},
        }

    def test_green_keep_metrics_healthy(self):
        action, reason = recommend("GREEN", self._signals())
        assert action == "keep"
        assert reason == "metrics healthy"

    def test_yellow_monitor(self):
        action, reason = recommend(
            "YELLOW", self._signals(stale="stale_10d"),
        )
        assert action == "monitor"
        assert "stale_10d" in reason

    def test_orange_review(self):
        action, reason = recommend(
            "ORANGE", self._signals(drift="sharpe_down_30pct"),
        )
        assert action == "review"
        assert "sharpe_down_30pct" in reason

    def test_red_consider_demote(self):
        action, reason = recommend(
            "RED", self._signals(drift="sharpe_down_60pct"),
        )
        assert action == "consider_demote"
        assert "sharpe_down_60pct" in reason

    def test_critical_includes_manual_triage_needed(self):
        action, reason = recommend(
            "CRITICAL",
            self._signals(stale="stale_60d", drift="sharpe_down_80pct"),
        )
        assert action == "investigate"
        assert "manual triage needed" in reason
        assert "stale_60d" in reason
        assert "sharpe_down_80pct" in reason

    def test_no_issues_when_no_reasons(self):
        # YELLOW with empty signals → reason "no issues"
        action, reason = recommend("YELLOW", self._signals())
        assert action == "monitor"
        assert reason == "no issues"


# ===========================================================================
# Pure-function dict-key contracts (snapshot-style)
# ===========================================================================

class TestPureFunctionsContractStability:
    def test_score_dict_keys_stable(self):
        out = compute_health_score(
            {"stale_severity": "green"},
            {"severity": "green"},
            {"is_orphan": False},
        )
        assert set(out.keys()) == {"score", "stale_pen", "drift_pen", "orphan_pen"}

    def test_drift_dict_keys_stable_decay_path(self):
        a = _alpha(
            decay_curve=[{"sharpe": 1.0, "fitness": 0.5, "turnover": 0.3}],
            is_sharpe=1.0, is_fitness=0.5, is_turnover=0.3,
        )
        out = classify_drift_from_decay(a)
        expected = {
            "baseline_source", "baseline_sharpe", "current_sharpe",
            "sharpe_delta_pct", "baseline_fitness", "current_fitness",
            "fitness_delta_pct", "baseline_turnover", "current_turnover",
            "turnover_delta_pct", "severity", "reason",
        }
        assert set(out.keys()) == expected
        assert out["baseline_source"] == "decay_curve_head"

    def test_drift_dict_keys_stable_baseline_path(self):
        a = _alpha(is_sharpe=1.0)
        out = classify_drift_from_baseline(a, _baseline_stats(mean=1.0))
        expected = {
            "baseline_source", "baseline_sharpe", "current_sharpe",
            "sharpe_delta_pct", "severity", "reason",
        }
        assert set(out.keys()) == expected
        assert out["baseline_source"] == "cluster_baseline"
