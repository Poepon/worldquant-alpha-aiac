"""Refresh local OS PnL cache used for self-correlation precheck.

The cache (backend/data/correlation_cache/os_pnls_{region}.pkl) drives
CorrelationService.calc_self_corr. Without periodic refresh, newly
submitted alphas don't enter the corr pool and precheck under-estimates
the max self-correlation against the user's portfolio.

Run sources:
- After each submit: scripts/submit_alpha.py auto-calls (post-success hook)
- Weekly: Celery beat (TODO) — see backend/celery_app.py
- Manual: this script

BRAIN delay note: a freshly-submitted alpha's /recordsets/pnl endpoint
may return empty for several hours to ~1 day. Refresh is idempotent and
will pick up the alpha on next call once PnL is populated.

Usage:
    python scripts/refresh_corr_cache.py
    python scripts/refresh_corr_cache.py --region USA
    python scripts/refresh_corr_cache.py --regions USA,CHN --full
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.adapters.brain_adapter import BrainAdapter
from backend.services.correlation_service import CorrelationService


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--region", type=str, default=None,
                   help="Single region (default: USA)")
    p.add_argument("--regions", type=str, default=None,
                   help="Comma-separated regions, overrides --region")
    p.add_argument("--full", action="store_true",
                   help="Full re-fetch (ignore existing cache); slow")
    args = p.parse_args()

    if args.regions:
        regions = [r.strip().upper() for r in args.regions.split(",")]
    elif args.region:
        regions = [args.region.upper()]
    else:
        regions = ["USA"]

    incremental = not args.full

    async with BrainAdapter() as adapter:
        await adapter.authenticate()
        svc = CorrelationService(adapter)
        for region in regions:
            print(f"\n=== Refreshing {region} (incremental={incremental}) ===")
            try:
                new_n, total_n = await svc.refresh_os_alpha_cache(
                    region=region, incremental=incremental,
                )
                print(f"  → {new_n} new PnL series fetched, {total_n} total in cache")
            except Exception as e:
                print(f"  ✗ PnL refresh failed: {e}")

            # P2: also refresh portfolio skeletons cache (DB-only, fast).
            try:
                from backend.agents.seed_pool.portfolio_skeletons import (
                    refresh_portfolio_from_db,
                )
                n = await refresh_portfolio_from_db(region=region)
                print(f"  → {n} portfolio skeletons cached")
            except Exception as e:
                print(f"  ✗ skeletons refresh failed: {e}")


if __name__ == "__main__":
    asyncio.run(main())
