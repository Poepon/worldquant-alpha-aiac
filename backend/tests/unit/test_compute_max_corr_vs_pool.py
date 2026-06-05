"""CorrelationService.compute_max_corr_vs_pool — the candidate-vs-pool
orthogonality signal for orthogonality-steered exploration Phase A (plan v4,
2026-06-05).

Unlike ``get_with_fallback`` (whose LOCAL path needs the candidate ALREADY in
the on-disk cache → ``UNKNOWN`` for a freshly-mined alpha; its BRAIN SELF path
is async-PENDING), this method FETCHES the candidate's PnL post-sim and
correlates it against the cached submitted/OS pool. That is what lets the A/B
record a DENSE ``orthogonality_score = 1 - max|corr|`` for fresh candidates —
the shadow run recorded 0 scores precisely because the old 1-self_corr path was
UNKNOWN for every fresh alpha.

Construction trick: the service diffs PnL → daily returns, so we feed
``cumsum(returns)`` as PnL and the diff recovers our designed returns. cos/sin
over whole periods are exactly orthogonal + equal-variance, giving known
Pearson correlations (0.6a+0.8b correlates 0.6 with a).
"""
import numpy as np
import pandas as pd
import pytest

from backend.services.correlation_service import CorrelationService


def _idx(n=90):
    return pd.bdate_range("2023-01-02", periods=n)


def _pnl_from_returns(returns, index):
    """Cumulative PnL whose daily diff recovers ``returns`` (service diffs PnL)."""
    return pd.Series(np.cumsum(np.asarray(returns, dtype="float64")), index=index)


def _basis(n=90, k=3):
    """Orthogonal cos/sin pair over k whole periods (exactly ⊥, equal variance)."""
    t = np.arange(n)
    return np.cos(2 * np.pi * k * t / n), np.sin(2 * np.pi * k * t / n)


def _make_svc(cand_pnl, pool_pnls):
    svc = CorrelationService(brain=object())  # brain unused — fetch/load patched

    async def _fake_fetch(alpha_id, max_attempts=3):
        return cand_pnl if cand_pnl is not None else pd.Series(dtype="float64")

    svc._fetch_pnl_series = _fake_fetch
    svc._load_cache = lambda region: {"pnls": pool_pnls}
    return svc


@pytest.mark.asyncio
async def test_perfect_corr_returns_one():
    idx = _idx()
    a, _ = _basis()
    svc = _make_svc(_pnl_from_returns(a, idx),
                    pd.DataFrame({"submitted_1": _pnl_from_returns(a, idx)}))
    mc = await svc.compute_max_corr_vs_pool("fresh-1", "USA")
    assert mc is not None and mc == pytest.approx(1.0, abs=1e-6)


@pytest.mark.asyncio
async def test_picks_max_abs_corr_anti_correlated():
    """An anti-correlated pool member (corr -1) must register as |corr|=1 —
    BRAIN's self-corr gate is on the magnitude, sign-flipped duplicates count."""
    idx = _idx()
    a, b = _basis()
    pool = pd.DataFrame({
        "weak": _pnl_from_returns(b, idx),    # ~orthogonal to a
        "anti": _pnl_from_returns(-a, idx),   # corr -1 → abs 1
    })
    svc = _make_svc(_pnl_from_returns(a, idx), pool)
    mc = await svc.compute_max_corr_vs_pool("fresh-2", "USA")
    assert mc == pytest.approx(1.0, abs=1e-6)


@pytest.mark.asyncio
async def test_partial_corr_value():
    idx = _idx()
    a, b = _basis()
    # 0.6a + 0.8b correlates 0.6 with a (a⊥b, equal variance over whole periods)
    pool = pd.DataFrame({"mix": _pnl_from_returns(0.6 * a + 0.8 * b, idx)})
    svc = _make_svc(_pnl_from_returns(a, idx), pool)
    mc = await svc.compute_max_corr_vs_pool("fresh-3", "USA")
    assert mc == pytest.approx(0.6, abs=0.05)


@pytest.mark.asyncio
async def test_none_when_candidate_pnl_empty():
    pool = pd.DataFrame({"x": _pnl_from_returns(_basis()[0], _idx())})
    svc = _make_svc(None, pool)
    assert await svc.compute_max_corr_vs_pool("fresh-4", "USA") is None


@pytest.mark.asyncio
async def test_none_when_pool_empty():
    cand = _pnl_from_returns(_basis()[0], _idx())
    svc = _make_svc(cand, pd.DataFrame())
    assert await svc.compute_max_corr_vs_pool("fresh-5", "USA") is None


@pytest.mark.asyncio
async def test_none_when_insufficient_overlap():
    idx = _idx(30)  # 30 days → 29 returns < default min_overlap_days=60
    a, _ = _basis(30)
    svc = _make_svc(_pnl_from_returns(a, idx),
                    pd.DataFrame({"x": _pnl_from_returns(a, idx)}))
    assert await svc.compute_max_corr_vs_pool("fresh-6", "USA") is None


@pytest.mark.asyncio
async def test_drops_self_column():
    """The candidate must never correlate against its own row in the pool."""
    idx = _idx()
    a, b = _basis()
    pool = pd.DataFrame({
        "fresh-7": _pnl_from_returns(a, idx),   # identical — but IS the candidate
        "other": _pnl_from_returns(b, idx),     # ~orthogonal
    })
    svc = _make_svc(_pnl_from_returns(a, idx), pool)
    mc = await svc.compute_max_corr_vs_pool("fresh-7", "USA")
    # self column dropped → only `other` (~0 corr) remains, not the 1.0 self-match
    assert mc is not None and mc < 0.3
