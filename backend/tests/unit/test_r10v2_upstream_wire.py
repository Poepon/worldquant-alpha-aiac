"""Tier A — R10-v2 upstream wire tests (2026-05-20).

Converts ENABLE_FAMILY_HARD_BAN from DOA (no producer for
state.r10v2_pnl_corr_matrix) to functional. Covers:
  - family_classifier.same_family_alpha_ids: returns only ids in
    families with ≥2 members; skips solo / FAIL / no-id / empty-expr
  - CorrelationService.compute_pairwise_corr_for_ids: <2 ids → None,
    BRAIN circuit open → None, <2 non-empty series → None, happy path
    returns a corr DataFrame indexed by alpha_id
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pandas as pd
import pytest


@dataclass
class _MockAlpha:
    alpha_id: Optional[str]
    expression: str
    metrics: Dict[str, Any] = field(default_factory=dict)
    quality_status: Optional[str] = "PENDING"


# ---------------------------------------------------------------------------
# same_family_alpha_ids
# ---------------------------------------------------------------------------

def test_same_family_returns_ids_with_two_plus_members():
    from backend.family_classifier import same_family_alpha_ids
    # a1, a2 share ts_rank skeleton + same pillar → family of 2
    # b1 solo (different op) → not returned
    alphas = [
        _MockAlpha("a1", "ts_rank(close, 60)", metrics={"pillar": "momentum"}),
        _MockAlpha("a2", "ts_rank(volume, 60)", metrics={"pillar": "momentum"}),
        _MockAlpha("b1", "rank(close)", metrics={"pillar": "value"}),
    ]
    out = same_family_alpha_ids(alphas)
    assert set(out) == {"a1", "a2"}


def test_same_family_skips_solo_family():
    from backend.family_classifier import same_family_alpha_ids
    alphas = [
        _MockAlpha("a1", "ts_rank(close, 60)", metrics={"pillar": "momentum"}),
        _MockAlpha("b1", "rank(close)", metrics={"pillar": "value"}),
    ]
    assert same_family_alpha_ids(alphas) == []


def test_same_family_different_pillar_not_grouped():
    """Same op skeleton but different pillar → different family → not grouped."""
    from backend.family_classifier import same_family_alpha_ids
    alphas = [
        _MockAlpha("a1", "ts_rank(close, 60)", metrics={"pillar": "momentum"}),
        _MockAlpha("a2", "ts_rank(close, 60)", metrics={"pillar": "mean_reversion"}),
    ]
    assert same_family_alpha_ids(alphas) == []


def test_same_family_skips_fail_status():
    from backend.family_classifier import same_family_alpha_ids
    alphas = [
        _MockAlpha("a1", "ts_rank(close, 60)", metrics={"pillar": "momentum"}),
        _MockAlpha("a2", "ts_rank(volume, 60)", metrics={"pillar": "momentum"}, quality_status="FAIL"),
    ]
    # a2 FAIL → only a1 left in family → solo → []
    assert same_family_alpha_ids(alphas) == []


def test_same_family_skips_no_alpha_id():
    from backend.family_classifier import same_family_alpha_ids
    alphas = [
        _MockAlpha("a1", "ts_rank(close, 60)", metrics={"pillar": "momentum"}),
        _MockAlpha(None, "ts_rank(volume, 60)", metrics={"pillar": "momentum"}),
    ]
    assert same_family_alpha_ids(alphas) == []


def test_same_family_empty_expression_skipped():
    from backend.family_classifier import same_family_alpha_ids
    alphas = [
        _MockAlpha("a1", "", metrics={"pillar": "momentum"}),
        _MockAlpha("a2", "", metrics={"pillar": "momentum"}),
    ]
    assert same_family_alpha_ids(alphas) == []


def test_same_family_empty_input():
    from backend.family_classifier import same_family_alpha_ids
    assert same_family_alpha_ids([]) == []


def test_same_family_three_member_family():
    from backend.family_classifier import same_family_alpha_ids
    alphas = [
        _MockAlpha(f"a{i}", f"ts_rank(field_{i}, 60)", metrics={"pillar": "momentum"})
        for i in range(3)
    ]
    out = same_family_alpha_ids(alphas)
    assert set(out) == {"a0", "a1", "a2"}


# ---------------------------------------------------------------------------
# CorrelationService.compute_pairwise_corr_for_ids
# ---------------------------------------------------------------------------

def _svc_with_series(series_map: Dict[str, Optional[pd.Series]]) -> Any:
    """Build a CorrelationService whose _fetch_pnl_series returns the
    mapped series per alpha_id."""
    from backend.services.correlation_service import CorrelationService
    svc = CorrelationService(brain=MagicMock())

    async def _fake_fetch(aid, max_attempts=2):
        s = series_map.get(aid)
        return s if s is not None else pd.Series(dtype="float64")

    svc._fetch_pnl_series = AsyncMock(side_effect=_fake_fetch)
    return svc


def _make_pnl_series(name: str, base: np.ndarray, noise: float, seed: int) -> pd.Series:
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2024-01-01", periods=len(base), freq="B")
    return pd.Series(base + rng.normal(0, noise, len(base)), index=dates, name=name)


@pytest.mark.asyncio
async def test_corr_matrix_fewer_than_two_ids_returns_none():
    svc = _svc_with_series({})
    out = await svc.compute_pairwise_corr_for_ids(["a1"])
    assert out is None
    out0 = await svc.compute_pairwise_corr_for_ids([])
    assert out0 is None


@pytest.mark.asyncio
async def test_corr_matrix_circuit_open_returns_none(monkeypatch):
    from backend.adapters.brain_adapter import BRAIN_AUTH_CIRCUIT
    # Force the circuit open
    monkeypatch.setattr(BRAIN_AUTH_CIRCUIT, "is_open", lambda: True)
    svc = _svc_with_series({"a1": None, "a2": None})
    out = await svc.compute_pairwise_corr_for_ids(["a1", "a2"])
    assert out is None


@pytest.mark.asyncio
async def test_corr_matrix_fewer_than_two_series_returns_none(monkeypatch):
    from backend.adapters.brain_adapter import BRAIN_AUTH_CIRCUIT
    monkeypatch.setattr(BRAIN_AUTH_CIRCUIT, "is_open", lambda: False)
    n = 120
    base = np.cumsum(np.ones(n))
    # Only a1 yields a series; a2 empty → <2 → None
    svc = _svc_with_series({
        "a1": _make_pnl_series("a1", base, 0.5, 1),
        "a2": None,
    })
    out = await svc.compute_pairwise_corr_for_ids(["a1", "a2"])
    assert out is None


@pytest.mark.asyncio
async def test_corr_matrix_happy_path_returns_dataframe(monkeypatch):
    from backend.adapters.brain_adapter import BRAIN_AUTH_CIRCUIT
    monkeypatch.setattr(BRAIN_AUTH_CIRCUIT, "is_open", lambda: False)
    n = 150
    base = np.cumsum(np.ones(n) * 10)
    svc = _svc_with_series({
        "a1": _make_pnl_series("a1", base, 0.5, 1),
        "a2": _make_pnl_series("a2", base, 0.5, 2),  # correlated with a1
        "a3": _make_pnl_series("a3", np.cumsum(np.ones(n) * 3), 0.5, 3),  # independent
    })
    out = await svc.compute_pairwise_corr_for_ids(["a1", "a2", "a3"])
    assert out is not None
    assert isinstance(out, pd.DataFrame)
    assert set(out.columns) == {"a1", "a2", "a3"}
    # a1↔a2 share the same base → higher corr than a1↔a3
    assert out.loc["a1", "a2"] > out.loc["a1", "a3"]


@pytest.mark.asyncio
async def test_corr_matrix_caps_at_max_alphas(monkeypatch):
    from backend.adapters.brain_adapter import BRAIN_AUTH_CIRCUIT
    monkeypatch.setattr(BRAIN_AUTH_CIRCUIT, "is_open", lambda: False)
    n = 120
    base = np.cumsum(np.ones(n))
    series_map = {
        f"a{i}": _make_pnl_series(f"a{i}", base, 0.5, i) for i in range(10)
    }
    svc = _svc_with_series(series_map)
    out = await svc.compute_pairwise_corr_for_ids(
        [f"a{i}" for i in range(10)], max_alphas=3,
    )
    # capped at 3 → matrix is 3×3
    assert out is not None
    assert out.shape[1] <= 3
