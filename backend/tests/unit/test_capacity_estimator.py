"""B1 R11 alpha_capacity_estimator unit tests (Phase 4 Sprint 2 / plan v5 §6.8).

Coverage:
  - estimate() — formula sanity (ADV × universe × max_share × decay)
  - estimate() — turnover decay clamp at 50%
  - estimate() — negative / non-numeric turnover handled
  - estimate() — unknown (region, universe) → conservative default
  - normalize() — log bucket boundaries
  - normalize() — handles 0 / negative / Infinity
  - estimate_from_alpha_dict() — multiple turnover key paths
  - estimate_from_alpha_dict() — missing region/universe → 0
  - Soft-fail when JSON missing / corrupt
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.services import capacity_estimator as cap


@pytest.fixture(autouse=True)
def _isolate_adv_cache():
    """Each test starts with a clean lru_cache so monkeypatching
    _ADV_JSON_PATH actually takes effect (the lru_cache otherwise
    serves the first-test JSON across all subsequent tests)."""
    cap.clear_adv_table_cache()
    yield
    cap.clear_adv_table_cache()


# ---------------------------------------------------------------------------
# estimate()
# ---------------------------------------------------------------------------

import math as _math


def test_estimate_usa_top3000_basic():
    """USA TOP3000, 30% turnover. sqrt scaling: 2e7 × 0.10 × √3000 × 1.0 ≈ $110M."""
    c = cap.estimate(region="USA", universe="TOP3000", turnover=0.30)
    expected = 2e7 * 0.10 * _math.sqrt(3000) * 1.0  # decay 0 (turnover < 50%)
    assert c == pytest.approx(expected, rel=1e-3)


def test_estimate_high_turnover_decays():
    """Turnover > 50% triggers decay; at turnover=1.5 → decay=0.5 → 50% capacity."""
    c_low = cap.estimate(region="USA", universe="TOP3000", turnover=0.30)
    c_hi = cap.estimate(region="USA", universe="TOP3000", turnover=1.5)
    # decay = clip((1.5 - 0.5) / 2.0, 0, 0.5) = 0.5 → factor = 0.5
    assert c_hi == pytest.approx(c_low * 0.5, rel=1e-3)


def test_estimate_decay_clamps_at_50pct():
    """Even at infinite turnover, decay cap is 0.5 (capacity never fully collapses)."""
    c_huge = cap.estimate(region="USA", universe="TOP3000", turnover=10.0)
    expected = 2e7 * 0.10 * _math.sqrt(3000) * 0.5
    assert c_huge == pytest.approx(expected, rel=1e-3)


def test_estimate_negative_turnover_treated_as_zero():
    c = cap.estimate(region="USA", universe="TOP3000", turnover=-0.5)
    expected = 2e7 * 0.10 * _math.sqrt(3000) * 1.0
    assert c == pytest.approx(expected, rel=1e-3)


def test_estimate_unknown_region_uses_default():
    """Region 'MARS' miss → _default (1e7 ADV, 1000 stocks). sqrt scaling."""
    c = cap.estimate(region="MARS", universe="UNKNOWN", turnover=0.30)
    expected = 1e7 * 0.10 * _math.sqrt(1000)  # ≈ $31.6M
    assert c == pytest.approx(expected, rel=1e-3)


def test_estimate_known_region_unknown_universe_uses_default():
    c = cap.estimate(region="USA", universe="WEIRD_UNIVERSE", turnover=0.30)
    expected = 1e7 * 0.10 * _math.sqrt(1000)
    assert c == pytest.approx(expected, rel=1e-3)


def test_estimate_sqrt_no_longer_saturates_top_heavy_universe():
    """Sprint 2 F6 fix: USA TOP200 ($500M ADV) used to hit ~$10B (saturating
    the top normalize bucket). sqrt scaling keeps it < $1B → bucket 0.6 not 1.0."""
    c = cap.estimate(region="USA", universe="TOP200", turnover=0.20)
    assert c < 1.0e9  # no longer saturating
    assert cap.normalize(c) < 1.0


def test_estimate_chn_top3000_smaller_than_usa_top3000():
    c_usa = cap.estimate(region="USA", universe="TOP3000", turnover=0.30)
    c_chn = cap.estimate(region="CHN", universe="TOP3000", turnover=0.30)
    assert c_chn < c_usa


# ---------------------------------------------------------------------------
# normalize()
# ---------------------------------------------------------------------------

def test_normalize_log_buckets():
    # bucket [1e6, 1e7, 1e8, 1e9, 1e10] with 5 buckets → score steps 0.2 each
    buckets = [1e6, 1e7, 1e8, 1e9, 1e10]
    assert cap.normalize(5e5, buckets) == 0.0   # below first
    assert cap.normalize(5e6, buckets) == pytest.approx(0.2, rel=1e-6)  # 1e6..1e7
    assert cap.normalize(5e7, buckets) == pytest.approx(0.4, rel=1e-6)  # 1e7..1e8
    assert cap.normalize(5e8, buckets) == pytest.approx(0.6, rel=1e-6)
    assert cap.normalize(5e9, buckets) == pytest.approx(0.8, rel=1e-6)
    assert cap.normalize(5e10, buckets) == 1.0  # above last


def test_normalize_zero_and_negative():
    assert cap.normalize(0.0) == 0.0
    assert cap.normalize(-100.0) == 0.0


def test_normalize_uses_default_buckets_when_none():
    """When buckets=None, should pull from settings.CAPACITY_LOG_BUCKETS."""
    # Just verify it doesn't raise and returns a value in [0, 1]
    v = cap.normalize(1e8)
    assert 0.0 <= v <= 1.0


# ---------------------------------------------------------------------------
# estimate_from_alpha_dict
# ---------------------------------------------------------------------------

def test_estimate_from_alpha_dict_top_level_turnover():
    d = {"region": "USA", "universe": "TOP3000", "turnover": 0.30}
    c = cap.estimate_from_alpha_dict(d)
    assert c == pytest.approx(2e7 * 0.10 * _math.sqrt(3000), rel=1e-3)


def test_estimate_from_alpha_dict_brain_is_stats_path():
    """BRAIN simulate response has turnover nested under `is`."""
    d = {
        "region": "USA",
        "settings": {"universe": "TOP3000"},
        "is": {"turnover": 0.30},
    }
    c = cap.estimate_from_alpha_dict(d)
    assert c == pytest.approx(2e7 * 0.10 * _math.sqrt(3000), rel=1e-3)


def test_estimate_from_alpha_dict_metrics_path():
    d = {
        "region": "USA",
        "universe": "TOP3000",
        "metrics": {"turnover": 0.30},
    }
    c = cap.estimate_from_alpha_dict(d)
    assert c == pytest.approx(2e7 * 0.10 * _math.sqrt(3000), rel=1e-3)


def test_estimate_from_alpha_dict_missing_region_returns_zero():
    d = {"universe": "TOP3000", "turnover": 0.30}
    assert cap.estimate_from_alpha_dict(d) == 0.0


def test_estimate_from_alpha_dict_missing_universe_returns_zero():
    d = {"region": "USA", "turnover": 0.30}
    assert cap.estimate_from_alpha_dict(d) == 0.0


def test_estimate_from_alpha_dict_non_dict_returns_zero():
    assert cap.estimate_from_alpha_dict(None) == 0.0
    assert cap.estimate_from_alpha_dict("garbage") == 0.0
    assert cap.estimate_from_alpha_dict([]) == 0.0


# ---------------------------------------------------------------------------
# Soft-fail loading
# ---------------------------------------------------------------------------

def test_adv_table_missing_file_soft_falls(monkeypatch, tmp_path):
    bogus = tmp_path / "missing.json"
    monkeypatch.setattr(cap, "_ADV_JSON_PATH", bogus)
    cap.clear_adv_table_cache()
    # Should not raise; defaults take over
    c = cap.estimate(region="USA", universe="TOP3000", turnover=0.30)
    assert c == pytest.approx(1e7 * 0.10 * _math.sqrt(1000), rel=1e-3)  # default sqrt


def test_adv_table_corrupt_json_soft_falls(monkeypatch, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(cap, "_ADV_JSON_PATH", bad)
    cap.clear_adv_table_cache()
    c = cap.estimate(region="USA", universe="TOP3000", turnover=0.30)
    assert c == pytest.approx(1e7 * 0.10 * _math.sqrt(1000), rel=1e-3)


# ---------------------------------------------------------------------------
# Real JSON load — sanity
# ---------------------------------------------------------------------------

def test_real_json_loads_all_regions_present():
    """The shipped JSON contains every region we currently support."""
    table = cap._load_adv_table()
    expected = {"USA", "CHN", "JPN", "EUR", "HKG", "KOR", "TWN"}
    assert expected <= set(table.keys())


def test_real_json_usa_top3000_realistic_range():
    """Sanity (post sqrt-scaling fix): USA TOP3000 ~$100M, the physically
    sensible single-alpha capacity — NOT the $6B linear formula gave."""
    c = cap.estimate(region="USA", universe="TOP3000", turnover=0.20)
    assert 1.0e7 <= c <= 1.0e9
