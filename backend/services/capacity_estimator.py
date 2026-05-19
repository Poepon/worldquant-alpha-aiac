"""B1 R11 alpha_capacity_estimator (Phase 4 Sprint 2 / plan v5 §6.8).

Industrial-派 capacity-cap intuition:high-sharpe low-capacity alphas
should rank lower than equally-strong-but-bigger ones. AIAC currently
ignores capacity in composite scoring.

Estimator is *deliberately coarse* — it is the **5th dimension** of
``evaluate_alpha_comprehensive`` composite score, not a precise sizing
model. Order-of-magnitude is enough.

Formula
-------
::

    capacity_usd = ADV(region, universe) × universe_size × max_alpha_share
                   × (1 - turnover_decay_factor)

  where:
    ADV               = average daily volume per stock (USD) from
                        region_universe_adv.json
    universe_size     = number of stocks in the universe
    max_alpha_share   = 0.10  (single alpha cannot trade >10% of ADV
                              before slippage erases edge — industry
                              rule of thumb)
    turnover_decay    = (turnover - 0.5) / 2.0 clipped to [0, 0.5]
                        — high-turnover alphas churn the universe
                        faster, so realizable capacity decays.

Log-scale normalization
-----------------------
``normalize(capacity_usd)`` maps capacity onto [0, 1] via 5 log buckets
(``CAPACITY_LOG_BUCKETS = [1e6, 1e7, 1e8, 1e9, 1e10]``):

  - <$1M       → 0.00
  - $1M–$10M   → 0.25
  - $10M–$100M → 0.50
  - $100M–$1B  → 0.75
  - >$1B       → 1.00

Composite score adds this as the 5th dimension at
``CAPACITY_SCORE_WEIGHT`` (default 0.10); the original 4 weights are
scaled by ``(1 - CAPACITY_SCORE_WEIGHT)`` to keep ``sum == 1.0``.

Pure-function module — zero DB dependency, ADV table is JSON cached
in-memory once at first call.
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


_ADV_JSON_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "region_universe_adv.json"
)

# Industry rule of thumb: a single alpha cannot trade more than ~10% of
# average daily volume before slippage and price-impact erase its edge.
# (Citadel / Two Sigma sizing papers; AQR Frazzini-Pedersen et al.)
_MAX_ALPHA_SHARE = 0.10


# ---------------------------------------------------------------------------
# ADV table I/O
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _load_adv_table() -> Dict[str, Any]:
    """Lazy-load the JSON ADV snapshot (cached for process lifetime).

    Soft-fail to ``{}`` on missing/corrupt JSON — caller falls through
    to the conservative default in ``_resolve_adv``.
    """
    try:
        with _ADV_JSON_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning(
            "[capacity_estimator] ADV table missing at %s — using default fallback",
            _ADV_JSON_PATH,
        )
        return {}
    except Exception as e:
        logger.warning(
            "[capacity_estimator] ADV table parse failed (%s) — using default fallback",
            e,
        )
        return {}


def clear_adv_table_cache() -> None:
    """Test helper — drops the lru_cache so a monkey-patched JSON path takes effect."""
    _load_adv_table.cache_clear()


def _resolve_adv(region: str, universe: str) -> Tuple[float, int]:
    """Look up (adv_usd_per_stock, universe_size) for a (region, universe).

    Falls through to the ``_default`` entry on miss. Both ``_default`` and
    a missing JSON give the conservative (1e7, 1000) baseline — that's
    log-bucket 0.50, which neither helps nor hurts the unknown alpha.
    """
    table = _load_adv_table()
    if not table:
        return 1.0e7, 1000

    region_table = table.get(region) or {}
    if not isinstance(region_table, dict):
        region_table = {}

    entry = region_table.get(universe)
    if not isinstance(entry, dict):
        # Try region-level miss → universe-default → global default
        entry = table.get("_default") or {}
        if not isinstance(entry, dict):
            return 1.0e7, 1000

    try:
        adv = float(entry.get("adv_usd_per_stock", 1.0e7))
        usize = int(entry.get("universe_size", 1000))
        return adv, usize
    except (TypeError, ValueError):
        return 1.0e7, 1000


# ---------------------------------------------------------------------------
# Estimate
# ---------------------------------------------------------------------------

def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def estimate(
    *,
    region: str,
    universe: str,
    turnover: float,
) -> float:
    """Return capacity in USD for an alpha with the given properties.

    Args:
        region: market region (e.g. "USA", "CHN") — case-sensitive,
            falls back to default on miss.
        universe: BRAIN universe id (e.g. "TOP3000") — falls back to
            region-default then global default.
        turnover: BRAIN-reported turnover (fraction, e.g. 0.30 = 30%
            daily turnover). Negative / non-numeric → treated as 0.

    Returns:
        capacity_usd (float, ≥0). Always finite. Will be 0.0 only when
        turnover_decay_factor=1.0 (unrealistic), otherwise ≥ 1 USD.

    Pure function — does not touch DB or BRAIN.
    """
    adv_per_stock, universe_size = _resolve_adv(region, universe)
    turnover_f = _safe_float(turnover, 0.0)
    if turnover_f < 0:
        turnover_f = 0.0

    # Turnover decay: alphas above 50% daily turnover lose capacity
    # proportionally. (Citadel slippage curves are roughly linear in this
    # range; cap at 50% decay so capacity never collapses entirely.)
    decay = max(0.0, min(0.5, (turnover_f - 0.5) / 2.0))
    capacity_factor = 1.0 - decay

    capacity_usd = (
        adv_per_stock
        * universe_size
        * _MAX_ALPHA_SHARE
        * capacity_factor
    )
    return float(max(capacity_usd, 0.0))


def estimate_from_alpha_dict(alpha_or_sim: Dict[str, Any]) -> float:
    """Convenience wrapper for the ``evaluate_alpha_comprehensive`` call site.

    Pulls ``region`` / ``settings.universe`` / ``is.turnover`` (or
    top-level ``turnover``) from a BRAIN sim_result dict (or a similar
    flat AlphaCandidate-like object) and dispatches to ``estimate``.
    Soft-fails to 0.0 on unparseable input.
    """
    if not isinstance(alpha_or_sim, dict):
        return 0.0

    region = alpha_or_sim.get("region")
    settings = alpha_or_sim.get("settings") or {}
    universe = (
        alpha_or_sim.get("universe")
        or (settings.get("universe") if isinstance(settings, dict) else None)
    )

    # Try multiple turnover paths — BRAIN sim_result has it under
    # `is.turnover` (raw response shape) but flat alpha dicts often
    # promote it to top-level `turnover`.
    is_stats = alpha_or_sim.get("is") or {}
    metrics = alpha_or_sim.get("metrics") or {}
    turnover = (
        alpha_or_sim.get("turnover")
        or (is_stats.get("turnover") if isinstance(is_stats, dict) else None)
        or (metrics.get("turnover") if isinstance(metrics, dict) else None)
        or 0.0
    )

    if not region or not universe:
        return 0.0

    return estimate(region=str(region), universe=str(universe), turnover=_safe_float(turnover))


# ---------------------------------------------------------------------------
# Normalization (log buckets → [0, 1])
# ---------------------------------------------------------------------------

def normalize(
    capacity_usd: float,
    buckets: Optional[list] = None,
) -> float:
    """Map a USD capacity onto [0, 1] using log-scale buckets.

    Default buckets [1e6, 1e7, 1e8, 1e9, 1e10] correspond to:
      - <$1M           → 0.00
      - $1M–$10M       → 0.25
      - $10M–$100M     → 0.50
      - $100M–$1B      → 0.75
      - >$1B           → 1.00

    Operator may pass a custom 4-element sorted list to shift the curve
    (e.g. for emerging-market task where $100M is "huge"). When
    ``buckets is None`` we read ``settings.CAPACITY_LOG_BUCKETS`` —
    lazy import to keep this module Pydantic-free in test envs.
    """
    if buckets is None:
        try:
            from backend.config import settings as _settings
            buckets = list(_settings.CAPACITY_LOG_BUCKETS or [])
        except Exception:
            buckets = []
    if not buckets:
        buckets = [1.0e6, 1.0e7, 1.0e8, 1.0e9, 1.0e10]

    n = len(buckets)
    if capacity_usd <= 0:
        return 0.0
    if capacity_usd < buckets[0]:
        return 0.0
    if capacity_usd >= buckets[-1]:
        return 1.0
    # buckets[i] ≤ capacity < buckets[i+1] → score = (i+1) / n
    for i in range(n - 1):
        if buckets[i] <= capacity_usd < buckets[i + 1]:
            return float((i + 1)) / float(n)
    return 1.0  # safety net (unreachable)


__all__ = [
    "estimate",
    "estimate_from_alpha_dict",
    "normalize",
    "clear_adv_table_cache",
]
