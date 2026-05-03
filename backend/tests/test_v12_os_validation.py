"""Tests for V-12 IS-only overfit gate (`_check_is_os_consistency`).

Scope: ensure that PASS gate rejects alphas whose IS sharpe far exceeds
OS sharpe. This was added after Spike (2026-05-02 → 03) showed
train_sharpe up to 16.2 with test_sharpe=0 (pure IS overfit).

Tiered rules:
  is_sharpe < 2:    no OS check (passes)
  2 <= is < 5:      require os_sharpe > 0 AND os/is >= 0.3
  is_sharpe >= 5:   require os_sharpe > 0 AND os/is >= 0.4
"""
from __future__ import annotations

import pytest

from backend.agents.graph.nodes.evaluation import _check_is_os_consistency


class TestLowIsSharpe:
    """is_sharpe < 2 → bypass OS check entirely."""

    @pytest.mark.parametrize("is_sh", [0.0, 0.5, 1.0, 1.5, 1.99])
    def test_low_is_passes_without_os(self, is_sh):
        # No os_sharpe / test_sharpe at all
        assert _check_is_os_consistency({"sharpe": is_sh})

    def test_low_is_passes_with_zero_os(self):
        assert _check_is_os_consistency(
            {"sharpe": 1.5, "os_sharpe": 0, "test_sharpe": 0}
        )


class TestMidIsSharpe:
    """2 <= is_sharpe < 5 → os_sharpe > 0 AND os/is >= 0.3."""

    def test_mid_is_passes_with_good_os(self):
        # is=3.0, os=1.0 → ratio 0.33 ≥ 0.3 ✓
        assert _check_is_os_consistency({"sharpe": 3.0, "os_sharpe": 1.0})

    def test_mid_is_passes_using_test_sharpe_fallback(self):
        # No os_sharpe but test_sharpe present
        assert _check_is_os_consistency(
            {"sharpe": 3.0, "test_sharpe": 1.0}
        )

    def test_mid_is_rejects_zero_os(self):
        # Spike reality: train=3.94, test=0
        assert not _check_is_os_consistency(
            {"sharpe": 3.94, "test_sharpe": 0}
        )

    def test_mid_is_rejects_no_os_field(self):
        assert not _check_is_os_consistency({"sharpe": 3.0})

    def test_mid_is_rejects_low_ratio(self):
        # is=4, os=0.5 → ratio 0.125 < 0.3
        assert not _check_is_os_consistency(
            {"sharpe": 4.0, "os_sharpe": 0.5}
        )

    def test_mid_is_threshold_boundary(self):
        # is=2, os=0.6 → ratio 0.3 == threshold (passes inclusive)
        assert _check_is_os_consistency(
            {"sharpe": 2.0, "os_sharpe": 0.6}
        )


class TestHighIsSharpe:
    """is_sharpe >= 5 → stricter os/is >= 0.4."""

    def test_high_is_passes_with_strong_os(self):
        # is=10, os=4.5 → ratio 0.45 ≥ 0.4 ✓
        assert _check_is_os_consistency({"sharpe": 10.0, "os_sharpe": 4.5})

    def test_high_is_rejects_zero_os(self):
        # Spike reality: train=16.2, test=0 (top alpha, task 32)
        assert not _check_is_os_consistency(
            {"sharpe": 16.2, "test_sharpe": 0}
        )

    def test_high_is_rejects_borderline_ratio(self):
        # is=10, os=3.5 → ratio 0.35 < 0.4
        assert not _check_is_os_consistency(
            {"sharpe": 10.0, "os_sharpe": 3.5}
        )

    def test_high_is_rejects_when_only_test_below_threshold(self):
        # Mid-range high IS, OS very weak
        assert not _check_is_os_consistency(
            {"sharpe": 5.0, "test_sharpe": 1.0}
        )

    def test_high_is_threshold_boundary(self):
        # is=5, os=2.0 → ratio 0.4 ✓
        assert _check_is_os_consistency({"sharpe": 5.0, "os_sharpe": 2.0})


class TestPriorityAndShape:
    """OS field priority + edge cases."""

    def test_os_sharpe_takes_priority_over_test_sharpe(self):
        # When both present, os_sharpe wins
        # is=3, os_sharpe=1.0, test_sharpe=0.1 → os/is=0.33 passes
        assert _check_is_os_consistency(
            {"sharpe": 3.0, "os_sharpe": 1.0, "test_sharpe": 0.1}
        )
        # is=3, os_sharpe=0.1, test_sharpe=1.5 → os/is=0.033 fails
        assert not _check_is_os_consistency(
            {"sharpe": 3.0, "os_sharpe": 0.1, "test_sharpe": 1.5}
        )

    def test_negative_os_rejected(self):
        # OS sharpe negative — alpha breaks down OOS
        assert not _check_is_os_consistency(
            {"sharpe": 3.0, "os_sharpe": -0.5}
        )

    def test_none_metrics_passes(self):
        # Missing sharpe field treated as 0 → bypass low-is gate
        assert _check_is_os_consistency({})
        assert _check_is_os_consistency({"sharpe": None})

    def test_non_dict_metrics_passes(self):
        # Defensive: malformed metrics shouldn't crash the pipeline
        assert _check_is_os_consistency(None)
        assert _check_is_os_consistency([])
