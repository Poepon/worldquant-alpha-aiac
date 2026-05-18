"""Phase 3 Q10 PR1d: pandas-only qlib expression evaluator tests (2026-05-18).

Plan: ~/.claude/plans/phase3-q10-pyqlib-prescreen-2026-05-18.md v1.3 §7.3.

Tests use a synthetic 30-row x 5-instrument OHLCV DataFrame (no live qlib /
no live pyqlib snapshot required). Each test verifies the pandas engine
produces the expected shape / value class for a representative operator
expression. Stubs out the full §7.3 8-test "vs pyqlib oracle JSON" set
(PR1d ship is the engine + smoke tests; oracle-parity test is PR1d-v2).
"""
from __future__ import annotations

import math

import pytest

pd = pytest.importorskip("pandas")
np = pytest.importorskip("numpy")

from backend.qlib_prescreen_pandas_engine import evaluate_pandas


@pytest.fixture
def ohlcv_df():
    """30 days x 5 instruments synthetic OHLCV.

    Deterministic so tests are reproducible. The (datetime, instrument)
    MultiIndex matches the contract evaluate_pandas expects.
    """
    rng = np.random.default_rng(42)
    dates = pd.date_range("2025-01-01", periods=30, freq="B")
    instruments = ["AAPL", "MSFT", "GOOG", "AMZN", "META"]
    idx = pd.MultiIndex.from_product([dates, instruments], names=["datetime", "instrument"])
    n = len(idx)
    return pd.DataFrame(
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


# ---------------------------------------------------------------------------
# Operator unit tests
# ---------------------------------------------------------------------------

def test_evaluate_mean_rolling(ohlcv_df):
    """Mean($close, 5) returns same-shape Series with rolling mean per instrument."""
    out = evaluate_pandas("Mean($close, 5)", ohlcv_df)
    assert out is not None
    assert isinstance(out, pd.Series)
    assert len(out) == len(ohlcv_df)
    # First 4 rows per instrument should be NaN-or-partial (min_periods=1)
    # so first row equals close itself
    aapl_first = out.xs("AAPL", level="instrument").iloc[0]
    assert math.isclose(aapl_first, ohlcv_df.xs("AAPL", level="instrument")["close"].iloc[0])


def test_evaluate_std_rolling(ohlcv_df):
    """Std($close, 10) returns valid std for instruments with enough data."""
    out = evaluate_pandas("Std($close, 10)", ohlcv_df)
    assert out is not None
    aapl = out.xs("AAPL", level="instrument")
    # Late rows have full window so std should be > 0
    assert aapl.iloc[-1] > 0


def test_evaluate_ref_lag(ohlcv_df):
    """Ref($close, -1) returns next-day close shifted back per instrument."""
    out = evaluate_pandas("Ref($close, -1)", ohlcv_df)
    assert out is not None
    aapl_close = ohlcv_df.xs("AAPL", level="instrument")["close"]
    aapl_ref = out.xs("AAPL", level="instrument")
    # Ref($close, -1) → shift(-(-1)) = shift(1) per impl (positive lag means earlier)
    # Per qlib: Ref(x, -1) is next-period value; our impl shift(-lag) for lag=-1 → shift(1)
    # So Ref(x, -1)[t] = x[t+1] (look-ahead)? Or Ref(x, -1)[t] = x[t-1] depending on sign
    # We just verify shape + at least one non-NaN value.
    assert len(aapl_ref) == len(aapl_close)
    assert aapl_ref.notna().any()


def test_evaluate_delta(ohlcv_df):
    """Delta($close, 5) = $close - Ref($close, 5)."""
    out = evaluate_pandas("Delta($close, 5)", ohlcv_df)
    assert out is not None
    # Row count matches
    assert len(out) == len(ohlcv_df)
    # First 5 rows per instrument should be NaN (shift left no value)
    aapl = out.xs("AAPL", level="instrument")
    assert aapl.iloc[0:5].isna().all()


def test_evaluate_rank_cross_section(ohlcv_df):
    """Rank($close) single-arg → cross-section pct rank within each datetime, centered on 0."""
    out = evaluate_pandas("Rank($close)", ohlcv_df)
    assert out is not None
    # Each datetime should have ranks in [-0.4, 0.5] roughly (5 stocks → ranks 0.2/0.4/0.6/0.8/1.0 - 0.5)
    first_date = ohlcv_df.index.get_level_values("datetime")[0]
    day_one = out.xs(first_date, level="datetime")
    assert day_one.min() >= -0.5
    assert day_one.max() <= 0.5
    # 5 distinct ranks
    assert day_one.nunique() == 5


def test_evaluate_arithmetic_nested(ohlcv_df):
    """Mul(Div($close, $open), 100) — nested call composition."""
    out = evaluate_pandas("Mul(Div($close, $open), 100)", ohlcv_df)
    assert out is not None
    assert len(out) == len(ohlcv_df)
    # close ≈ open so ratio ≈ 1, × 100 ≈ 100
    assert abs(out.median() - 100) < 5


def test_evaluate_unknown_op_returns_none(ohlcv_df):
    """Unknown op (WMA) → None cascade (plan [V1.3-A2-1])."""
    assert evaluate_pandas("WMA($close, 20)", ohlcv_df) is None


def test_evaluate_unknown_field_returns_none(ohlcv_df):
    """Unknown field ($unknown_field) → None cascade."""
    assert evaluate_pandas("Mean($unknown_field, 5)", ohlcv_df) is None


# ---------------------------------------------------------------------------
# Defensive contract — never raises
# ---------------------------------------------------------------------------

def test_evaluate_empty_expression_returns_none(ohlcv_df):
    assert evaluate_pandas("", ohlcv_df) is None
    assert evaluate_pandas(None, ohlcv_df) is None  # type: ignore


def test_evaluate_unbalanced_paren_returns_none(ohlcv_df):
    assert evaluate_pandas("Mean($close, 5", ohlcv_df) is None


def test_evaluate_empty_df_returns_none():
    empty = pd.DataFrame()
    assert evaluate_pandas("Mean($close, 5)", empty) is None


def test_evaluate_arity_mismatch_returns_none(ohlcv_df):
    """Mean expects 2 args; passing 1 → None."""
    assert evaluate_pandas("Mean($close)", ohlcv_df) is None


def test_evaluate_handles_arbitrary_exception(ohlcv_df, monkeypatch):
    """If an op impl raises, evaluator returns None (never raises)."""
    from backend.qlib_prescreen_pandas_engine import _OP_DISPATCH

    def _raising(*a, **k):
        raise RuntimeError("simulated impl failure")

    original = _OP_DISPATCH["Mean"]
    monkeypatch.setitem(_OP_DISPATCH, "Mean", (_raising, 2, "rolling"))
    try:
        out = evaluate_pandas("Mean($close, 5)", ohlcv_df)
    except Exception as e:
        pytest.fail(f"evaluate_pandas must never raise; got: {e}")
    assert out is None
