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
import json
import pickle
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
from loguru import logger

from backend.adapters.brain_adapter import BrainAdapter
from backend.config import settings


class CorrSource(StrEnum):
    """V-27.158: unified vocabulary for "where a self-correlation value came
    from", replacing the old per-method ad-hoc strings (calc_self_corr used
    {local, empty}; get_with_fallback used {local, brain, unknown}).

    StrEnum (Python 3.11+) so existing `source == "local"` comparisons in
    callers stay valid, str()/f-string render the value ("local"), and JSONB
    serialisation stays a plain string.

    NOTE: calc_self_corr_by_window keeps its own finer-grained per-window
    status set {ok, insufficient_data, empty_pool, missing_window} — that is
    a different axis (per-crisis-window measurability), intentionally NOT
    folded into CorrSource.
    """
    LOCAL = "local"      # measured from the local OS PnL matrix (preferred)
    BRAIN = "brain"      # from BRAIN /correlations/SELF API
    # V-27.126: BRAIN accepted the request but corr is still computing
    # (max=None). Distinct from UNKNOWN ("could not measure") — a caller that
    # can wait may retry. corr value is still None.
    BRAIN_PENDING = "brain_pending"
    UNKNOWN = "unknown"  # not measured — cache miss / no PnL / both tiers failed

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


# ---------------------------------------------------------------------------
# Crisis windows for stress-testing pairwise correlation.
#
# V-27.148: the date ranges now live in config.py (settings.CRISIS_WINDOWS)
# so a new crisis event can be added without a code change + redeploy.
# Selection criteria (see plan): distinct regime character (each window
# stresses a different failure mode — liquidity / rates / sector contagion /
# geopolitics); >= ~30 trading days; inside the BRAIN PnL history typically
# available for OS alphas. Values are [start, end] ISO date pairs; the
# in-file references use rng[0]/rng[1] so list-vs-tuple is irrelevant.
# ---------------------------------------------------------------------------
CRISIS_WINDOWS = settings.CRISIS_WINDOWS

# Per-window overlap floor. Crisis windows are inherently shorter than the
# 4-year lookback so the 60-day default would reject everything. 20 trading
# days ≈ one calendar month — below that the corr is too noisy to act on.
MIN_OVERLAP_DAYS_PER_WINDOW = 20

# Threshold above which a per-window pairwise correlation is flagged as a
# "hotspot" — surfaced in stress-test summary so reviewers can see which
# alphas converge under stress even if their global self-corr looks fine.
CRISIS_HOTSPOT_THRESHOLD = 0.7


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


def _pnls_to_returns_df(pnls: pd.DataFrame) -> pd.DataFrame:
    """Convert a wide PnL DataFrame (one column per alpha) to daily returns.

    Unlike _series_to_returns above, this does NOT apply the LOOKBACK_YEARS
    cutoff — crisis-window slicing happens after this call and may need
    older data than the 4-year max-corr window.
    """
    if pnls.empty:
        return pnls
    return pnls.apply(lambda col: col - col.ffill().shift(1), axis=0)


def _slice_returns_to_window(
    returns: pd.DataFrame | pd.Series,
    window: str,
) -> pd.DataFrame | pd.Series:
    """Slice a returns frame/series to one of the named crisis windows.

    Returns an empty frame/series if the window is unknown.
    """
    rng = CRISIS_WINDOWS.get(window)
    if not rng:
        return returns.iloc[0:0] if hasattr(returns, "iloc") else returns
    start, end = pd.Timestamp(rng[0]), pd.Timestamp(rng[1])
    return returns.loc[(returns.index >= start) & (returns.index <= end)]


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
        # V-27.131: atomic write — the refresh_os_corr_cache.py script and the
        # 06:30 beat can both write os_pnls_{region}.pkl. A direct open("wb")
        # leaves a half-written pickle visible to _load_cache (which then
        # throws, gets swallowed, and silently degrades every self_corr to
        # the BRAIN tier). tmp-then-rename makes the swap atomic on POSIX and
        # Windows (Path.replace).
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            with tmp.open("wb") as f:
                pickle.dump(payload, f, pickle.HIGHEST_PROTOCOL)
            tmp.replace(path)
        except Exception as e:
            logger.error(f"[CorrelationService] Failed to save cache {path}: {e}")
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

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
                if a.get("settings", {}).get("region") != region:
                    continue
                # V-27.157: a BRAIN listing item occasionally lacks `id` —
                # skip it rather than KeyError-crash the whole OS sync.
                aid = a.get("id")
                if aid is None:
                    logger.warning(
                        f"[CorrelationService] OS alpha listing item missing "
                        f"`id`, skipping: {str(a)[:160]}"
                    )
                    continue
                out.append(aid)
            if len(results) < limit:
                break
            offset += limit
        return out

    async def _fetch_pnl_series(
        self, alpha_id: str, max_attempts: int = 3
    ) -> pd.Series:
        """Fetch + parse an alpha's PnL series, retrying transient failures.

        BRAIN's /alphas/{id}/recordsets/pnl occasionally returns an empty
        payload under burst (rate-limit soft-fail) — at the parse layer this
        is indistinguishable from an alpha that genuinely has no PnL. Before
        this retry loop, a single transient empty response would propagate
        all the way up to `get_with_fallback` returning ("unknown"), and the
        caller (evaluation node) would mark the alpha self_corr-unverified
        forever — even though a retry would have measured it. Retrying with
        backoff recovers the transient case; a still-empty result after
        `max_attempts` is treated as genuinely empty by the caller.
        """
        last_exc: Optional[Exception] = None
        for attempt in range(max_attempts):
            try:
                payload = await self.brain.get_alpha_pnl(alpha_id)
                series = _pnl_records_to_series(payload, alpha_id)
                if not series.empty:
                    return series
            except Exception as e:
                last_exc = e
                logger.debug(
                    f"[CorrelationService] PnL fetch attempt "
                    f"{attempt + 1}/{max_attempts} failed for {alpha_id}: {e}"
                )
            if attempt < max_attempts - 1:
                await asyncio.sleep(1.5 * (attempt + 1))
        # V-27.129: unify both failure paths — "3× exception" and "3× empty"
        # now both return an empty Series. PnL being unreachable is an
        # expected boundary (BRAIN rate-limit soft-fail / genuinely no PnL);
        # callers already handle empty Series, and raising here forced them
        # onto a second code path. Downgrade the last exception to a warning.
        if last_exc is not None:
            logger.warning(
                f"[CorrelationService] PnL fetch for {alpha_id} failed all "
                f"{max_attempts} attempts: {last_exc} — returning empty series"
            )
        return pd.Series(dtype="float64", name=alpha_id)

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
    ) -> Tuple[Optional[float], CorrSource]:
        """Compute max self-correlation against cached OS alphas.

        Returns: (corr_value, source) where source ∈ CorrSource.
        - CorrSource.LOCAL: corr_value is a real float measured against the pool.
        - CorrSource.UNKNOWN: corr_value is **None** — the value could NOT be
          measured (no cache / no PnL / insufficient overlap / all-NaN). The
          old contract returned 0.0 here, which downstream gates could not
          distinguish from "measured and genuinely uncorrelated". Callers MUST
          branch on source, never trust a 0.0. (V-27.158: was the ad-hoc
          string "empty".)
        """
        cache = self._load_cache(region)
        if not cache or not cache.get("alpha_ids"):
            return None, CorrSource.UNKNOWN

        if alpha_pnl_series is None:
            alpha_pnl_series = await self._fetch_pnl_series(alpha_id)
        if alpha_pnl_series.empty:
            return None, CorrSource.UNKNOWN

        target_returns = _series_to_returns(alpha_pnl_series)
        if len(target_returns.dropna()) < MIN_OVERLAP_DAYS:
            logger.debug(
                f"[CorrelationService] {alpha_id} has {len(target_returns.dropna())} "
                f"days < {MIN_OVERLAP_DAYS}; cannot measure (insufficient sample)"
            )
            return None, CorrSource.UNKNOWN

        os_returns = cache["pnls"].apply(
            lambda col: col - col.ffill().shift(1), axis=0
        )
        cutoff = os_returns.index.max() - pd.DateOffset(years=LOOKBACK_YEARS)
        os_returns = os_returns[os_returns.index > cutoff]

        # Drop the target alpha if it happens to already be in the OS cache
        if alpha_id in os_returns.columns:
            os_returns = os_returns.drop(columns=[alpha_id])

        if os_returns.shape[1] == 0:
            return None, CorrSource.UNKNOWN

        corrs = os_returns.corrwith(target_returns)
        max_corr = corrs.max(skipna=True)
        if pd.isna(max_corr):
            # Every pairwise corr was NaN — no overlapping observations with
            # any pool member. Not measurable; do NOT report 0.0.
            return None, CorrSource.UNKNOWN
        return float(max_corr), CorrSource.LOCAL

    # ------------------------------------------------------------------
    # Public entry: three-tier fallback
    # ------------------------------------------------------------------

    async def get_with_fallback(
        self,
        alpha_id: str,
        region: str = "USA",
    ) -> Tuple[Optional[float], CorrSource]:
        """Three-tier resolver. Returns (corr, source) where source ∈ CorrSource.

          - CorrSource.LOCAL         — corr is a real float from the local PnL cache
          - CorrSource.BRAIN         — corr is a real float from BRAIN /correlations/SELF
          - CorrSource.BRAIN_PENDING — corr is **None**; BRAIN accepted the
            request but the correlation is still computing (V-27.126). Distinct
            from UNKNOWN — a caller that can wait may retry.
          - CorrSource.UNKNOWN       — corr is **None**; both tiers failed. Caller
            MUST NOT treat this as PASS or as "uncorrelated" — it means "not
            measured". The old contract returned 0.0, which silently looked
            like a safe alpha to any `corr < threshold` check.
        """
        try:
            corr, src = await self.calc_self_corr(alpha_id, region)
            if src == CorrSource.LOCAL and corr is not None:
                return corr, CorrSource.LOCAL
        except Exception as e:
            logger.warning(f"[CorrelationService] local calc failed for {alpha_id}: {e}")

        try:
            res = await self.brain.check_correlation(alpha_id, check_type="SELF")
            # P3-Brain (2026-05-16): check_correlation now returns
            # {"status_code": int, "data": {...}}. Legacy fakes/mocks may still
            # return the bare payload {"max": ...} — accept both shapes so
            # in-tree fake brains and the real adapter share this code path.
            if isinstance(res, dict):
                data = res["data"] if "status_code" in res and isinstance(res.get("data"), dict) else res
                if data.get("max") is not None:
                    return float(data["max"]), CorrSource.BRAIN
                # V-27.126: well-formed dict but max=None — BRAIN accepted the
                # request and the correlation job is still computing. Report
                # BRAIN_PENDING (not UNKNOWN) so a caller that can wait may
                # retry. submit_alpha's gate-4 is None-safe for both.
                return None, CorrSource.BRAIN_PENDING
        except Exception as e:
            logger.warning(f"[CorrelationService] BRAIN /correlations/SELF failed for {alpha_id}: {e}")

        return None, CorrSource.UNKNOWN

    # ------------------------------------------------------------------
    # Crisis-window stress test
    # ------------------------------------------------------------------

    async def calc_self_corr_by_window(
        self,
        alpha_id: str,
        region: str,
        alpha_pnl_series: Optional[pd.Series] = None,
        windows: Optional[List[str]] = None,
    ) -> Dict[str, Dict]:
        """Compute the new alpha's max correlation against the OS pool, sliced
        per crisis window.

        Differs from `calc_self_corr` in two ways:
          1. Drops the LOOKBACK_YEARS cutoff — we want older data when a
             crisis window predates the trailing-4y range.
          2. Uses MIN_OVERLAP_DAYS_PER_WINDOW (20) instead of the 60-day
             default since each window is itself short.

        Returns: `{window_name: {max_corr, overlap_days, counterpart_id,
                                 status}}` where status ∈
        {"ok", "insufficient_data", "empty_pool", "missing_window"}.
        """
        cache = self._load_cache(region)
        if not cache or not cache.get("alpha_ids"):
            return {w: {"status": "empty_pool"} for w in (windows or CRISIS_WINDOWS)}

        if alpha_pnl_series is None:
            alpha_pnl_series = await self._fetch_pnl_series(alpha_id)
        if alpha_pnl_series.empty:
            return {w: {"status": "empty_pool"} for w in (windows or CRISIS_WINDOWS)}

        # Full-history returns — DO NOT apply LOOKBACK_YEARS here.
        target_returns_full = alpha_pnl_series - alpha_pnl_series.ffill().shift(1)
        os_returns_full = _pnls_to_returns_df(cache["pnls"])
        if alpha_id in os_returns_full.columns:
            os_returns_full = os_returns_full.drop(columns=[alpha_id])

        out: Dict[str, Dict] = {}
        for window in (windows or list(CRISIS_WINDOWS.keys())):
            if window not in CRISIS_WINDOWS:
                out[window] = {"status": "missing_window"}
                continue

            # V-27.124 / V-27.138: keep the RAW window slice for correlation
            # alignment — do NOT pre-dropna it. The old code dropna'd target
            # first, then `os_w.corrwith(target_w)` aligned every OS column
            # onto target's now-sparse index, collapsing pairwise overlap
            # (compounded by os columns' own first-day NaN). Result: per-window
            # corr systematically too low → "crisis convergence" alerts
            # essentially could not fire. The dropna'd copy is still used —
            # only as the target's own validity gate.
            target_w_raw = _slice_returns_to_window(target_returns_full, window)
            target_valid = target_w_raw.dropna()
            if len(target_valid) < MIN_OVERLAP_DAYS_PER_WINDOW:
                out[window] = {
                    "status": "insufficient_data",
                    "overlap_days": int(len(target_valid)),
                }
                continue

            os_w = _slice_returns_to_window(os_returns_full, window)
            if os_w.empty or os_w.shape[1] == 0:
                out[window] = {"status": "empty_pool"}
                continue

            # Per-column pairwise-complete correlation: a day is dropped only
            # when THAT specific pair is NaN, not globally. Each column must
            # clear MIN_OVERLAP_DAYS_PER_WINDOW *actual overlapping* obs.
            corr_by_col: Dict[str, float] = {}
            overlap_by_col: Dict[str, int] = {}
            for col in os_w.columns:
                pair = pd.concat([target_w_raw, os_w[col]], axis=1).dropna()
                if len(pair) < MIN_OVERLAP_DAYS_PER_WINDOW:
                    continue
                c = pair.iloc[:, 0].corr(pair.iloc[:, 1])
                if not pd.isna(c):
                    corr_by_col[col] = float(c)
                    overlap_by_col[col] = int(len(pair))

            if not corr_by_col:
                out[window] = {
                    "status": "insufficient_data",
                    "overlap_days": int(len(target_valid)),
                }
                continue

            max_idx = max(corr_by_col, key=corr_by_col.__getitem__)
            out[window] = {
                "status": "ok",
                "max_corr": corr_by_col[max_idx],
                # Report the winning pair's actual overlap, not the (misleading)
                # target-only day count.
                "overlap_days": overlap_by_col[max_idx],
                "counterpart_id": str(max_idx),
            }

        return out

    async def compute_pairwise_corr_for_ids(
        self,
        alpha_ids: List[str],
        *,
        min_overlap_days: int = MIN_OVERLAP_DAYS,
        max_alphas: int = 50,
    ) -> Optional["pd.DataFrame"]:
        """Build an in-round pairwise daily-return correlation matrix for
        a SPECIFIC set of alpha_ids (Phase 4 R10-v2 upstream wire, Tier A).

        Distinct from ``compute_portfolio_matrix`` (which works off the
        cached OS pool) — this fetches PnL for the exact round alpha_ids
        passed in (typically only same-family members from
        ``family_classifier.same_family_alpha_ids``, so the fetch count
        is bounded and usually 0).

        Returns a pandas DataFrame indexed by alpha_id on both axes
        (``DataFrame.corr(min_periods=min_overlap_days)``), or None when:
          - alpha_ids has < 2 entries
          - BRAIN_AUTH_CIRCUIT is open (fast-fail, no fetch stampede)
          - < 2 alphas yielded a non-empty PnL series

        Soft-fail: per-alpha fetch errors are skipped (partial coverage
        is handled by apply_family_hard_ban's min_coverage_ratio guard).
        Bounded by max_alphas to cap worst-case BRAIN cost.
        """
        if not alpha_ids or len(alpha_ids) < 2:
            return None

        # F6 lesson (Sprint 4): short-circuit on BRAIN auth circuit open so
        # a dead session doesn't trigger a per-alpha retry stampede.
        try:
            from backend.adapters.brain_adapter import BRAIN_AUTH_CIRCUIT
            if BRAIN_AUTH_CIRCUIT.is_open():
                logger.info(
                    "[CorrelationService] R10-v2 corr matrix skipped — "
                    "BRAIN_AUTH_CIRCUIT open"
                )
                return None
        except Exception:  # noqa: BLE001
            pass

        # Dedup + cap
        uniq_ids = list(dict.fromkeys(str(a) for a in alpha_ids))[:max_alphas]
        sem = asyncio.Semaphore(PNL_FETCH_CONCURRENCY)

        # R2 review fix: in-round budget is tight — use max_attempts=1 (a
        # retry's value is low here vs the latency it adds) and re-check the
        # auth circuit before each fetch so a mid-gather auth drop fast-fails
        # the remaining fetches instead of each one burning a retry/backoff.
        try:
            from backend.adapters.brain_adapter import BRAIN_AUTH_CIRCUIT as _BAC
        except Exception:  # noqa: BLE001
            _BAC = None

        async def _fetch(aid: str) -> Optional[pd.Series]:
            async with sem:
                if _BAC is not None and _BAC.is_open():
                    return None
                try:
                    s = await self._fetch_pnl_series(aid, max_attempts=1)
                    return s if (s is not None and not s.empty) else None
                except Exception as e:  # noqa: BLE001
                    logger.debug(
                        f"[CorrelationService] R10-v2 PnL fetch failed {aid}: {e}"
                    )
                    return None

        results = await asyncio.gather(*(_fetch(a) for a in uniq_ids))
        series = [s for s in results if s is not None and not s.empty]
        if len(series) < 2:
            return None

        pnl_df = pd.concat(series, axis=1)
        pnl_df = pnl_df.loc[:, ~pnl_df.columns.duplicated()]
        # daily returns then pairwise corr (mirror calibrate_r10 convention)
        returns = pnl_df - pnl_df.ffill().shift(1)
        corr_df = returns.corr(min_periods=min_overlap_days)
        return corr_df

    def compute_portfolio_matrix(
        self,
        region: str,
        window: Optional[str] = None,
    ) -> Dict:
        """Compute the full pairwise correlation matrix over the OS pool.

        Unlike `calc_self_corr*` which compares a new alpha against the pool,
        this returns the N×N matrix of OS-vs-OS correlations so the user can
        see portfolio-level concentration.

        Args:
            region: BRAIN region code.
            window: optional crisis-window name. If None, uses the standard
                    LOOKBACK_YEARS=4 window.

        Returns: dict with keys:
            - alpha_ids: List[str]
            - matrix: List[List[float]] (NaN-filled where overlap < floor)
            - window: str | None
            - n_obs: int (rows after slicing, before column-wise dropna)
            - n_alphas: int
            - status: "ok" | "empty" | "missing_window"
        """
        cache = self._load_cache(region)
        if not cache or not cache.get("alpha_ids"):
            return {"status": "empty", "window": window}

        returns = _pnls_to_returns_df(cache["pnls"])

        if window is None:
            cutoff = returns.index.max() - pd.DateOffset(years=LOOKBACK_YEARS)
            returns = returns[returns.index > cutoff]
            overlap_floor = MIN_OVERLAP_DAYS
        else:
            if window not in CRISIS_WINDOWS:
                return {"status": "missing_window", "window": window}
            returns = _slice_returns_to_window(returns, window)
            overlap_floor = MIN_OVERLAP_DAYS_PER_WINDOW

        if returns.empty:
            return {"status": "empty", "window": window}

        # Drop columns where the alpha has < floor non-NaN obs in this slice.
        valid_cols = [c for c in returns.columns if returns[c].dropna().shape[0] >= overlap_floor]
        returns = returns[valid_cols]

        if returns.shape[1] < 2:
            return {
                "status": "empty",
                "window": window,
                "alpha_ids": valid_cols,
                "n_obs": int(returns.shape[0]),
                "n_alphas": int(returns.shape[1]),
            }

        # pairwise correlation; NaN where insufficient overlap.
        corr_df = returns.corr(min_periods=overlap_floor)

        # Replace NaN with None so JSON serialization works downstream.
        matrix = [
            [None if pd.isna(v) else float(v) for v in row]
            for row in corr_df.values
        ]

        return {
            "status": "ok",
            "window": window,
            "alpha_ids": list(corr_df.columns),
            "matrix": matrix,
            "n_obs": int(returns.shape[0]),
            "n_alphas": int(corr_df.shape[0]),
        }

    def crisis_stress_test(
        self,
        region: str,
        top_n_hotspots: int = 20,
        hotspot_threshold: float = CRISIS_HOTSPOT_THRESHOLD,
    ) -> Dict:
        """Run the full crisis stress test over the cached OS pool.

        For every crisis window:
          - Compute the pairwise corr matrix on that slice.
          - Record max / median / mean pairwise corr (off-diagonal).
          - Extract pairs whose corr exceeds `hotspot_threshold` ranked by
            severity.

        Also computes the BASELINE (full-LOOKBACK_YEARS) version for
        comparison so the caller can see the "calm vs stress" delta.

        Returns: dict with `baseline` and `windows` keys. Each window entry:
            {
              status, n_alphas, n_obs,
              max_pairwise, median_pairwise, mean_pairwise,
              hotspots: [{a, b, corr}, ...]
            }
        """
        cache = self._load_cache(region)
        if not cache or not cache.get("alpha_ids"):
            return {"status": "empty", "windows": {}, "baseline": {"status": "empty"}}

        def _summarize(matrix_payload: Dict) -> Dict:
            if matrix_payload.get("status") != "ok":
                return {
                    "status": matrix_payload.get("status", "empty"),
                    "n_alphas": matrix_payload.get("n_alphas", 0),
                    "n_obs": matrix_payload.get("n_obs", 0),
                }
            ids = matrix_payload["alpha_ids"]
            mat = matrix_payload["matrix"]
            n = len(ids)
            offdiag: List[float] = []
            hotspots: List[Dict] = []
            for i in range(n):
                for j in range(i + 1, n):
                    v = mat[i][j]
                    if v is None:
                        continue
                    offdiag.append(v)
                    if v >= hotspot_threshold:
                        hotspots.append({"a": ids[i], "b": ids[j], "corr": v})
            hotspots.sort(key=lambda x: x["corr"], reverse=True)
            if offdiag:
                ser = pd.Series(offdiag)
                summary = {
                    "max_pairwise": float(ser.max()),
                    "median_pairwise": float(ser.median()),
                    "mean_pairwise": float(ser.mean()),
                }
            else:
                summary = {
                    "max_pairwise": None,
                    "median_pairwise": None,
                    "mean_pairwise": None,
                }
            return {
                "status": "ok",
                "n_alphas": n,
                "n_obs": matrix_payload["n_obs"],
                "hotspots": hotspots[:top_n_hotspots],
                **summary,
            }

        baseline = _summarize(self.compute_portfolio_matrix(region, window=None))
        windows = {
            name: _summarize(self.compute_portfolio_matrix(region, window=name))
            for name in CRISIS_WINDOWS
        }

        return {
            "status": "ok",
            "region": region,
            "computed_at": datetime.utcnow().isoformat(),
            "hotspot_threshold": hotspot_threshold,
            "baseline": baseline,
            "windows": windows,
        }

    # ------------------------------------------------------------------
    # Snapshot persistence
    # ------------------------------------------------------------------

    def save_crisis_snapshot(self, region: str, payload: Dict) -> Path:
        """Persist crisis stress-test output to disk for the UI / audit log."""
        path = CACHE_DIR / f"crisis_corr_{region}.json"
        # V-27.130: atomic write — the 06:30 beat and a user-triggered
        # GET /crisis-summary?refresh=1 can both write crisis_corr_{region}.json
        # concurrently while load_crisis_snapshot reads it. A direct open("w")
        # leaves a half-written JSON visible to the reader (json.load throws,
        # gets swallowed, UI shows empty). tmp-then-rename makes the swap
        # atomic on POSIX and Windows (Path.replace).
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, default=str)
            tmp.replace(path)
        except Exception as e:
            logger.error(f"[CorrelationService] Failed to save crisis snapshot {path}: {e}")
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
        return path

    def load_crisis_snapshot(self, region: str) -> Optional[Dict]:
        """Load the last persisted crisis stress-test snapshot, if any."""
        path = CACHE_DIR / f"crisis_corr_{region}.json"
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"[CorrelationService] Failed to load crisis snapshot {path}: {e}")
            return None
