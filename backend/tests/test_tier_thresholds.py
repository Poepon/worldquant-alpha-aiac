"""Tests for backend.agents.graph.tier_thresholds.get_tier_thresholds."""
from __future__ import annotations

import pytest

from backend.agents.graph.tier_thresholds import get_min_seed_count, get_tier_thresholds
from backend.config import settings


class TestTier1Thresholds:
    def test_tier1_pass_thresholds_match_settings(self):
        t = get_tier_thresholds(1)
        assert t["tier"] == 1
        assert t["sharpe_min"] == settings.TIER1_SHARPE_MIN
        assert t["fitness_min"] == settings.TIER1_FITNESS_MIN
        assert t["turnover_max"] == settings.TIER1_TURNOVER_MAX
        assert t["subuniv_min"] == settings.TIER1_SUBUNIV_MIN

    def test_tier1_skips_self_corr_and_concentrated(self):
        """T1 task does NOT call correlation_service or read CONCENTRATED_WEIGHT."""
        t = get_tier_thresholds(1)
        assert t["check_self_corr"] is False
        assert t["check_concentrated"] is False
        assert t["self_corr_max"] is None

    def test_tier1_provisional(self):
        t = get_tier_thresholds(1)
        prov = t["provisional"]
        assert prov is not None
        assert prov["sharpe_min"] == settings.TIER1_PROVISIONAL_SHARPE_MIN
        assert prov["fitness_min"] == settings.TIER1_PROVISIONAL_FITNESS_MIN


class TestTier2Thresholds:
    def test_tier2_pass_thresholds_match_settings(self):
        t = get_tier_thresholds(2)
        assert t["tier"] == 2
        assert t["sharpe_min"] == settings.TIER2_SHARPE_MIN
        assert t["fitness_min"] == settings.TIER2_FITNESS_MIN
        assert t["turnover_max"] == settings.TIER2_TURNOVER_MAX

    def test_tier2_skips_self_corr_but_checks_concentrated(self):
        """T2 task allows same-seed wrapper variants to coexist (no self_corr check)."""
        t = get_tier_thresholds(2)
        assert t["check_self_corr"] is False
        assert t["self_corr_max"] is None
        assert t["check_concentrated"] is True

    def test_tier2_turnover_max_is_055(self):
        """T2 turnover ceiling is 0.55 to coordinate with T3 trade_when's effective downscaling."""
        t = get_tier_thresholds(2)
        assert t["turnover_max"] == 0.55


class TestTier3Thresholds:
    def test_tier3_pass_thresholds_align_with_legacy(self):
        """T3 PASS bar mirrors the original SHARPE_MIN=1.5 / FITNESS_MIN=1.0 — submission-ready."""
        t = get_tier_thresholds(3)
        assert t["tier"] == 3
        assert t["sharpe_min"] == 1.5
        assert t["fitness_min"] == 1.0
        assert t["turnover_max"] == 0.70

    def test_tier3_enforces_self_corr_and_concentrated(self):
        """T3 is the only tier that gates on self_corr and concentrated_weight."""
        t = get_tier_thresholds(3)
        assert t["check_self_corr"] is True
        assert t["self_corr_max"] == 0.7
        assert t["check_concentrated"] is True

    def test_tier3_subuniv_uses_dynamic_brain_limit(self):
        """T3 sub-universe min reads from BRAIN's dynamic LOW_SUB_UNIVERSE_SHARPE limit at runtime."""
        t = get_tier_thresholds(3)
        assert t["subuniv_min"] is None  # signals "use BRAIN dynamic"

    def test_tier3_provisional_subuniv_factor(self):
        t = get_tier_thresholds(3)
        prov = t["provisional"]
        assert prov is not None
        assert prov["subuniv_dynamic_factor"] == 0.7


class TestUnknownTier:
    """tier=None / 0 / 99 should fall back to legacy global thresholds."""

    @pytest.mark.parametrize("tier_in", [None, 0, 99, -1])
    def test_falls_back_to_legacy(self, tier_in):
        t = get_tier_thresholds(tier_in)
        assert t["tier"] is None
        assert t["sharpe_min"] == settings.SHARPE_MIN
        assert t["fitness_min"] == settings.FITNESS_MIN
        assert t["turnover_max"] == settings.TURNOVER_MAX
        assert t["self_corr_max"] == settings.MAX_CORRELATION
        # Legacy path still gates on both checks
        assert t["check_self_corr"] is True
        assert t["check_concentrated"] is True


class TestMinSeedCount:
    def test_returns_settings_value(self):
        assert get_min_seed_count() == settings.MIN_TIER_SEED_COUNT
        # Plan default
        assert get_min_seed_count() == 5
