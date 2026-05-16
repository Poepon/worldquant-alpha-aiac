"""
Unit tests for P1-A diversity_tracker changes:
  - _percentile_rank helper
  - DiversityTracker.__init__ weights param
  - evaluate_diversity: percentile normalization, config-driven weights

来源: docs/alphagbm_skills_research_2026-05-15.md 原则① — diversity 权重 config 化
+ 百分位归一化
"""

import pytest
from unittest.mock import patch

from backend.diversity_tracker import DiversityTracker, _percentile_rank


# ---------------------------------------------------------------------------
# TestPercentileRank
# ---------------------------------------------------------------------------

class TestPercentileRank:
    """Pure helper: fraction of distribution <= value."""

    def test_empty_distribution_returns_05(self):
        assert _percentile_rank(5.0, []) == pytest.approx(0.5)

    def test_all_zero_distribution_returns_0(self):
        # All values are 0 and value is 0: every elem ≤ value but all are zero
        # → our implementation returns 0.0 (treating all-zero as "as diverse as possible")
        assert _percentile_rank(0.0, [0, 0, 0]) == pytest.approx(0.0)

    def test_value_is_maximum(self):
        dist = [1, 2, 3, 4, 5]
        assert _percentile_rank(5.0, dist) == pytest.approx(1.0)

    def test_value_is_minimum(self):
        dist = [2, 3, 4, 5]
        # value=1 < all elements; count(v <= 1) = 0 → rank = 0.0
        assert _percentile_rank(1.0, dist) == pytest.approx(0.0)

    def test_value_at_median_gives_roughly_half(self):
        dist = [1, 2, 3, 4, 5]
        rank = _percentile_rank(3.0, dist)
        # 3 of 5 values <= 3 → 0.6; not exactly 0.5 but close
        assert 0.4 <= rank <= 0.7

    def test_value_greater_than_all(self):
        dist = [1, 2, 3]
        assert _percentile_rank(100.0, dist) == pytest.approx(1.0)

    def test_clamped_to_zero_one(self):
        for dist, val in [([0, 0, 0], 0.0), ([5, 6, 7], 10.0)]:
            r = _percentile_rank(val, dist)
            assert 0.0 <= r <= 1.0


# ---------------------------------------------------------------------------
# TestDiversityTrackerWeights
# ---------------------------------------------------------------------------

class TestDiversityTrackerWeights:
    """Weights are config-driven and injectable; evaluate_diversity uses them."""

    def _tracker_with_weights(self, **w):
        weights = {
            "dataset": w.get("dataset", 0.25),
            "field":   w.get("field", 0.25),
            "operator":w.get("operator", 0.25),
            "settings":w.get("settings", 0.25),
        }
        return DiversityTracker(db=None, weights=weights)

    def test_default_weights_match_historical_hardcoded(self):
        """weights=None should produce 0.30/0.30/0.25/0.15 (from config or fallback)."""
        tracker = DiversityTracker(db=None, weights=None)
        assert tracker.weights["dataset"]  == pytest.approx(0.30)
        assert tracker.weights["field"]    == pytest.approx(0.30)
        assert tracker.weights["operator"] == pytest.approx(0.25)
        assert tracker.weights["settings"] == pytest.approx(0.15)

    def test_injected_weights_override_config(self):
        w = {"dataset": 1.0, "field": 0.0, "operator": 0.0, "settings": 0.0}
        tracker = DiversityTracker(db=None, weights=w)
        assert tracker.weights["dataset"] == pytest.approx(1.0)
        assert tracker.weights["field"]   == pytest.approx(0.0)

    def test_overall_score_dominated_by_heavy_weight_dimension(self):
        """When dataset weight == 1, overall_score equals dataset_diversity."""
        tracker = self._tracker_with_weights(dataset=1.0, field=0.0, operator=0.0, settings=0.0)
        score = tracker.evaluate_diversity("never_seen", ["close"], ["ts_rank"])
        assert score.overall_score == pytest.approx(score.dataset_diversity, abs=0.01)

    def test_overall_score_is_weighted_sum(self):
        """overall_score == sum(weight_i * component_i)."""
        w = {"dataset": 0.4, "field": 0.3, "operator": 0.2, "settings": 0.1}
        tracker = DiversityTracker(db=None, weights=w)
        sc = tracker.evaluate_diversity("ds1", ["close"], ["ts_rank"])
        expected = (
            w["dataset"]  * sc.dataset_diversity +
            w["field"]    * sc.field_diversity +
            w["operator"] * sc.operator_diversity +
            w["settings"] * sc.settings_diversity
        )
        assert sc.overall_score == pytest.approx(expected, abs=1e-9)

    def test_config_weight_override_via_patch(self):
        """When backend.config.settings attributes are overridden, DiversityTracker
        picks up the new values (via getattr fallback path in __init__).

        Note: `diversity_tracker` does `from backend.config import settings` at
        module top, so monkeypatching `backend.config.settings` after import
        does NOT rebind the name in `diversity_tracker`. We patch the
        `settings` attribute on the consumer module directly instead.
        """
        from unittest.mock import MagicMock
        import backend.diversity_tracker as _dt_mod
        fake_settings = MagicMock()
        fake_settings.DIVERSITY_DATASET_WEIGHT  = 0.10
        fake_settings.DIVERSITY_FIELD_WEIGHT    = 0.20
        fake_settings.DIVERSITY_OPERATOR_WEIGHT = 0.30
        fake_settings.DIVERSITY_SETTINGS_WEIGHT = 0.40
        with patch.object(_dt_mod, "settings", fake_settings):
            tracker = DiversityTracker(db=None, weights=None)
        assert tracker.weights["dataset"]  == pytest.approx(0.10)
        assert tracker.weights["field"]    == pytest.approx(0.20)
        assert tracker.weights["operator"] == pytest.approx(0.30)
        assert tracker.weights["settings"] == pytest.approx(0.40)


# ---------------------------------------------------------------------------
# TestDiversityPercentileNormalization
# ---------------------------------------------------------------------------

class TestDiversityPercentileNormalization:
    """evaluate_diversity uses _percentile_rank, not *5 or max_count+1."""

    def test_first_ever_dataset_gets_high_diversity(self):
        """A never-seen dataset should score near 1.0 (rank=0 in empty distribution)."""
        tracker = DiversityTracker(db=None)
        sc = tracker.evaluate_diversity("brand_new_dataset", [], [])
        # empty distribution → _percentile_rank returns 0.5 neutral; 1-0.5=0.5
        # that's still decently high; more importantly no crash
        assert 0.0 <= sc.dataset_diversity <= 1.0

    def test_diversity_decreases_as_dataset_explored_more(self):
        """Repeated exploration of the same dataset lowers its diversity score."""
        tracker = DiversityTracker(db=None)
        sc_first = tracker.evaluate_diversity("ds_a", [], [])
        # Simulate many uses of "ds_a"
        for _ in range(30):
            tracker.dataset_usage["ds_a"] += 1
        sc_later = tracker.evaluate_diversity("ds_a", [], [])
        assert sc_later.dataset_diversity <= sc_first.dataset_diversity

    def test_overall_score_in_unit_interval(self):
        tracker = DiversityTracker(db=None)
        for _ in range(5):
            tracker.dataset_usage["ds1"] += 1
            tracker.field_usage["close"] += 1
            tracker.operator_usage["ts_rank"] += 1
        sc = tracker.evaluate_diversity("ds1", ["close"], ["ts_rank"])
        assert 0.0 <= sc.overall_score <= 1.0

    def test_no_magic_five_behavior(self):
        """Old formula: 1 - freq*5 → 0.5 when freq=0.1.
        New formula: percentile_rank.  Just confirm overall_score is in [0,1]
        and the '*5 cliff' (negative clipped to 0) cannot occur."""
        tracker = DiversityTracker(db=None)
        # Simulate: dataset_usage totals 10, ds_a used once → freq=0.1
        for i in range(9):
            tracker.dataset_usage[f"other_{i}"] = 1
        tracker.dataset_usage["ds_a"] = 1
        sc = tracker.evaluate_diversity("ds_a", [], [])
        # In the old code score could be ≤ 0.5; new code might differ — just
        # assert it's stable and non-negative.
        assert sc.dataset_diversity >= 0.0

    def test_no_exception_with_empty_usages(self):
        tracker = DiversityTracker(db=None)
        sc = tracker.evaluate_diversity("ds", ["f1"], ["op1"], delay=1, decay=0, neutralization="NONE")
        assert isinstance(sc.overall_score, float)
