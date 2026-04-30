"""
Self-correlation service for hard-gate validation.

Implements the local PnL-matrix approach (per W0.5 plan, mirrored from the
user-provided reference snippet):

1. Maintain a per-region cache of OS-stage alpha PnL series under
   `backend/data/correlation_cache/os_pnls_{region}.pkl`.
2. For each new alpha, fetch its PnL, compute daily returns, then
   `corrwith` against every cached OS alpha's daily returns and take max.
3. Three-tier fallback when local cache is unavailable / stale:
   local cache → BRAIN `/alphas/{id}/correlations/SELF` API → 0.0 with
   `unverified` flag (caller should treat as PASS_PROVISIONAL, not PASS).

Why local first: BRAIN /correlations/SELF endpoint is slow (5-30s) and
counts toward rate limits; the local matrix gives ~50ms/alpha.

Reference: see plan §"Self-correlation 实现路径".
"""

from __future__ import annotations

import asyncio
import pickle
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
from loguru import logger

from backend.adapters.brain_adapter import BrainAdapter

# Cache lives under repo data dir; .gitignore should exclude it.
CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "correlation_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# History window for return correlation. Mirrors the reference: trailing 4 yrs.
LOOKBACK_YEARS = 4
# Minimum overlap days for a meaningful corr. Below this we treat as "unknown"
# rather than reporting a noisy small-sample correlation as truth.
MIN_OVERLAP_DAYS = 60
# Concurrency limit for PnL fetches (BRAIN tolerates ~10 in parallel without
# triggering sub-minute burst red-line).
PNL_FETCH_CONCURRENCY = 10
# Cache freshness: refresh if older than 24h.
CACHE_TTL_HOURS = 24


def _cache_path(region: str) -> Path:
    return CACHE_DIR / f"os_pnls_{region}.pkl"


def _pnl_records_to_series(pnl_payload: Dict, alpha_id: str) -> pd.Series:
    """Convert BRAIN PnL recordset payload to a Date-indexed Series.

    BRAIN response shape: `{"records": [[date, pnl, ...], ...],
                            "schema": {"properties": [{"name": ...}, ...]}}`
    """
    records = pnl_payload.get("records") or []
    props = pnl_payload.get("schema", {}).get("properties", []) or []
    if not records or not props:
        return pd.Series(dtype="float64", name=alpha_id)
    columns = [p.get("name") for p in props]
    df = pd.DataFrame(records, columns=columns)
    if "date" not in df.columns or "pnl" not in df.columns:
        return pd.Series(dtype="float64", name=alpha_id)
    df["date"] = pd.to_datetime(df["date"])
    series = df.set_index("date")["pnl"].astype("float64")
    series.name = alpha_id
    return series


def _series_to_returns(pnl_series: pd.Series) -> pd.Series:
    """Daily returns = pnl - pnl.ffill().shift(1), trimmed to lookback window."""
    if pnl_series.empty:
        return pnl_series
    returns = pnl_series - pnl_series.ffill().shift(1)
    cutoff = pnl_series.index.max() - pd.DateOffset(years=LOOKBACK_YEARS)
    return returns[returns.index > cutoff]


class CorrelationService:
    """Compute self-correlation against the user's OS alpha set.

    Stateless except for the on-disk pickle cache; safe to instantiate
    per request. Pass an existing BrainAdapter to share authenticated
    session.
    """

    def __init__(self, brain: BrainAdapter):
        self.brain = brain

    # ------------------------------------------------------------------
    # Cache I/O
    # ------------------------------------------------------------------

    def _load_cache(self, region: str) -> Optional[Dict]:
        path = _cache_path(region)
        if not path.exists():
            return None
        try:
            with path.open("rb") as f:
                return pickle.load(f)
        except Exception as e:
            logger.warning(f"[CorrelationService] Failed to load cache {path}: {e}")
            return None

    def _save_cache(self, region: str, alpha_ids: List[str], pnls: pd.DataFrame) -> None:
        path = _cache_path(region)
        payload = {
            "alpha_ids": alpha_ids,
            "pnls": pnls,
            "saved_at": datetime.utcnow().isoformat(),
        }
        try:
            with path.open("wb") as f:
                pickle.dump(payload, f, pickle.HIGHEST_PROTOCOL)
        except Exception as e:
            logger.error(f"[CorrelationService] Failed to save cache {path}: {e}")

    def _is_cache_fresh(self, cache: Dict) -> bool:
        saved_at = cache.get("saved_at")
        if not saved_at:
            return False
        try:
            age = datetime.utcnow() - datetime.fromisoformat(saved_at)
            return age < timedelta(hours=CACHE_TTL_HOURS)
        except Exception:
            return False

    # ------------------------------------------------------------------
    # OS alpha cache refresh
    # ------------------------------------------------------------------

    async def _fetch_os_alpha_ids(self, region: str, max_count: int = 500) -> List[str]:
        """List OS alphas for a region via paginated /users/self/alphas."""
        out: List[str] = []
        offset = 0
        limit = 100
        while offset < max_count:
            res = await self.brain.get_user_alphas(limit=limit, offset=offset, stage="OS")
            results = res.get("results") or []
            if not results:
                break
            for a in results:
                # filter to region (BRAIN /alphas listing returns mixed regions)
                if a.get("settings", {}).get("region") == region:
                    out.append(a["id"])
            if len(results) < limit:
                break
            offset += limit
        return out

    async def _fetch_pnl_series(self, alpha_id: str) -> pd.Series:
        payload = await self.brain.get_alpha_pnl(alpha_id)
        return _pnl_records_to_series(payload, alpha_id)

    async def refresh_os_alpha_cache(
        self,
        region: str = "USA",
        incremental: bool = True,
    ) -> Tuple[int, int]:
        """Refresh PnL cache for OS alphas in a region.

        Args:
            region: BRAIN region code.
            incremental: if True, only fetch alphas not already cached.

        Returns: (newly_fetched_count, total_in_cache)
        """
        existing = self._load_cache(region) if incremental else None
        existing_ids = set(existing["alpha_ids"]) if existing else set()
        existing_pnls = existing["pnls"] if existing else pd.DataFrame()

        all_ids = await self._fetch_os_alpha_ids(region)
        new_ids = [aid for aid in all_ids if aid not in existing_ids]

        if not new_ids:
            logger.info(
                f"[CorrelationService] {region} cache up to date "
                f"({len(existing_ids)} alphas)"
            )
            return 0, len(existing_ids)

        logger.info(
            f"[CorrelationService] {region}: fetching {len(new_ids)} new PnL series "
            f"(existing={len(existing_ids)}, concurrency={PNL_FETCH_CONCURRENCY})"
        )

        sem = asyncio.Semaphore(PNL_FETCH_CONCURRENCY)

        async def fetch_one(aid: str) -> Optional[pd.Series]:
            async with sem:
                try:
                    s = await self._fetch_pnl_series(aid)
                    return s if not s.empty else None
                except Exception as e:
                    logger.warning(f"[CorrelationService] PnL fetch failed for {aid}: {e}")
                    return None

        results = await asyncio.gather(*(fetch_one(aid) for aid in new_ids))
        new_series = [s for s in results if s is not None and not s.empty]

        if new_series:
            new_df = pd.concat(new_series, axis=1)
            combined = (
                pd.concat([existing_pnls, new_df], axis=1)
                if not existing_pnls.empty
                else new_df
            )
            combined = combined.loc[:, ~combined.columns.duplicated()]
            combined.sort_index(inplace=True)
            all_cached_ids = list(combined.columns)
            self._save_cache(region, all_cached_ids, combined)
            return len(new_series), len(all_cached_ids)

        return 0, len(existing_ids)

    # ------------------------------------------------------------------
    # Self-correlation calculation
    # ------------------------------------------------------------------

    async def calc_self_corr(
        self,
        alpha_id: str,
        region: str,
        alpha_pnl_series: Optional[pd.Series] = None,
    ) -> Tuple[float, str]:
        """Compute max self-correlation against cached OS alphas.

        Returns: (corr_value, source) where source ∈ {"local", "empty"}.
        Caller should branch on source for fallback.
        """
        cache = self._load_cache(region)
        if not cache or not cache.get("alpha_ids"):
            return 0.0, "empty"

        if alpha_pnl_series is None:
            alpha_pnl_series = await self._fetch_pnl_series(alpha_id)
        if alpha_pnl_series.empty:
            return 0.0, "empty"

        target_returns = _series_to_returns(alpha_pnl_series)
        if len(target_returns.dropna()) < MIN_OVERLAP_DAYS:
            logger.debug(
                f"[CorrelationService] {alpha_id} has {len(target_returns.dropna())} "
                f"days < {MIN_OVERLAP_DAYS}; treat as 0.0 (insufficient sample)"
            )
            return 0.0, "empty"

        os_returns = cache["pnls"].apply(
            lambda col: col - col.ffill().shift(1), axis=0
        )
        cutoff = os_returns.index.max() - pd.DateOffset(years=LOOKBACK_YEARS)
        os_returns = os_returns[os_returns.index > cutoff]

        # Drop the target alpha if it happens to already be in the OS cache
        if alpha_id in os_returns.columns:
            os_returns = os_returns.drop(columns=[alpha_id])

        if os_returns.shape[1] == 0:
            return 0.0, "empty"

        corrs = os_returns.corrwith(target_returns)
        max_corr = float(corrs.max(skipna=True))
        if pd.isna(max_corr):
            max_corr = 0.0
        return max_corr, "local"

    # ------------------------------------------------------------------
    # Public entry: three-tier fallback
    # ------------------------------------------------------------------

    async def get_with_fallback(
        self,
        alpha_id: str,
        region: str = "USA",
    ) -> Tuple[float, str]:
        """Three-tier resolver. Returns (corr, source).

        source values:
          - "local"   — computed from local PnL cache (preferred)
          - "brain"   — via BRAIN /correlations/SELF API
          - "unknown" — both failed; caller should NOT treat as PASS
        """
        try:
            corr, src = await self.calc_self_corr(alpha_id, region)
            if src == "local":
                return corr, "local"
        except Exception as e:
            logger.warning(f"[CorrelationService] local calc failed for {alpha_id}: {e}")

        try:
            res = await self.brain.check_correlation(alpha_id, check_type="SELF")
            if isinstance(res, dict) and res.get("max") is not None:
                return float(res["max"]), "brain"
        except Exception as e:
            logger.warning(f"[CorrelationService] BRAIN /correlations/SELF failed for {alpha_id}: {e}")

        return 0.0, "unknown"
