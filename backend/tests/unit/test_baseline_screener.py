"""
Unit tests for the baseline screener (P0: fit-baseline + Nσ-residual).

Tests:
- fit_baseline: sufficient / insufficient / degenerate-distribution cases
- residual_sigma: normal value / insufficient baseline / missing value
- classify_residual: bucket boundaries
"""

import pytest

from backend.baseline_screener import (
    BELOW,
    DISCOVERY,
    INSUFFICIENT_DATA,
    NORMAL,
    BaselineStats,
    classify_residual,
    fit_baseline,
    residual_sigma,
)


class TestFitBaseline:
    """Tests for fit_baseline."""

    def test_sufficient_samples(self):
        """Enough varied samples -> baseline fitted with given granularity."""
        samples = [float(x) for x in range(0, 40)]  # 40 samples, real spread
        stats = fit_baseline(samples, min_samples=30, cell_key="momentum|pv1|USA", granularity="fine")
        assert stats.granularity == "fine"
        assert stats.count == 40
        assert stats.usable is True
        assert stats.mean == pytest.approx(19.5)
        assert stats.std > 0

    def test_insufficient_samples(self):
        """Fewer than min_samples -> insufficient, not usable."""
        stats = fit_baseline([1.0, 2.0, 3.0], min_samples=30, cell_key="c", granularity="fine")
        assert stats.granularity == "insufficient"
        assert stats.usable is False
        assert stats.count == 3

    def test_degenerate_distribution(self):
        """All-identical samples (std ~ 0) -> insufficient even if count is high."""
        stats = fit_baseline([2.0] * 50, min_samples=30, cell_key="c", granularity="coarse")
        assert stats.granularity == "insufficient"
        assert stats.usable is False

    def test_none_values_filtered(self):
        """None entries are dropped before the count check."""
        samples = [None] * 10 + [float(x) for x in range(30)]
        stats = fit_baseline(samples, min_samples=30, cell_key="c", granularity="fine")
        assert stats.count == 30
        assert stats.granularity == "fine"

    def test_coarse_granularity_preserved(self):
        """granularity arg is passed through when the fit succeeds."""
        stats = fit_baseline([float(x) for x in range(40)], min_samples=10, cell_key="c", granularity="coarse")
        assert stats.granularity == "coarse"


class TestResidualSigma:
    """Tests for residual_sigma."""

    def test_normal_value(self):
        """Residual = (value - mean) / std."""
        stats = BaselineStats(mean=1.0, std=0.5, count=40, cell_key="c", granularity="fine")
        assert residual_sigma(2.0, stats) == pytest.approx(2.0)
        assert residual_sigma(0.5, stats) == pytest.approx(-1.0)

    def test_insufficient_baseline_returns_none(self):
        """An insufficient baseline yields no residual."""
        stats = BaselineStats(mean=0.0, std=0.0, count=3, cell_key="c", granularity="insufficient")
        assert residual_sigma(1.5, stats) is None

    def test_missing_value_returns_none(self):
        """A missing metric value yields no residual."""
        stats = BaselineStats(mean=1.0, std=0.5, count=40, cell_key="c", granularity="fine")
        assert residual_sigma(None, stats) is None


class TestClassifyResidual:
    """Tests for classify_residual."""

    def test_none_is_insufficient(self):
        assert classify_residual(None, discovery=2.0, below=-1.0) == INSUFFICIENT_DATA

    def test_discovery_at_and_above_threshold(self):
        assert classify_residual(2.0, discovery=2.0, below=-1.0) == DISCOVERY
        assert classify_residual(3.5, discovery=2.0, below=-1.0) == DISCOVERY

    def test_below_at_and_under_threshold(self):
        assert classify_residual(-1.0, discovery=2.0, below=-1.0) == BELOW
        assert classify_residual(-2.5, discovery=2.0, below=-1.0) == BELOW

    def test_normal_in_between(self):
        assert classify_residual(0.0, discovery=2.0, below=-1.0) == NORMAL
        assert classify_residual(1.99, discovery=2.0, below=-1.0) == NORMAL
        assert classify_residual(-0.99, discovery=2.0, below=-1.0) == NORMAL
