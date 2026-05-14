"""
Baseline Screener - Fit-baseline + Nσ-residual anomaly detection for alpha mining.

Borrowed from the AlphaGBM ``vol-surface`` skill's "fit a surface, then flag the
points that deviate >Nσ from the fit" pattern (see
docs/alphagbm_skills_research_2026-05-15.md).

Idea: bucket historically backtested alphas into a (hypothesis-family × dataset ×
region) grid. For each cell fit a performance distribution (mean / std). A fresh
alpha's residual sigma = (value - mean) / std tells whether it genuinely beats the
typical attempt in its cell, rather than merely clearing an absolute threshold.

Pure functions only — no DB, no I/O. DB orchestration (cell sampling + fine→coarse
fallback) lives in backend/agents/services/baseline_provider.py.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import List, Optional

# A std below this is treated as degenerate (all samples ~identical) — the
# residual would be meaningless / explode, so we decline to score the cell.
_MIN_STD = 1e-9

# Residual classification buckets.
DISCOVERY = "DISCOVERY"
NORMAL = "NORMAL"
BELOW = "BELOW"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


@dataclass
class BaselineStats:
    """Fitted performance distribution for one grid cell."""

    mean: float
    std: float
    count: int
    cell_key: str
    granularity: str  # "fine" | "coarse" | "insufficient"

    @property
    def usable(self) -> bool:
        """True when the baseline can produce a meaningful residual."""
        return self.granularity != "insufficient" and self.std > _MIN_STD


def fit_baseline(
    samples: List[float],
    min_samples: int,
    cell_key: str,
    granularity: str,
) -> BaselineStats:
    """
    Fit a (mean, std) baseline over a cell's historical metric samples.

    Returns granularity="insufficient" when there are fewer than ``min_samples``
    usable samples, or the distribution is degenerate (std ≈ 0).
    """
    clean = [float(s) for s in samples if s is not None]
    if len(clean) < max(min_samples, 2):
        return BaselineStats(0.0, 0.0, len(clean), cell_key, "insufficient")

    mean = statistics.mean(clean)
    std = statistics.pstdev(clean)
    if std <= _MIN_STD:
        return BaselineStats(mean, std, len(clean), cell_key, "insufficient")

    return BaselineStats(mean, std, len(clean), cell_key, granularity)


def residual_sigma(value: Optional[float], stats: BaselineStats) -> Optional[float]:
    """
    Residual of ``value`` against the cell baseline, in standard deviations.

    Returns None when the baseline is unusable or ``value`` is missing.
    """
    if value is None or not stats.usable:
        return None
    return (float(value) - stats.mean) / stats.std


def classify_residual(
    sigma: Optional[float],
    discovery: float,
    below: float,
) -> str:
    """
    Bucket a residual sigma into a named class:

      - sigma is None      -> INSUFFICIENT_DATA
      - sigma >= discovery -> DISCOVERY   (genuinely beats the cell baseline)
      - sigma <= below     -> BELOW       (worse than the cell's typical attempt)
      - otherwise          -> NORMAL
    """
    if sigma is None:
        return INSUFFICIENT_DATA
    if sigma >= discovery:
        return DISCOVERY
    if sigma <= below:
        return BELOW
    return NORMAL
