"""Unit tests for P2-B diversity_tracker pillar dimension (M4 fix).

来源: docs/alphagbm_skills_research_2026-05-15.md skill `compare`.

Key invariants:
  - ``ENABLE_PILLAR_AWARE_SELECTION=False`` OR ``pillar=None`` →
    overall_score is the P1-A 4-dim formula byte-for-byte.
  - ``ENABLE_PILLAR_AWARE_SELECTION=True`` AND ``pillar`` non-None →
    overall_score is the 5-dim renormalised weighted sum.
  - get_pillar_balance() respects PILLAR_TARGET_DISTRIBUTION + reports
    skew / deficits / next_pillar correctly.
  - ExplorationRecord.pillar tallied into pillar_usage via record_attempt.
"""
from __future__ import annotations

import pytest
from unittest.mock import patch

from backend.diversity_tracker import (
    DiversityTracker,
    ExplorationRecord,
)


# ---------------------------------------------------------------------------
# 4-dim invariant: P1-A path is byte-for-byte preserved
# ---------------------------------------------------------------------------

class TestP1AInvariant:
    """The default path (pillar=None / flag OFF) must compute overall_score
    exactly the way P1-A did. ``test_diversity_weights.py`` asserts on this."""

    def test_pillar_none_uses_4dim_formula(self):
        """Even with pillar weight loaded, pillar=None falls back to 4-dim."""
        tracker = DiversityTracker(db=None)
        sc = tracker.evaluate_diversity("ds_a", ["close"], ["ts_rank"])
        # Reconstruct the legacy formula directly
        expected = (
            tracker.weights["dataset"]  * sc.dataset_diversity +
            tracker.weights["field"]    * sc.field_diversity +
            tracker.weights["operator"] * sc.operator_diversity +
            tracker.weights["settings"] * sc.settings_diversity
        )
        assert sc.overall_score == pytest.approx(expected, abs=1e-9)

    def test_pillar_param_present_but_flag_off_uses_4dim(self):
        """Even when pillar is supplied, with the flag OFF (default) the
        4-dim path runs unchanged."""
        tracker = DiversityTracker(db=None)
        sc = tracker.evaluate_diversity(
            "ds_a", ["close"], ["ts_rank"], pillar="momentum",
        )
        expected = (
            tracker.weights["dataset"]  * sc.dataset_diversity +
            tracker.weights["field"]    * sc.field_diversity +
            tracker.weights["operator"] * sc.operator_diversity +
            tracker.weights["settings"] * sc.settings_diversity
        )
        assert sc.overall_score == pytest.approx(expected, abs=1e-9)

    def test_p1a_test_diversity_weights_still_passes(self):
        """Sanity: the P1-A invariants (default 4-dim weights match the
        historical hardcoded 0.30/0.30/0.25/0.15) hold."""
        tracker = DiversityTracker(db=None, weights=None)
        assert tracker.weights["dataset"]  == pytest.approx(0.30)
        assert tracker.weights["field"]    == pytest.approx(0.30)
        assert tracker.weights["operator"] == pytest.approx(0.25)
        assert tracker.weights["settings"] == pytest.approx(0.15)
        # 5th weight is additive — old 4 weights still match.


# ---------------------------------------------------------------------------
# 5-dim path: flag + pillar both present
# ---------------------------------------------------------------------------

class TestFiveDimEnabledPath:
    def test_5dim_renormalised_when_enabled(self):
        """Flag ON + pillar provided → 5-dim renormalised weighted sum."""
        import backend.config as _cfg_mod
        import backend.diversity_tracker as _div_mod
        # Patch the same settings symbol that diversity_tracker reads.
        class FakeSettings:
            ENABLE_PILLAR_AWARE_SELECTION = True
            DIVERSITY_DATASET_WEIGHT = 0.30
            DIVERSITY_FIELD_WEIGHT = 0.30
            DIVERSITY_OPERATOR_WEIGHT = 0.25
            DIVERSITY_SETTINGS_WEIGHT = 0.15
            DIVERSITY_PILLAR_WEIGHT = 0.20
        with patch.object(_cfg_mod, "settings", FakeSettings()):
            tracker = DiversityTracker(db=None)
            sc = tracker.evaluate_diversity(
                "ds_a", ["close"], ["ts_rank"], pillar="momentum",
            )
        w = tracker.weights
        total = w["dataset"] + w["field"] + w["operator"] + w["settings"] + w["pillar"]
        expected = (
            w["dataset"]  / total * sc.dataset_diversity +
            w["field"]    / total * sc.field_diversity +
            w["operator"] / total * sc.operator_diversity +
            w["settings"] / total * sc.settings_diversity +
            w["pillar"]   / total * sc.pillar_diversity
        )
        assert sc.overall_score == pytest.approx(expected, abs=1e-9)

    def test_pillar_diversity_computed_when_pillar_present(self):
        """pillar_diversity is calculated regardless of the flag — only
        overall_score gating depends on the flag."""
        tracker = DiversityTracker(db=None)
        tracker.pillar_usage["momentum"] = 5
        sc = tracker.evaluate_diversity(
            "ds_a", ["close"], ["ts_rank"], pillar="momentum",
        )
        assert 0.0 <= sc.pillar_diversity <= 1.0

    def test_pillar_diversity_zero_when_pillar_none(self):
        tracker = DiversityTracker(db=None)
        sc = tracker.evaluate_diversity("ds_a", ["close"], ["ts_rank"])
        assert sc.pillar_diversity == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# record_attempt populates pillar_usage
# ---------------------------------------------------------------------------

class TestPillarUsageRecording:
    def test_record_with_pillar_increments_counter(self):
        tracker = DiversityTracker(db=None)
        rec = ExplorationRecord(
            dataset_id="ds1", region="USA", universe="TOP3000",
            fields_used=["close"], operators_used=["ts_rank"],
            pillar="momentum",
        )
        tracker.record_attempt(rec)
        assert tracker.pillar_usage["momentum"] == 1

    def test_record_without_pillar_does_not_create_unknown_bucket(self):
        """ExplorationRecord.pillar=None should NOT increment any pillar
        counter (we don't want to dilute fresh ratios with backlog)."""
        tracker = DiversityTracker(db=None)
        rec = ExplorationRecord(
            dataset_id="ds1", region="USA", universe="TOP3000",
        )
        tracker.record_attempt(rec)
        assert sum(tracker.pillar_usage.values()) == 0


# ---------------------------------------------------------------------------
# get_pillar_balance observability
# ---------------------------------------------------------------------------

class TestGetPillarBalance:
    def test_empty_tracker_returns_zero_shares(self):
        tracker = DiversityTracker(db=None)
        result = tracker.get_pillar_balance()
        for share in result["shares"].values():
            assert share == 0.0

    def test_skew_computed(self):
        tracker = DiversityTracker(db=None)
        tracker.pillar_usage["momentum"] = 8
        tracker.pillar_usage["value"] = 2
        result = tracker.get_pillar_balance()
        # 8 + 2 = 10; momentum share = 0.8, value = 0.2
        assert result["shares"]["momentum"] == pytest.approx(0.8, abs=0.01)
        assert result["shares"]["value"] == pytest.approx(0.2, abs=0.01)

    def test_next_pillar_is_max_deficit(self):
        tracker = DiversityTracker(db=None)
        tracker.pillar_usage["momentum"] = 100  # over-weighted
        # All others at zero → next_pillar should be the most-target-deficient
        result = tracker.get_pillar_balance()
        assert result["next_pillar"] is not None
        assert result["next_pillar"] != "momentum"
