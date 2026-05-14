"""Refresh the OS-stage PnL cache used by CorrelationService.

The cache lives at backend/data/correlation_cache/os_pnls_{region}.pkl and
backs the local-first self-correlation gate (see backend/services/
correlation_service.py). It's stale by default because nothing keeps it
warm — when newly submitted alphas land in the OS pool they don't appear
in the cache until this script runs.

Usage:
    python scripts/refresh_os_corr_cache.py                 # USA incremental
    python scripts/refresh_os_corr_cache.py --region CHN    # specific region
    python scripts/refresh_os_corr_cache.py --full          # full rebuild

`--full` discards the existing pickle and refetches every OS alpha's PnL
(~500 fetches at PNL_FETCH_CONCURRENCY=10, takes a few minutes). The
default incremental mode only fetches alphas that aren't already cached.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loguru import logger  # noqa: E402

from backend.adapters.brain_adapter import BrainAdapter  # noqa: E402
from backend.services.correlation_service import CorrelationService  # noqa: E402


async def main(region: str, full: bool) -> int:
    async with BrainAdapter() as brain:
        svc = CorrelationService(brain)
        added, total = await svc.refresh_os_alpha_cache(
            region=region,
            incremental=not full,
        )
    logger.info(
        f"[refresh_os_corr_cache] region={region} added={added} total={total}"
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default="USA")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Discard existing cache and rebuild from scratch",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.region, args.full)))
