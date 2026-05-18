"""Phase 3 Q10 PR1d: pandas-only qlib expression evaluator.

Plan: ~/.claude/plans/phase3-q10-pyqlib-prescreen-2026-05-18.md v1.3 §3.4
[V1.3-A2-1] (scope-locked to Q10 §2.2 operator subset).

The tier-3 fallback engine that activates when pyqlib is unavailable
(common on Windows dev per [[reference_phase0_plan_and_pyqlib_caveat]]).
Pure-pandas eval of a translated qlib expression on a MultiIndex
(datetime, instrument) OHLCV DataFrame. Unsupported operators return
None — caller cascades to skip:eval_error (mirror of unknown-field
treatment in the translator).

Scope-locked operator subset (plan §2.2 reverse table):
  - Rolling per-instrument: Mean / Std / Ref / Delta / Rank (ts) /
    Max / Min / Corr / Cov / ZScore
  - Element-wise: Add / Sub / Mul / Div / Sign / Abs / Log / Sqrt
  - Cross-section single-arg: Rank(x) (emulated via groupby('datetime'))

Out of subset (returns None, caller skips):
  - WMA / Slope / Resi / SignedPower / EMA / Quantile / Med / etc.
  - Any 3+-arity that's not Corr / Cov / If
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Operator dispatch table (qlib op name → callable on (pd.Series, *args))
# ---------------------------------------------------------------------------
#
# Operator implementations live below. Dispatch table built lazily because
# pandas import is optional at module load time (we want the file to be
# importable even when pandas/numpy missing — defensive for CI smoke).


def _try_import_pandas():
    """Defer pandas import until evaluate_pandas() is actually called."""
    try:
        import numpy as np
        import pandas as pd
        return pd, np
    except ImportError as ex:
        logger.warning(f"[Q10 pandas-engine] pandas/numpy unavailable: {ex}")
        return None, None


# ---------------------------------------------------------------------------
# Expression lexer + parser (regex + balanced paren — same style as
# qlib_translator)
# ---------------------------------------------------------------------------

_CALL_RE = re.compile(r"\b([A-Z][A-Za-z_]*)\s*\(")
_FIELD_RE = re.compile(r"\$([a-zA-Z_]\w*)")
_NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


def _split_args(arg_text: str) -> List[str]:
    """Balanced-paren top-level comma split (mirror of qlib_translator helper)."""
    out: List[str] = []
    depth = 0
    buf: List[str] = []
    for ch in arg_text:
        if ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf).strip())
    return out


def _is_numeric(token: str) -> bool:
    return bool(_NUM_RE.match(token.strip()))


# ---------------------------------------------------------------------------
# Operator implementations
# ---------------------------------------------------------------------------
# Signature for all impls: (pd, np, df, *resolved_args) -> Optional[pd.Series]
# where resolved_args are either pd.Series (per-instrument indexed) or
# scalars (int/float). Output is a Series indexed by (datetime, instrument).
# Return None on shape mismatch or runtime error so caller cascades.


def _per_instrument_rolling(series, window, func_name):
    """Apply a rolling-window aggregation grouped by instrument."""
    grouped = series.groupby(level="instrument", group_keys=False)
    if func_name == "mean":
        return grouped.rolling(int(window), min_periods=1).mean().droplevel(0)
    if func_name == "std":
        return grouped.rolling(int(window), min_periods=2).std().droplevel(0)
    if func_name == "max":
        return grouped.rolling(int(window), min_periods=1).max().droplevel(0)
    if func_name == "min":
        return grouped.rolling(int(window), min_periods=1).min().droplevel(0)
    raise ValueError(f"unsupported rolling func: {func_name}")


def _op_mean(pd, np, df, series, window):
    return _per_instrument_rolling(series, window, "mean")


def _op_std(pd, np, df, series, window):
    return _per_instrument_rolling(series, window, "std")


def _op_max(pd, np, df, series, window):
    return _per_instrument_rolling(series, window, "max")


def _op_min(pd, np, df, series, window):
    return _per_instrument_rolling(series, window, "min")


def _op_ref(pd, np, df, series, lag):
    # Ref($close, -5) = lag by 5 periods (positive shift). Negative ints in
    # qlib semantics. Per-instrument shift, otherwise leakage across stocks.
    # Defensive: ``brain_to_qlib`` only emits ``Ref(x, -N)`` (past value).
    # A positive lag here would translate to ``shift(-N)`` = peek into the
    # future = look-ahead bias. Fail loud if a future translator regression
    # ever lets a positive lag through, rather than silently producing a
    # leaky alpha that passes the Q10 floor.
    assert lag <= 0, (
        f"Ref with positive lag {lag} would peek into future; "
        "translator should not emit this"
    )
    return series.groupby(level="instrument").shift(-int(lag))


def _op_delta(pd, np, df, series, window):
    # Delta(x, w) = x - Ref(x, w)
    shifted = series.groupby(level="instrument").shift(int(window))
    return series - shifted


def _op_rank_ts(pd, np, df, series, window):
    # Time-series rank over rolling window, per instrument. pct=True 0..1.
    def _r(s):
        return s.rolling(int(window), min_periods=2).apply(
            lambda x: (x.argsort().argsort()[-1] + 1) / len(x) if len(x) > 1 else 0.5,
            raw=True,
        )
    return series.groupby(level="instrument", group_keys=False).apply(_r)


def _op_rank_cross_section(pd, np, df, series):
    # Single-arg cross-section: percentile rank within each datetime.
    return series.groupby(level="datetime").rank(pct=True) - 0.5


def _op_zscore(pd, np, df, series, window):
    mean = _per_instrument_rolling(series, window, "mean")
    std = _per_instrument_rolling(series, window, "std")
    return (series - mean) / std.replace(0, np.nan)


def _op_corr(pd, np, df, x, y, window):
    def _c(pair):
        return pair.iloc[:, 0].rolling(int(window), min_periods=2).corr(pair.iloc[:, 1])
    pair = pd.concat([x.rename("a"), y.rename("b")], axis=1)
    return pair.groupby(level="instrument", group_keys=False).apply(_c)


def _op_cov(pd, np, df, x, y, window):
    def _c(pair):
        return pair.iloc[:, 0].rolling(int(window), min_periods=2).cov(pair.iloc[:, 1])
    pair = pd.concat([x.rename("a"), y.rename("b")], axis=1)
    return pair.groupby(level="instrument", group_keys=False).apply(_c)


# Element-wise binary
def _op_add(pd, np, df, a, b):
    return _as_series(pd, df, a) + _as_series(pd, df, b)


def _op_sub(pd, np, df, a, b):
    return _as_series(pd, df, a) - _as_series(pd, df, b)


def _op_mul(pd, np, df, a, b):
    return _as_series(pd, df, a) * _as_series(pd, df, b)


def _op_div(pd, np, df, a, b):
    return _as_series(pd, df, a) / _as_series(pd, df, b).replace(0, np.nan)


# Element-wise unary
def _op_abs(pd, np, df, series):
    return series.abs()


def _op_sign(pd, np, df, series):
    return np.sign(series)


def _op_log(pd, np, df, series):
    return np.log(series.where(series > 0))  # NaN out non-positive


def _op_sqrt(pd, np, df, series):
    return np.sqrt(series.where(series >= 0))


def _as_series(pd, df, value):
    """Promote a scalar to a Series aligned with df.index; pass through if already Series."""
    if hasattr(value, "index"):
        return value
    return pd.Series(float(value), index=df.index)


_OP_DISPATCH: Dict[str, tuple] = {
    # name → (impl_callable, expected_arity, "rolling"/"elemwise"/"unary"/"binary_window"/"cross_section")
    "Mean":  (_op_mean,  2, "rolling"),
    "Std":   (_op_std,   2, "rolling"),
    "Max":   (_op_max,   2, "rolling"),
    "Min":   (_op_min,   2, "rolling"),
    "Ref":   (_op_ref,   2, "rolling"),
    "Delta": (_op_delta, 2, "rolling"),
    "Rank":  (None,      0, "rank_variadic"),  # special-cased: 1 arg → cross-section, 2 args → ts
    "ZScore":(_op_zscore,2, "rolling"),
    "Corr":  (_op_corr,  3, "binary_window"),
    "Cov":   (_op_cov,   3, "binary_window"),
    "Add":   (_op_add,   2, "binary"),
    "Sub":   (_op_sub,   2, "binary"),
    "Mul":   (_op_mul,   2, "binary"),
    "Div":   (_op_div,   2, "binary"),
    "Abs":   (_op_abs,   1, "unary"),
    "Sign":  (_op_sign,  1, "unary"),
    "Log":   (_op_log,   1, "unary"),
    "Sqrt":  (_op_sqrt,  1, "unary"),
}


# ---------------------------------------------------------------------------
# Recursive evaluator
# ---------------------------------------------------------------------------


def _resolve_arg(pd, np, df, token: str):
    """Resolve an argument: $field / numeric literal / nested call expression."""
    token = token.strip()
    if not token:
        return None
    # Numeric literal
    if _is_numeric(token):
        try:
            v = float(token)
            return int(v) if v == int(v) else v
        except Exception:
            return None
    # Field ($close etc.)
    field_match = _FIELD_RE.fullmatch(token)
    if field_match:
        name = field_match.group(1)
        if name in df.columns:
            return df[name]
        return None
    # Nested call
    return _eval_node(pd, np, df, token)


def _eval_node(pd, np, df, expr: str):
    """Recursively evaluate a single qlib (sub)expression."""
    expr = expr.strip()
    if not expr:
        return None
    # Strip outer parens if balanced
    if expr.startswith("(") and expr.endswith(")"):
        depth = 0
        ok = True
        for i, ch in enumerate(expr):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and i < len(expr) - 1:
                    ok = False
                    break
        if ok:
            return _eval_node(pd, np, df, expr[1:-1])
    # Look for call
    m = _CALL_RE.search(expr)
    if m is None:
        # Leaf (field / numeric / unknown)
        return _resolve_arg(pd, np, df, expr)
    op_name = m.group(1)
    if op_name not in _OP_DISPATCH:
        logger.debug(f"[Q10 pandas-engine] unknown op '{op_name}' — cascade to None")
        return None
    # Find matching close paren
    paren_open = m.end() - 1
    depth = 0
    paren_close = -1
    for i in range(paren_open, len(expr)):
        if expr[i] == "(":
            depth += 1
        elif expr[i] == ")":
            depth -= 1
            if depth == 0:
                paren_close = i
                break
    if paren_close == -1:
        return None  # unbalanced
    raw_args = expr[paren_open + 1:paren_close]
    args = _split_args(raw_args)
    # Special-case Rank (1 arg = cross-section, 2 args = ts)
    if op_name == "Rank":
        if len(args) == 1:
            x = _resolve_arg(pd, np, df, args[0])
            if x is None:
                return None
            return _op_rank_cross_section(pd, np, df, x)
        if len(args) == 2:
            x = _resolve_arg(pd, np, df, args[0])
            w = _resolve_arg(pd, np, df, args[1])
            if x is None or w is None:
                return None
            return _op_rank_ts(pd, np, df, x, w)
        return None
    impl, arity, kind = _OP_DISPATCH[op_name]
    if len(args) != arity:
        logger.debug(
            f"[Q10 pandas-engine] arity mismatch for '{op_name}': expected {arity}, got {len(args)}"
        )
        return None
    resolved = [_resolve_arg(pd, np, df, a) for a in args]
    if any(r is None for r in resolved):
        return None
    try:
        return impl(pd, np, df, *resolved)
    except Exception as ex:
        logger.debug(f"[Q10 pandas-engine] op '{op_name}' raised: {ex}")
        return None


def evaluate_pandas(qlib_expr: str, df) -> Optional[Any]:
    """Evaluate a qlib DSL expression on a pandas OHLCV DataFrame.

    Args:
        qlib_expr: qlib DSL string (e.g., 'Mean($close, 20)').
        df: pd.DataFrame indexed by MultiIndex (datetime, instrument) with
            columns matching qlib $field names ('close', 'open', 'high',
            'low', 'volume', 'vwap', ...). The $ prefix is stripped during
            arg resolution.

    Returns:
        pd.Series indexed by (datetime, instrument) on success, or None on
        any failure (unknown op, unsupported arity, runtime exception).

    Contract: never raises — all errors return None so the Q10 caller can
    soft-fall to skip:eval_error.
    """
    if not qlib_expr or not isinstance(qlib_expr, str):
        return None
    pd_mod, np_mod = _try_import_pandas()
    if pd_mod is None:
        return None
    if df is None or len(df) == 0:
        return None
    try:
        return _eval_node(pd_mod, np_mod, df, qlib_expr)
    except Exception as ex:
        logger.warning(f"[Q10 pandas-engine] top-level exception: {ex}")
        return None


__all__ = [
    "evaluate_pandas",
]
