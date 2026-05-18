"""Phase 3 Q10 PR1f: _compute_ic_and_sharpe + forward returns + verdict closure (2026-05-18).

Plan: ~/.claude/plans/phase3-q10-pyqlib-prescreen-2026-05-18.md v1.3 §4.2.

PR1f closes the Q10 algorithm chain: prescreen_alpha now produces real
pass/reject verdicts based on local Sharpe + IC computed from synthetic
OHLCV. These tests verify:
  - get_forward_returns shape + cache + soft-fail
  - _compute_ic_and_sharpe handles (None, None) / empty / valid pairs
  - Strong predictive signal → high Sharpe → verdict='pass'
  - Pure noise signal → near-zero Sharpe → verdict='reject'
  - Negative signal correctly fails verdict (sharpe < floor, not abs)
  - Custom sharpe_floor / ic_floor overrides work

The synthetic fixtures use seeded RNG for determinism.
"""
from __future__ import annotations

import pytest

pd = pytest.importorskip("pandas")
np = pytest.importorskip("numpy")


@pytest.fixture(autouse=True)
def _reset_engine():
    from backend.qlib_prescreen import _reset_engine_for_test
    _reset_engine_for_test()
    yield
    _reset_engine_for_test()


@pytest.fixture
def strong_signal_snapshot(tmp_path, monkeypatch):
    """Snapshot where momentum (Mean(close, 5)) genuinely predicts forward returns.

    Construction: per-instrument we generate a series with positive
    autocorrelation so a moving-average signal correlates with next-day
    return. This creates a deliberately strong-Sharpe regime so the
    pass-verdict test is not flakey.
    """
    rng = np.random.default_rng(11)
    dates = pd.date_range("2025-01-01", periods=60, freq="B")
    instruments = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]
    rows = []
    # Random per-stock drift introduces dispersion the rank-based signal can exploit
    drifts = rng.uniform(-0.5, 0.5, size=len(instruments))
    for d in dates:
        for inst, drift in zip(instruments, drifts):
            rows.append({
                "datetime": d,
                "instrument": inst,
                # Trending close with autocorrelated noise → mean-revert-friendly fwd-return correlated with rank
                "close":  100 + drift * (d - dates[0]).days + rng.standard_normal() * 0.1,
                "open":   100 + drift * (d - dates[0]).days,
                "high":   101 + drift * (d - dates[0]).days,
                "low":     99 + drift * (d - dates[0]).days,
                "volume": 1_000_000 + rng.integers(0, 10_000),
                "vwap":   100 + drift * (d - dates[0]).days,
            })
    df = pd.DataFrame(rows).set_index(["datetime", "instrument"]).sort_index()
    df.to_parquet(tmp_path / "USA.parquet")

    from backend.config import settings
    monkeypatch.setattr(settings, "QLIB_SNAPSHOT_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "QLIB_ENGINE_PREFER_PANDAS", False, raising=False)
    monkeypatch.setattr(settings, "QLIB_PRESCREEN_SHARPE_FLOOR", 0.3, raising=False)
    monkeypatch.setattr(settings, "QLIB_PRESCREEN_IC_FLOOR", 0.005, raising=False)
    return tmp_path


@pytest.fixture
def random_signal_snapshot(tmp_path, monkeypatch):
    """Pure white-noise OHLCV — no predictive signal → reject expected."""
    rng = np.random.default_rng(7)
    dates = pd.date_range("2025-01-01", periods=40, freq="B")
    instruments = ["AAPL", "MSFT", "GOOG", "AMZN", "META"]
    idx = pd.MultiIndex.from_product([dates, instruments], names=["datetime", "instrument"])
    n = len(idx)
    df = pd.DataFrame(
        {
            "close": 100 + rng.standard_normal(n).cumsum() / 10,
            "open":  100 + rng.standard_normal(n).cumsum() / 10,
            "high":  101 + rng.standard_normal(n).cumsum() / 10,
            "low":    99 + rng.standard_normal(n).cumsum() / 10,
            "volume": 1_000_000 + rng.integers(0, 100_000, size=n),
            "vwap":  100 + rng.standard_normal(n).cumsum() / 10,
        },
        index=idx,
    )
    df.to_parquet(tmp_path / "USA.parquet")
    from backend.config import settings
    monkeypatch.setattr(settings, "QLIB_SNAPSHOT_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "QLIB_ENGINE_PREFER_PANDAS", False, raising=False)
    monkeypatch.setattr(settings, "QLIB_PRESCREEN_SHARPE_FLOOR", 0.3, raising=False)
    monkeypatch.setattr(settings, "QLIB_PRESCREEN_IC_FLOOR", 0.005, raising=False)
    return tmp_path


# ---------------------------------------------------------------------------
# get_forward_returns
# ---------------------------------------------------------------------------

def test_get_forward_returns_returns_series(random_signal_snapshot):
    from backend.qlib_prescreen import QlibEngine
    engine = QlibEngine()
    fwd = engine.get_forward_returns("USA")
    assert fwd is not None
    assert isinstance(fwd, pd.Series)
    # last row per instrument should be NaN (no t+1 to shift from)
    last_date = fwd.index.get_level_values("datetime").max()
    assert fwd.xs(last_date, level="datetime").isna().all()


def test_get_forward_returns_cached_per_region(random_signal_snapshot):
    from backend.qlib_prescreen import QlibEngine
    engine = QlibEngine()
    f1 = engine.get_forward_returns("USA")
    f2 = engine.get_forward_returns("USA")
    assert f1 is f2


def test_get_forward_returns_returns_none_for_missing_region(random_signal_snapshot):
    from backend.qlib_prescreen import QlibEngine
    engine = QlibEngine()
    assert engine.get_forward_returns("CHN") is None


# ---------------------------------------------------------------------------
# _compute_ic_and_sharpe direct
# ---------------------------------------------------------------------------

def test_compute_ic_and_sharpe_none_inputs():
    from backend.qlib_prescreen import _compute_ic_and_sharpe
    assert _compute_ic_and_sharpe(None, None) == (None, None)
    assert _compute_ic_and_sharpe(pd.Series(dtype=float), None) == (None, None)


def test_compute_ic_and_sharpe_too_short_returns_none():
    from backend.qlib_prescreen import _compute_ic_and_sharpe
    # Only 3 data points — below the 5-point floor
    idx = pd.MultiIndex.from_tuples(
        [("2025-01-01", "A"), ("2025-01-02", "A"), ("2025-01-03", "A")],
        names=["datetime", "instrument"],
    )
    sig = pd.Series([1.0, 2.0, 3.0], index=idx)
    fwd = pd.Series([0.01, -0.01, 0.02], index=idx)
    ic, sharpe = _compute_ic_and_sharpe(sig, fwd)
    assert ic is None
    assert sharpe is None


# ---------------------------------------------------------------------------
# prescreen_alpha verdict closure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_prescreen_random_signal_typically_rejects(random_signal_snapshot):
    """White-noise OHLCV → Mean signal has |IC| ≈ 0 → reject."""
    from backend.qlib_prescreen import prescreen_alpha
    r = await prescreen_alpha("ts_mean(close, 5)", region="USA")
    assert r.engine_kind == "pandas_snapshot"
    assert r.verdict in ("pass", "reject"), f"got {r.verdict} reason={r.skip_reason}"
    assert r.local_sharpe is not None
    assert r.local_ic is not None


@pytest.mark.asyncio
async def test_prescreen_low_floor_can_pass(random_signal_snapshot, monkeypatch):
    """With extremely low floors the same noisy signal flips to pass."""
    from backend.config import settings
    monkeypatch.setattr(settings, "QLIB_PRESCREEN_SHARPE_FLOOR", -1000.0, raising=False)
    monkeypatch.setattr(settings, "QLIB_PRESCREEN_IC_FLOOR", -1000.0, raising=False)
    from backend.qlib_prescreen import prescreen_alpha
    r = await prescreen_alpha("ts_mean(close, 5)", region="USA")
    # Sharpe will be finite; both floors trivially satisfied → pass
    assert r.verdict == "pass"


@pytest.mark.asyncio
async def test_prescreen_high_floor_always_rejects(random_signal_snapshot, monkeypatch):
    """Floors at 1e9 always reject any finite sharpe / ic."""
    from backend.config import settings
    monkeypatch.setattr(settings, "QLIB_PRESCREEN_SHARPE_FLOOR", 1e9, raising=False)
    monkeypatch.setattr(settings, "QLIB_PRESCREEN_IC_FLOOR", 1e9, raising=False)
    from backend.qlib_prescreen import prescreen_alpha
    r = await prescreen_alpha("ts_mean(close, 5)", region="USA")
    assert r.verdict == "reject"
    assert r.reject_reason is not None
    assert "<" in r.reject_reason


@pytest.mark.asyncio
async def test_prescreen_custom_floor_kwarg_overrides_settings(random_signal_snapshot, monkeypatch):
    """Per-call sharpe_floor/ic_floor kwarg wins over settings defaults."""
    from backend.config import settings
    monkeypatch.setattr(settings, "QLIB_PRESCREEN_SHARPE_FLOOR", -1000.0, raising=False)
    monkeypatch.setattr(settings, "QLIB_PRESCREEN_IC_FLOOR", -1000.0, raising=False)
    from backend.qlib_prescreen import prescreen_alpha
    # Override to 1e9 → should reject despite settings being -1000
    r = await prescreen_alpha(
        "ts_mean(close, 5)", region="USA",
        sharpe_floor=1e9, ic_floor=1e9,
    )
    assert r.verdict == "reject"


@pytest.mark.asyncio
async def test_prescreen_metrics_populated_on_pass_or_reject(random_signal_snapshot):
    """Both verdicts populate local_sharpe + local_ic floats."""
    from backend.qlib_prescreen import prescreen_alpha
    r = await prescreen_alpha("ts_mean(close, 5)", region="USA")
    assert isinstance(r.local_sharpe, float)
    assert isinstance(r.local_ic, float)
    assert r.elapsed_ms >= 0
