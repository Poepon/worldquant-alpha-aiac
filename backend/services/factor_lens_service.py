"""B2 R13 factor_decomposition shadow (Phase 4 Sprint 2 / plan v5 §6.9).

OLS decompose an alpha's daily return series against a static factor-
returns snapshot, producing:
  - residual_sharpe   = sharpe of (alpha_returns - X @ beta)
  - factor_exposures  = {factor_name → beta_coefficient}
  - r_squared         = 1 - SS_res / SS_tot

R13's purpose: distinguish *idiosyncratic* alpha edge from style-factor
exposure (size / value / momentum / quality / low_vol). High residual
sharpe means edge survives after factor neutralization → genuinely
novel signal. Low residual sharpe → most of the IS sharpe is just
ridding-the-factor-wave (Two Sigma's 18-factor lens; AQR's Frazzini-
Pedersen autoencoder asset pricing).

Three modes (per FACTOR_LENS_MODE):
  - shadow:  stamp residual_sharpe + factor_exposures into
             alpha.metrics, no quality_status change (Phase A obs)
  - soft:    residual_sharpe < τ → quality_status="PASS_PROVISIONAL"
             (queued for human review; Phase B)
  - hard:    residual_sharpe < τ → quality_status="FAIL" (Phase C)

Factor-returns snapshot lives at
``backend/data/factor_returns_snapshot/{region}.parquet`` — operator
refreshes monthly. Schema: date (index) × 5 factor columns. The
"static snapshot" approach mirrors Two Sigma / Citadel internal style-
risk monitors: tight refresh cadence isn't needed for cross-sectional
exposure measurement.

Pure-function module — no DB / BRAIN. Caller (evaluation node) does
the daily PnL fetch via CorrelationService + passes the resulting
Series in.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


_SNAPSHOT_DIR = (
    Path(__file__).resolve().parent.parent / "data" / "factor_returns_snapshot"
)

# Default factor set — operator can override via FACTOR_LENS_FACTORS.
_DEFAULT_FACTORS: List[str] = ["size", "value", "momentum", "quality", "low_vol"]


@dataclass
class Residual:
    """OLS decomposition result for one alpha."""
    residual_sharpe: float
    factor_exposures: Dict[str, float] = field(default_factory=dict)
    r_squared: float = 0.0
    ols_n_days: int = 0
    mode_used: str = "ols_daily"  # "ols_daily" | "bucket_median" | "skipped"


def _empty_residual(reason: str = "skipped") -> Residual:
    return Residual(residual_sharpe=0.0, mode_used=reason)


# ---------------------------------------------------------------------------
# Factor snapshot I/O
# ---------------------------------------------------------------------------

def _snapshot_path(region: str) -> Path:
    return _SNAPSHOT_DIR / f"{region.lower()}.parquet"


def load_factor_returns(
    region: str,
    *,
    factors: Optional[List[str]] = None,
) -> Optional["object"]:
    """Load a region's factor-returns snapshot as pandas DataFrame.

    Returns None on missing / corrupt / non-parquet file. Caller (the
    decompose path) is expected to soft-skip when None.

    Schema: ``date`` index (DatetimeIndex) × N factor columns. Only the
    columns named in ``factors`` are returned (subset filter so adding
    new factors to a snapshot doesn't break existing alpha decompose).
    """
    try:
        import pandas as pd
    except ImportError:
        logger.warning("[factor_lens] pandas missing — cannot load snapshot")
        return None

    path = _snapshot_path(region)
    if not path.exists():
        logger.debug(f"[factor_lens] snapshot missing: {path}")
        return None

    try:
        df = pd.read_parquet(path)
    except Exception as e:
        logger.warning(f"[factor_lens] snapshot parse failed ({path}): {e}")
        return None

    if df.empty:
        return None

    if factors is None:
        factors = _DEFAULT_FACTORS
    keep = [c for c in factors if c in df.columns]
    if not keep:
        logger.warning(
            f"[factor_lens] no overlapping factors in snapshot {path}: "
            f"want={factors} got={list(df.columns)}"
        )
        return None

    return df[keep].sort_index()


# ---------------------------------------------------------------------------
# OLS decomposition
# ---------------------------------------------------------------------------

def decompose(
    alpha_returns: "object",  # pd.Series
    factor_returns: "object",  # pd.DataFrame
    *,
    min_overlap_days: int = 60,
) -> Residual:
    """OLS decompose alpha_returns against factor_returns.

    Args:
        alpha_returns: pd.Series of daily returns indexed by Date.
        factor_returns: pd.DataFrame, columns = factor names, indexed
            by Date.
        min_overlap_days: minimum dates after intersection;less → return
            empty Residual.

    Returns:
        Residual with residual_sharpe + per-factor beta + r_squared.

    Empty / insufficient-overlap input → ``_empty_residual``.

    Pure-numpy lstsq (uncached at this layer; caller is expected to
    feed the same factor_returns DataFrame to multiple alphas within
    one round so the X matrix is reused without explicit caching).
    """
    try:
        import pandas as pd
    except ImportError:
        return _empty_residual("pandas_missing")

    if alpha_returns is None or factor_returns is None:
        return _empty_residual("none_input")
    if not isinstance(alpha_returns, pd.Series):
        return _empty_residual("bad_alpha_shape")
    if not isinstance(factor_returns, pd.DataFrame):
        return _empty_residual("bad_factor_shape")
    if alpha_returns.empty or factor_returns.empty:
        return _empty_residual("empty_input")

    # Drop NaN + align indexes
    a = alpha_returns.dropna()
    F = factor_returns.dropna(how="any")
    common = a.index.intersection(F.index)
    if len(common) < min_overlap_days:
        return _empty_residual("insufficient_overlap")

    y = a.loc[common].to_numpy(dtype=float)
    X = F.loc[common].to_numpy(dtype=float)
    n_factors = X.shape[1]
    factor_names = list(F.columns)

    # Add intercept column so factor_exposures captures pure beta + mean
    X_aug = np.hstack([X, np.ones((X.shape[0], 1), dtype=float)])

    try:
        beta_aug, _residuals_sumsq, _rank, _sv = np.linalg.lstsq(X_aug, y, rcond=None)
    except np.linalg.LinAlgError as e:
        logger.warning(f"[factor_lens] lstsq failed: {e}")
        return _empty_residual("lstsq_failed")

    # beta_aug = [β_1, ..., β_N, α(intercept)]
    betas = beta_aug[:n_factors]
    intercept = float(beta_aug[n_factors])

    # Residual returns = y - X_aug @ beta_aug
    y_hat = X_aug @ beta_aug
    residuals = y - y_hat

    # Annualized residual sharpe = mean(residuals) / std(residuals) × sqrt(252)
    res_mean = float(np.mean(residuals))
    res_std = float(np.std(residuals, ddof=1)) if len(residuals) > 1 else 0.0
    if res_std <= 1e-12:
        residual_sharpe = 0.0
    else:
        residual_sharpe = res_mean / res_std * float(np.sqrt(252))

    # R²
    ss_res = float(np.sum(residuals ** 2))
    y_mean = float(np.mean(y))
    ss_tot = float(np.sum((y - y_mean) ** 2))
    if ss_tot <= 1e-12:
        r_squared = 0.0
    else:
        r_squared = max(0.0, 1.0 - ss_res / ss_tot)

    factor_exposures: Dict[str, float] = {
        name: float(b) for name, b in zip(factor_names, betas)
    }
    # Stash intercept for diagnostics — small abs intercept = good fit
    factor_exposures["_intercept"] = intercept

    return Residual(
        residual_sharpe=float(residual_sharpe),
        factor_exposures=factor_exposures,
        r_squared=float(r_squared),
        ols_n_days=int(len(common)),
        mode_used="ols_daily",
    )


# ---------------------------------------------------------------------------
# Bucket fallback (when daily PnL series unavailable per R13-spike NO-GO)
# ---------------------------------------------------------------------------

def decompose_bucket(
    *,
    alpha_sharpe: float,
    pool_median_sharpe: float,
) -> Residual:
    """Degraded path: residual = alpha_sharpe - pool_median_sharpe.

    Used when ``decompose()`` cannot run (no daily PnL series). Output
    has no factor_exposures (zero-dimensional decomposition) and r²=0.
    """
    return Residual(
        residual_sharpe=float(alpha_sharpe - pool_median_sharpe),
        factor_exposures={},
        r_squared=0.0,
        ols_n_days=0,
        mode_used="bucket_median",
    )


# ---------------------------------------------------------------------------
# High-level convenience for the evaluation-node caller
# ---------------------------------------------------------------------------

def decompose_alpha(
    *,
    alpha_returns: Optional["object"],
    region: str,
    factors: Optional[List[str]] = None,
    min_overlap_days: int = 60,
) -> Residual:
    """One-call wrapper: load region snapshot + decompose.

    Returns ``_empty_residual("skipped")`` on any input gap so the
    caller can stamp metrics["_r13_residual_sharpe_mode"] = "skipped".

    The evaluation node uses this directly:

        residual = factor_lens_service.decompose_alpha(
            alpha_returns=daily_returns,
            region=alpha.region,
        )
        if residual.mode_used != "skipped":
            alpha.metrics["_r13_residual_sharpe"] = residual.residual_sharpe
            ...
    """
    if alpha_returns is None or not region:
        return _empty_residual("no_input")
    factor_df = load_factor_returns(region, factors=factors)
    if factor_df is None:
        return _empty_residual("no_snapshot")
    return decompose(alpha_returns, factor_df, min_overlap_days=min_overlap_days)


__all__ = [
    "Residual",
    "decompose",
    "decompose_bucket",
    "decompose_alpha",
    "load_factor_returns",
]
