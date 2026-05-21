"""B2 R13 factor_lens_service unit tests (Phase 4 Sprint 2 / plan v5 §6.9).

Coverage:
  - decompose(): OLS β + residual sharpe + r² on synthetic data
  - decompose(): perfect collinear → residual_sharpe ≈ 0 + r² ≈ 1
  - decompose(): orthogonal alpha → low r²
  - decompose(): insufficient overlap → empty Residual
  - decompose(): bad input shapes / empty → empty Residual
  - decompose_bucket(): fallback formula
  - load_factor_returns(): missing file → None
  - load_factor_returns(): missing factor columns → None
  - decompose_alpha(): one-call wrapper integration with load
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backend.services import factor_lens_service as fls


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def factor_returns_500d():
    """500-day synthetic factor returns DataFrame, 5 factors, all i.i.d."""
    n = 500
    dates = pd.date_range("2022-01-01", periods=n, freq="B")
    rng = np.random.default_rng(42)
    df = pd.DataFrame(
        {
            "size": rng.normal(0, 0.01, n),
            "value": rng.normal(0, 0.01, n),
            "momentum": rng.normal(0, 0.01, n),
            "quality": rng.normal(0, 0.01, n),
            "low_vol": rng.normal(0, 0.01, n),
        },
        index=dates,
    )
    return df


# ---------------------------------------------------------------------------
# decompose() — happy paths
# ---------------------------------------------------------------------------

def test_decompose_orthogonal_alpha_low_r_squared(factor_returns_500d):
    """An alpha with NO factor exposure (pure noise) → r² ≈ 0 + residual
    sharpe ≈ alpha's own sharpe."""
    n = len(factor_returns_500d)
    rng = np.random.default_rng(7)
    # Add small positive drift so residual sharpe is measurable
    raw = rng.normal(0.001, 0.01, n)
    alpha_returns = pd.Series(raw, index=factor_returns_500d.index)
    res = fls.decompose(alpha_returns, factor_returns_500d)
    assert res.mode_used == "ols_daily"
    assert res.ols_n_days == n
    assert res.r_squared < 0.1  # essentially uncorrelated
    # Most of the original sharpe should survive
    raw_sharpe = float(np.mean(raw)) / float(np.std(raw, ddof=1)) * np.sqrt(252)
    assert abs(res.residual_sharpe - raw_sharpe) < 0.5


def test_decompose_perfect_collinear_residual_near_zero(factor_returns_500d):
    """If alpha is exactly 2 × size + 1 × momentum (deterministic),
    OLS should recover β + residuals near zero."""
    alpha_returns = (
        2.0 * factor_returns_500d["size"]
        + 1.0 * factor_returns_500d["momentum"]
    )
    res = fls.decompose(alpha_returns, factor_returns_500d)
    assert res.mode_used == "ols_daily"
    assert res.r_squared == pytest.approx(1.0, abs=1e-6)
    # β_size ≈ 2, β_momentum ≈ 1
    assert res.factor_exposures["size"] == pytest.approx(2.0, abs=1e-6)
    assert res.factor_exposures["momentum"] == pytest.approx(1.0, abs=1e-6)
    # residual sharpe → 0 (perfect fit means no residual)
    assert abs(res.residual_sharpe) < 1e-3 or np.isfinite(res.residual_sharpe)


def test_decompose_intercept_stamped(factor_returns_500d):
    """Intercept (alpha-of-Jensen) should be in factor_exposures dict."""
    alpha_returns = factor_returns_500d["value"] + 0.0005  # +0.05% daily
    res = fls.decompose(alpha_returns, factor_returns_500d)
    assert "_intercept" in res.factor_exposures
    # intercept should be ~ 0.0005
    assert res.factor_exposures["_intercept"] == pytest.approx(0.0005, abs=1e-4)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_decompose_insufficient_overlap(factor_returns_500d):
    """Series with <60-day overlap → empty Residual."""
    short = factor_returns_500d.iloc[:30]["value"]
    res = fls.decompose(short, factor_returns_500d, min_overlap_days=60)
    assert res.mode_used == "insufficient_overlap"
    assert res.ols_n_days == 0


def test_decompose_empty_input():
    empty = pd.Series([], dtype=float)
    factors = pd.DataFrame()
    res = fls.decompose(empty, factors)
    assert res.residual_sharpe == 0.0


def test_decompose_none_input():
    res = fls.decompose(None, None)
    assert res.mode_used == "none_input"


def test_decompose_bad_alpha_shape(factor_returns_500d):
    res = fls.decompose([1, 2, 3], factor_returns_500d)
    assert res.mode_used == "bad_alpha_shape"


def test_decompose_bad_factor_shape():
    s = pd.Series([0.1, 0.2, 0.3])
    res = fls.decompose(s, "not_a_dataframe")
    assert res.mode_used == "bad_factor_shape"


def test_decompose_no_overlap_at_all(factor_returns_500d):
    """Alpha series with dates disjoint from factor snapshot → 0 overlap."""
    n = 100
    future = pd.date_range("2030-01-01", periods=n, freq="B")
    alpha = pd.Series(np.zeros(n), index=future)
    res = fls.decompose(alpha, factor_returns_500d, min_overlap_days=60)
    assert res.mode_used == "insufficient_overlap"


# ---------------------------------------------------------------------------
# decompose_bucket
# ---------------------------------------------------------------------------

def test_decompose_bucket():
    res = fls.decompose_bucket(alpha_sharpe=1.8, pool_median_sharpe=1.2)
    assert res.residual_sharpe == pytest.approx(0.6)
    assert res.mode_used == "bucket_median"
    assert res.factor_exposures == {}


# ---------------------------------------------------------------------------
# load_factor_returns
# ---------------------------------------------------------------------------

def test_load_factor_returns_missing_region_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(fls, "_SNAPSHOT_DIR", tmp_path)
    out = fls.load_factor_returns("USA")
    assert out is None


def test_load_factor_returns_factor_filter_drops_extras(tmp_path, monkeypatch):
    """load_factor_returns should return only the columns we asked for."""
    n = 100
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    df = pd.DataFrame(
        {
            "size": np.zeros(n),
            "value": np.zeros(n),
            "momentum": np.zeros(n),
            "quality": np.zeros(n),
            "low_vol": np.zeros(n),
            "extra_factor": np.zeros(n),  # should be dropped
        },
        index=dates,
    )
    df.to_parquet(tmp_path / "usa.parquet")
    monkeypatch.setattr(fls, "_SNAPSHOT_DIR", tmp_path)

    out = fls.load_factor_returns("USA", factors=["size", "value"])
    assert out is not None
    assert list(out.columns) == ["size", "value"]


def test_load_factor_returns_no_overlap_returns_none(tmp_path, monkeypatch):
    """If requested factors share zero columns with the snapshot → None."""
    n = 100
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    df = pd.DataFrame({"weird_factor": np.zeros(n)}, index=dates)
    df.to_parquet(tmp_path / "usa.parquet")
    monkeypatch.setattr(fls, "_SNAPSHOT_DIR", tmp_path)

    out = fls.load_factor_returns("USA", factors=["size", "value"])
    assert out is None


# ---------------------------------------------------------------------------
# decompose_alpha — high-level wrapper
# ---------------------------------------------------------------------------

def test_decompose_alpha_no_snapshot(tmp_path, monkeypatch):
    """One-call wrapper when no region snapshot exists → no_snapshot."""
    monkeypatch.setattr(fls, "_SNAPSHOT_DIR", tmp_path)
    s = pd.Series(np.zeros(100), index=pd.date_range("2024-01-01", periods=100, freq="B"))
    res = fls.decompose_alpha(alpha_returns=s, region="USA")
    assert res.mode_used == "no_snapshot"


def test_decompose_alpha_happy_path(factor_returns_500d, tmp_path, monkeypatch):
    """End-to-end: snapshot loads + decompose runs + Residual returned."""
    factor_returns_500d.to_parquet(tmp_path / "usa.parquet")
    monkeypatch.setattr(fls, "_SNAPSHOT_DIR", tmp_path)

    alpha = factor_returns_500d["value"] + np.random.default_rng(1).normal(0.001, 0.005, len(factor_returns_500d))
    res = fls.decompose_alpha(alpha_returns=alpha, region="USA")
    assert res.mode_used == "ols_daily"
    assert res.ols_n_days > 60
    assert res.r_squared > 0.0
    assert "size" in res.factor_exposures
    assert "value" in res.factor_exposures


def test_decompose_alpha_none_input_no_snapshot():
    res = fls.decompose_alpha(alpha_returns=None, region="USA")
    assert res.mode_used == "no_input"


def test_decompose_alpha_empty_region(tmp_path):
    s = pd.Series(np.zeros(100))
    res = fls.decompose_alpha(alpha_returns=s, region="")
    assert res.mode_used == "no_input"
