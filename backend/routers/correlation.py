"""Correlation matrix + crisis-window stress-test endpoints.

Three views over the OS PnL cache maintained by CorrelationService:

  GET /correlation/portfolio-matrix?region=USA[&window=covid_2020]
      Full N×N pairwise correlation of the OS pool. Optional `window`
      restricts to one of the named crisis windows.

  GET /correlation/crisis-summary?region=USA[&refresh=1]
      Combined baseline + per-window summary (max/median/mean pairwise +
      ranked hotspots). Cached on disk; pass `refresh=1` to recompute now.
      Refresh is also wired into the daily Celery beat at 06:30.

  GET /correlation/alpha/{alpha_id}/crisis?region=USA
      Per-window max-corr for a single new alpha against the cached OS
      pool. Use when deciding whether to submit an alpha — surfaces
      "looks calm but spikes in 2020-03" type risk.
"""

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from backend.adapters.brain_adapter import BrainAdapter
from backend.services.correlation_service import (
    CRISIS_WINDOWS,
    CorrelationService,
)

router = APIRouter(
    prefix="/correlation",
    tags=["correlation"],
)


@router.get("/windows")
async def list_crisis_windows():
    """Return the configured crisis windows. Cheap config endpoint."""
    return {
        name: {"start": start, "end": end}
        for name, (start, end) in CRISIS_WINDOWS.items()
    }


@router.get("/portfolio-matrix")
async def get_portfolio_matrix(
    region: str = Query("USA"),
    window: Optional[str] = Query(
        None,
        description="One of the crisis-window names, or omit for the full LOOKBACK_YEARS=4 view.",
    ),
):
    """Pairwise correlation matrix of the cached OS pool.

    Does not hit BRAIN; reads only the local pickle cache. If the cache
    is empty, returns `status: empty` rather than 404 — the UI should
    prompt the user to run `refresh_os_correlation_cache`.
    """
    if window and window not in CRISIS_WINDOWS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown window '{window}'. Valid: {list(CRISIS_WINDOWS)}",
        )

    # We don't need a live BRAIN session for cache-only reads, but the
    # CorrelationService constructor expects one. Pass an unauthenticated
    # adapter — the read path never touches the network.
    async with BrainAdapter() as brain:
        svc = CorrelationService(brain)
        return svc.compute_portfolio_matrix(region=region, window=window)


@router.get("/crisis-summary")
async def get_crisis_summary(
    region: str = Query("USA"),
    refresh: bool = Query(
        False,
        description="If true, recompute now instead of returning the on-disk snapshot.",
    ),
    top_n_hotspots: int = Query(20, ge=1, le=200),
):
    """Combined baseline + per-window stress test.

    By default returns the snapshot written by the daily Celery beat. Pass
    `refresh=1` to force a recompute (cheap — uses cached PnL).
    """
    async with BrainAdapter() as brain:
        svc = CorrelationService(brain)

        if not refresh:
            cached = svc.load_crisis_snapshot(region)
            if cached:
                return cached

        payload = svc.crisis_stress_test(region=region, top_n_hotspots=top_n_hotspots)
        if payload.get("status") == "ok":
            svc.save_crisis_snapshot(region, payload)
        return payload


@router.get("/alpha/{alpha_id}/crisis")
async def get_alpha_crisis_correlations(
    alpha_id: str,
    region: str = Query("USA"),
):
    """Per-window max-corr for a single alpha vs. the OS pool.

    Requires a live BRAIN session — fetches the alpha's PnL series before
    computing per-window correlations.
    """
    async with BrainAdapter() as brain:
        svc = CorrelationService(brain)
        try:
            by_window = await svc.calc_self_corr_by_window(
                alpha_id=alpha_id, region=region
            )
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"BRAIN PnL fetch failed: {e}")

    return {"alpha_id": alpha_id, "region": region, "by_window": by_window}
