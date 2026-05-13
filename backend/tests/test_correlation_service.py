"""Unit tests for CorrelationService crisis-window stress test.

Tests run against an in-memory synthetic PnL cache — no BRAIN calls. The
service is constructed with a bare AsyncMock for BrainAdapter; only the
cache-only methods are exercised.

Covers:
  1. `_slice_returns_to_window` date math (inclusive endpoints, unknown
     window returns empty).
  2. `calc_self_corr_by_window`:
        - returns insufficient_data when overlap < MIN_OVERLAP_DAYS_PER_WINDOW
        - returns ok with sensible max_corr / counterpart_id when cache has data
        - returns empty_pool when cache file missing
  3. `compute_portfolio_matrix` shape, symmetry, diagonal=1.0.
  4. `crisis_stress_test` hotspot extraction with a planted two-cluster pair.
  5. Snapshot save/load roundtrip.
"""

from __future__ import annotations

import json
import pickle
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pandas as pd
import pytest

from backend.services import correlation_service as cs


# ---------------------------------------------------------------------------
# Synthetic data helpers
# ---------------------------------------------------------------------------

def _make_pnls(
    alpha_specs: dict,
    start: str = "2019-01-01",
    end: str = "2026-05-14",
    seed: int = 42,
) -> pd.DataFrame:
    """Build a synthetic wide PnL DataFrame.

    `alpha_specs` is {alpha_id: noise_seed_offset}. PnL is integrated random
    walk (so that pnl - pnl.shift(1) gives the daily returns we want).
    """
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, end)
    out = {}
    for aid, offset in alpha_specs.items():
        local_rng = np.random.default_rng(seed + offset)
        returns = local_rng.normal(0.0, 1.0, size=len(idx))
        out[aid] = pd.Series(np.cumsum(returns), index=idx)
    return pd.DataFrame(out)


def _make_correlated_pnls(
    n_alphas_a: int = 3,
    n_alphas_b: int = 3,
    start: str = "2019-01-01",
    end: str = "2026-05-14",
    seed: int = 7,
) -> pd.DataFrame:
    """Two clusters: alphas in cluster A share a return component (high
    pairwise corr); alphas in cluster B share a different component;
    cross-cluster corr is ~0.
    """
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, end)
    n = len(idx)

    factor_a = rng.normal(0, 1.0, size=n)
    factor_b = rng.normal(0, 1.0, size=n)

    out = {}
    for i in range(n_alphas_a):
        noise = rng.normal(0, 0.3, size=n)
        returns = factor_a + noise
        out[f"A{i:02d}"] = pd.Series(np.cumsum(returns), index=idx)
    for i in range(n_alphas_b):
        noise = rng.normal(0, 0.3, size=n)
        returns = factor_b + noise
        out[f"B{i:02d}"] = pd.Series(np.cumsum(returns), index=idx)
    return pd.DataFrame(out)


def _install_cache(tmp_path, region: str, pnls: pd.DataFrame, monkeypatch):
    """Redirect CACHE_DIR to tmp_path and drop a pickle there."""
    monkeypatch.setattr(cs, "CACHE_DIR", tmp_path)
    path = tmp_path / f"os_pnls_{region}.pkl"
    payload = {
        "alpha_ids": list(pnls.columns),
        "pnls": pnls,
        "saved_at": datetime.utcnow().isoformat(),
    }
    with path.open("wb") as f:
        pickle.dump(payload, f, pickle.HIGHEST_PROTOCOL)


def _make_service():
    brain = MagicMock()
    brain.get_alpha_pnl = AsyncMock()
    return cs.CorrelationService(brain=brain)


# ---------------------------------------------------------------------------
# 1. _slice_returns_to_window
# ---------------------------------------------------------------------------

def test_slice_returns_to_window_inclusive_endpoints():
    idx = pd.bdate_range("2020-02-01", "2020-05-31")
    df = pd.DataFrame({"x": np.arange(len(idx))}, index=idx)

    sliced = cs._slice_returns_to_window(df, "covid_2020")
    start, end = cs.CRISIS_WINDOWS["covid_2020"]
    assert sliced.index.min() >= pd.Timestamp(start)
    assert sliced.index.max() <= pd.Timestamp(end)
    # All trading days inside the window should be present (rough sanity).
    assert len(sliced) >= 40


def test_slice_returns_to_window_unknown_returns_empty():
    idx = pd.bdate_range("2020-01-01", "2020-12-31")
    df = pd.DataFrame({"x": np.arange(len(idx))}, index=idx)

    sliced = cs._slice_returns_to_window(df, "not_a_window")
    assert len(sliced) == 0


# ---------------------------------------------------------------------------
# 2. calc_self_corr_by_window
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_calc_self_corr_by_window_empty_pool(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "CACHE_DIR", tmp_path)
    svc = _make_service()
    result = await svc.calc_self_corr_by_window(
        alpha_id="X", region="USA", alpha_pnl_series=pd.Series(dtype="float64")
    )
    assert all(v["status"] == "empty_pool" for v in result.values())


@pytest.mark.asyncio
async def test_calc_self_corr_by_window_insufficient_data(tmp_path, monkeypatch):
    pnls = _make_pnls({"OS1": 0, "OS2": 1, "OS3": 2})
    _install_cache(tmp_path, "USA", pnls, monkeypatch)

    # Target alpha only has data for 5 days inside covid_2020 → insufficient.
    short_idx = pd.bdate_range("2020-04-25", "2020-04-30")
    target_pnl = pd.Series(np.cumsum(np.random.randn(len(short_idx))), index=short_idx)

    svc = _make_service()
    result = await svc.calc_self_corr_by_window(
        alpha_id="NEW", region="USA", alpha_pnl_series=target_pnl
    )
    assert result["covid_2020"]["status"] == "insufficient_data"


@pytest.mark.asyncio
async def test_calc_self_corr_by_window_finds_counterpart(tmp_path, monkeypatch):
    pnls = _make_correlated_pnls(n_alphas_a=4, n_alphas_b=4)
    _install_cache(tmp_path, "USA", pnls, monkeypatch)

    # Target is a perfect copy of A00 → max_corr in any window should be A00
    # with corr ≈ 1.0.
    target_pnl = pnls["A00"].copy()

    svc = _make_service()
    result = await svc.calc_self_corr_by_window(
        alpha_id="A_copy", region="USA", alpha_pnl_series=target_pnl
    )

    # covid_2020 has ~50 trading days inside the window, > 20 floor → ok.
    covid = result["covid_2020"]
    assert covid["status"] == "ok"
    assert covid["max_corr"] > 0.95
    assert covid["counterpart_id"] == "A00"


# ---------------------------------------------------------------------------
# 3. compute_portfolio_matrix
# ---------------------------------------------------------------------------

def test_compute_portfolio_matrix_shape_and_diagonal(tmp_path, monkeypatch):
    pnls = _make_correlated_pnls(n_alphas_a=3, n_alphas_b=3)
    _install_cache(tmp_path, "USA", pnls, monkeypatch)

    svc = _make_service()
    payload = svc.compute_portfolio_matrix(region="USA", window=None)

    assert payload["status"] == "ok"
    assert payload["n_alphas"] == 6
    mat = payload["matrix"]
    ids = payload["alpha_ids"]

    # Diagonal == 1.0
    for i in range(len(ids)):
        assert mat[i][i] == pytest.approx(1.0)

    # Symmetry
    for i in range(len(ids)):
        for j in range(len(ids)):
            assert mat[i][j] == pytest.approx(mat[j][i])


def test_compute_portfolio_matrix_within_window(tmp_path, monkeypatch):
    pnls = _make_correlated_pnls()
    _install_cache(tmp_path, "USA", pnls, monkeypatch)

    svc = _make_service()
    payload = svc.compute_portfolio_matrix(region="USA", window="covid_2020")

    assert payload["status"] == "ok"
    assert payload["window"] == "covid_2020"
    # covid_2020 has ~50 trading days, > 20 floor → all alphas should make it.
    assert payload["n_alphas"] == 6


def test_compute_portfolio_matrix_unknown_window(tmp_path, monkeypatch):
    pnls = _make_correlated_pnls()
    _install_cache(tmp_path, "USA", pnls, monkeypatch)

    svc = _make_service()
    payload = svc.compute_portfolio_matrix(region="USA", window="not_a_window")
    assert payload["status"] == "missing_window"


# ---------------------------------------------------------------------------
# 4. crisis_stress_test
# ---------------------------------------------------------------------------

def test_crisis_stress_test_detects_planted_hotspots(tmp_path, monkeypatch):
    # Two highly-correlated clusters so off-diagonal max should be high
    # (> hotspot threshold), and cross-cluster pairs should be near zero.
    pnls = _make_correlated_pnls(n_alphas_a=3, n_alphas_b=3)
    _install_cache(tmp_path, "USA", pnls, monkeypatch)

    svc = _make_service()
    payload = svc.crisis_stress_test(region="USA", hotspot_threshold=0.6)

    assert payload["status"] == "ok"
    assert payload["baseline"]["status"] == "ok"

    # Hotspots are all within-cluster pairs.
    hotspots = payload["baseline"]["hotspots"]
    assert len(hotspots) > 0
    for h in hotspots:
        same_cluster = (h["a"][0] == h["b"][0])  # A.. with A.. or B.. with B..
        assert same_cluster, f"unexpected cross-cluster hotspot: {h}"

    # Per-window summaries should all carry status ok (windows have enough
    # data in our synthetic 2019→2026 history).
    for wname in cs.CRISIS_WINDOWS:
        assert payload["windows"][wname]["status"] == "ok"


def test_crisis_stress_test_empty_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "CACHE_DIR", tmp_path)
    svc = _make_service()
    payload = svc.crisis_stress_test(region="USA")
    assert payload["status"] == "empty"


# ---------------------------------------------------------------------------
# 5. Snapshot persistence roundtrip
# ---------------------------------------------------------------------------

def test_save_and_load_crisis_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "CACHE_DIR", tmp_path)
    svc = _make_service()
    payload = {
        "status": "ok",
        "region": "USA",
        "baseline": {"status": "ok", "n_alphas": 5},
        "windows": {"covid_2020": {"status": "ok", "n_alphas": 5}},
    }
    path = svc.save_crisis_snapshot("USA", payload)
    assert path.exists()

    loaded = svc.load_crisis_snapshot("USA")
    assert loaded == payload


def test_load_crisis_snapshot_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "CACHE_DIR", tmp_path)
    svc = _make_service()
    assert svc.load_crisis_snapshot("USA") is None
