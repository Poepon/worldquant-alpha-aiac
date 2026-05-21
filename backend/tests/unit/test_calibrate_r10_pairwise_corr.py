"""Sprint 2 PR3 — R10 pairwise-correlation calibration unit tests.

Covers pure-function helpers (compute_pair_stats + recommend_tau) on
synthetic PnL matrices. The DB / BRAIN fetch path is integration-tested
elsewhere (operator runs the script once per region).
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from scripts.calibrate_r10_pairwise_corr import (
    compute_pair_stats,
    recommend_tau,
)


def _make_corr_pnl(n_dates: int, base: pd.Series, noise: float, seed: int) -> pd.Series:
    """Return a PnL series correlated with ``base`` plus i.i.d. Gaussian noise."""
    rng = np.random.default_rng(seed)
    return base + pd.Series(rng.normal(0, noise, n_dates), index=base.index)


@pytest.fixture
def synthetic_intra_high_corr():
    """3 alphas in family F1 with high pairwise corr + 3 alphas in F2 also
    high-corr but independent from F1."""
    n = 250
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    rng = np.random.default_rng(0)
    base_f1 = pd.Series(rng.normal(100, 10, n).cumsum(), index=dates)
    base_f2 = pd.Series(rng.normal(50, 5, n).cumsum(), index=dates)

    series = {
        "a1": _make_corr_pnl(n, base_f1, noise=0.5, seed=1),
        "a2": _make_corr_pnl(n, base_f1, noise=0.5, seed=2),
        "a3": _make_corr_pnl(n, base_f1, noise=0.5, seed=3),
        "b1": _make_corr_pnl(n, base_f2, noise=0.5, seed=4),
        "b2": _make_corr_pnl(n, base_f2, noise=0.5, seed=5),
        "b3": _make_corr_pnl(n, base_f2, noise=0.5, seed=6),
    }
    pnl_matrix = pd.DataFrame(series)
    surviving_rows = [
        {"alpha_id": "a1", "family_signature": "F1"},
        {"alpha_id": "a2", "family_signature": "F1"},
        {"alpha_id": "a3", "family_signature": "F1"},
        {"alpha_id": "b1", "family_signature": "F2"},
        {"alpha_id": "b2", "family_signature": "F2"},
        {"alpha_id": "b3", "family_signature": "F2"},
    ]
    return pnl_matrix, surviving_rows


def test_compute_pair_stats_partitions_intra_vs_cross(synthetic_intra_high_corr):
    pnl_matrix, surviving = synthetic_intra_high_corr
    intra, cross, per_family = compute_pair_stats(pnl_matrix, surviving)

    # 2 families × C(3,2)=3 intra pairs each = 6 intra pairs
    assert len(intra) == 6
    # 3 × 3 cross-family pairs = 9
    assert len(cross) == 9
    # 2 family stats entries
    assert len(per_family) == 2
    assert {f.family_signature for f in per_family} == {"F1", "F2"}


def test_intra_corr_higher_than_cross(synthetic_intra_high_corr):
    """Same family should have higher mean corr than different families."""
    pnl_matrix, surviving = synthetic_intra_high_corr
    intra, cross, _ = compute_pair_stats(pnl_matrix, surviving)
    assert np.mean(intra) > np.mean(cross)


def test_empty_pnl_matrix_returns_empty():
    intra, cross, per_family = compute_pair_stats(pd.DataFrame(), [])
    assert intra == []
    assert cross == []
    assert per_family == []


def test_single_alpha_returns_no_pairs():
    n = 100
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    pnl_matrix = pd.DataFrame({"a1": np.arange(n, dtype=float)}, index=dates)
    surviving = [{"alpha_id": "a1", "family_signature": "F1"}]
    intra, cross, per_family = compute_pair_stats(pnl_matrix, surviving)
    assert intra == []
    assert cross == []
    # F1 has 1 alpha → 0 intra pairs → no family stats
    assert per_family == []


def test_recommend_tau_is_p95(synthetic_intra_high_corr):
    """Operator decision 2026-05-20: τ = intra-family p95 directly (was
    mean(p95, p99) which sat above p95 → barely actionable)."""
    pnl_matrix, surviving = synthetic_intra_high_corr
    intra, _, _ = compute_pair_stats(pnl_matrix, surviving)
    # Augment to ≥10 pairs (synthetic fixture has 6); duplicate
    intra_aug = intra * 3
    tau = recommend_tau(intra_aug)
    assert tau == pytest.approx(np.percentile(intra_aug, 95), rel=1e-6)


def test_recommend_tau_insufficient_returns_nan():
    assert math.isnan(recommend_tau([0.5, 0.6, 0.7]))  # <10 pairs
    assert math.isnan(recommend_tau([]))


def test_per_family_stats_pair_count_consistent(synthetic_intra_high_corr):
    """For each family, n_pairs should equal C(n_alphas, 2)."""
    pnl_matrix, surviving = synthetic_intra_high_corr
    _, _, per_family = compute_pair_stats(pnl_matrix, surviving)
    for f in per_family:
        # n_alphas == 3 in both families, C(3,2) = 3
        assert f.n_alphas == 3
        assert f.n_pairs == 3


def test_per_family_corr_in_unit_interval(synthetic_intra_high_corr):
    pnl_matrix, surviving = synthetic_intra_high_corr
    _, _, per_family = compute_pair_stats(pnl_matrix, surviving)
    for f in per_family:
        assert -1.0 <= f.p50_corr <= 1.0
        assert -1.0 <= f.p95_corr <= 1.0
        assert -1.0 <= f.p99_corr <= 1.0
        # p95 ≥ p50, p99 ≥ p95 (sorted percentile invariant)
        assert f.p95_corr >= f.p50_corr - 1e-9
        assert f.p99_corr >= f.p95_corr - 1e-9
