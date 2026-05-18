"""Phase 3 Q10 PR1e: full prescreen_alpha pipeline with snapshot loader (2026-05-18).

Plan: ~/.claude/plans/phase3-q10-pyqlib-prescreen-2026-05-18.md v1.3 §3.2 + §3.3.

PR1e wires QlibEngine.evaluate to the pandas evaluator via a snapshot
loader. These tests verify the full skip → pass/reject transition by
writing a synthetic parquet snapshot into a tmp_path, then calling
prescreen_alpha end-to-end and asserting:
  - probe upgrades to 'pandas_snapshot' when snapshot file exists
  - QLIB_ENGINE_PREFER_PANDAS=True forces tier even without snapshot file
  - evaluate produces non-None Series via evaluate_pandas
  - prescreen_alpha skip_reason transitions from engine_disabled to
    empty_series / metrics_nan / reject / pass based on synthetic returns
  - missing snapshot file → snapshot loader returns None → skip:empty_series
  - all soft-fail paths preserved
"""
from __future__ import annotations

import os

import pytest

pd = pytest.importorskip("pandas")
np = pytest.importorskip("numpy")


@pytest.fixture(autouse=True)
def _reset_engine():
    """Force a fresh QlibEngine probe for every test."""
    from backend.qlib_prescreen import _reset_engine_for_test
    _reset_engine_for_test()
    yield
    _reset_engine_for_test()


@pytest.fixture
def synthetic_snapshot(tmp_path, monkeypatch):
    """Write a synthetic USA.parquet snapshot to tmp_path and point settings at it."""
    rng = np.random.default_rng(7)
    dates = pd.date_range("2025-01-01", periods=30, freq="B")
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
    path = tmp_path / "USA.parquet"
    df.to_parquet(path)

    from backend.config import settings
    monkeypatch.setattr(settings, "QLIB_SNAPSHOT_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "QLIB_ENGINE_PREFER_PANDAS", False, raising=False)
    return path


# ---------------------------------------------------------------------------
# Probe upgrade
# ---------------------------------------------------------------------------

def test_probe_upgrades_to_pandas_snapshot_when_file_exists(synthetic_snapshot):
    from backend.qlib_prescreen import QlibEngine
    engine = QlibEngine()
    assert engine.kind == "pandas_snapshot"


def test_probe_force_pandas_overrides_missing_snapshot(tmp_path, monkeypatch):
    """QLIB_ENGINE_PREFER_PANDAS=True forces tier-3 even with no snapshot file.

    This is the dev/CI escape hatch — engine.kind='pandas_snapshot' but
    evaluate() returns None because _load_snapshot finds no file.
    """
    from backend.config import settings
    monkeypatch.setattr(settings, "QLIB_SNAPSHOT_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "QLIB_ENGINE_PREFER_PANDAS", True, raising=False)
    from backend.qlib_prescreen import QlibEngine
    engine = QlibEngine()
    assert engine.kind == "pandas_snapshot"


def test_probe_falls_back_to_disabled_with_no_snapshot_dir(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "QLIB_SNAPSHOT_DIR", "/definitely/does/not/exist", raising=False)
    monkeypatch.setattr(settings, "QLIB_ENGINE_PREFER_PANDAS", False, raising=False)
    from backend.qlib_prescreen import QlibEngine
    engine = QlibEngine()
    assert engine.kind == "disabled"


# ---------------------------------------------------------------------------
# Snapshot loader
# ---------------------------------------------------------------------------

def test_snapshot_loader_returns_dataframe(synthetic_snapshot):
    from backend.qlib_prescreen import QlibEngine
    engine = QlibEngine()
    df = engine._load_snapshot("USA")
    assert df is not None
    assert len(df) == 30 * 5  # 30 days x 5 instruments
    assert "close" in df.columns


def test_snapshot_loader_caches_per_region(synthetic_snapshot):
    """Second call for the same region returns the same object (cached)."""
    from backend.qlib_prescreen import QlibEngine
    engine = QlibEngine()
    df1 = engine._load_snapshot("USA")
    df2 = engine._load_snapshot("USA")
    assert df1 is df2  # cache hit returns same object


def test_snapshot_loader_returns_none_for_missing_region(synthetic_snapshot):
    """Missing CHN.parquet → loader returns None (engine soft-falls)."""
    from backend.qlib_prescreen import QlibEngine
    engine = QlibEngine()
    df = engine._load_snapshot("CHN")
    assert df is None


# ---------------------------------------------------------------------------
# Engine.evaluate end-to-end
# ---------------------------------------------------------------------------

def test_engine_evaluate_runs_pandas_when_snapshot_present(synthetic_snapshot):
    from backend.qlib_prescreen import QlibEngine
    engine = QlibEngine()
    series = engine.evaluate("Mean($close, 5)", "USA", "TOP3000")
    assert series is not None
    assert len(series) == 30 * 5


def test_engine_evaluate_returns_none_when_snapshot_missing(synthetic_snapshot):
    from backend.qlib_prescreen import QlibEngine
    engine = QlibEngine()
    # Region without a snapshot file
    assert engine.evaluate("Mean($close, 5)", "CHN", "TOP3000") is None


def test_engine_evaluate_returns_none_when_disabled(monkeypatch):
    """Disabled engine ignores evaluate calls entirely."""
    from backend.config import settings
    monkeypatch.setattr(settings, "QLIB_SNAPSHOT_DIR", "/no/dir", raising=False)
    monkeypatch.setattr(settings, "QLIB_ENGINE_PREFER_PANDAS", False, raising=False)
    from backend.qlib_prescreen import QlibEngine
    engine = QlibEngine()
    assert engine.kind == "disabled"
    assert engine.evaluate("Mean($close, 5)", "USA", "TOP3000") is None


# ---------------------------------------------------------------------------
# prescreen_alpha end-to-end transitions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_prescreen_alpha_engine_disabled_still_skips(monkeypatch):
    """Without a snapshot the engine probes disabled and prescreen returns skip."""
    from backend.config import settings
    monkeypatch.setattr(settings, "QLIB_SNAPSHOT_DIR", "/no/dir", raising=False)
    monkeypatch.setattr(settings, "QLIB_ENGINE_PREFER_PANDAS", False, raising=False)
    from backend.qlib_prescreen import prescreen_alpha
    r = await prescreen_alpha("ts_mean(close, 20)")
    assert r.verdict == "skip"
    assert r.skip_reason == "engine_disabled"
    assert r.engine_kind == "disabled"


@pytest.mark.asyncio
async def test_prescreen_alpha_pandas_tier_reaches_evaluate(synthetic_snapshot):
    """Snapshot present → engine='pandas_snapshot' → evaluate returns Series →
    PR1f-wired _compute_ic_and_sharpe produces real metrics → verdict ∈
    {pass, reject} on synthetic random OHLCV.

    The synthetic OHLCV is essentially white-noise so a vanilla Mean signal
    has no real predictive power → verdict='reject' (low |IC| / low Sharpe
    below default floors 0.3/0.005). The KEY assertion is that we've moved
    past skip — Q10 algorithm chain is now closed end-to-end.
    """
    from backend.qlib_prescreen import prescreen_alpha
    r = await prescreen_alpha("ts_mean(close, 5)", region="USA")
    assert r.engine_kind == "pandas_snapshot"
    assert r.qlib_expression == "Mean($close, 5)"
    # Q10 algorithm chain closure — verdict is no longer 'skip' with metrics_nan
    assert r.verdict in ("pass", "reject")
    assert r.skip_reason is None
    assert r.local_sharpe is not None
    assert r.local_ic is not None
    assert r.translation_error is None


@pytest.mark.asyncio
async def test_prescreen_alpha_untranslatable_short_circuits_before_engine(synthetic_snapshot):
    """Untranslatable expression skips before engine probe even with snapshot up."""
    from backend.qlib_prescreen import prescreen_alpha
    r = await prescreen_alpha("group_neutralize(close, sector)", region="USA")
    assert r.verdict == "skip"
    assert r.skip_reason == "untranslatable"
    assert r.qlib_expression is None


@pytest.mark.asyncio
async def test_prescreen_alpha_unknown_region_skips_empty_series(synthetic_snapshot):
    """Engine ready, but request region with no snapshot → empty_series skip."""
    from backend.qlib_prescreen import prescreen_alpha
    r = await prescreen_alpha("ts_mean(close, 5)", region="CHN")
    # engine.kind='pandas_snapshot' but _load_snapshot('CHN') returns None
    assert r.engine_kind == "pandas_snapshot"
    assert r.verdict == "skip"
    assert r.skip_reason == "empty_series"
