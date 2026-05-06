"""Backfill hypothesis denormalized stats for any rows whose
alpha_count/pass_count/sharpe_max are out of sync with the JOIN to
alphas.hypothesis_id.

V-19.5 (2026-05-06): pre-fix, _process_hypothesis_feedback ran refresh_stats
INSIDE node_save_results (before workflow's outer commit), so the JOIN saw
0 rows for the round that just landed. Post-fix the refresh runs
post-commit in workflow.run_with_persistence — but existing PROMOTED/ACTIVE
rows still have stale stats. This script recomputes them in one batch.

Usage:
    python scripts/backfill_hypothesis_stats.py
    python scripts/backfill_hypothesis_stats.py --dry-run
    python scripts/backfill_hypothesis_stats.py --hid 209,210,211,212
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import List, Optional

# Allow `python scripts/backfill_hypothesis_stats.py` from anywhere
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select


_PG_URL = "postgresql+asyncpg://postgres:postgres@localhost:5433/alpha_gpt"


async def main(only_hids: Optional[List[int]] = None, dry_run: bool = False) -> int:
    from backend.models import Hypothesis
    from backend.services.hypothesis_service import HypothesisService

    engine = create_async_engine(_PG_URL, echo=False)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        # Find candidates: hypotheses that should have stats but rows show 0
        if only_hids:
            stmt = select(Hypothesis).where(Hypothesis.id.in_(only_hids))
        else:
            # Heuristic: any non-ABANDONED row OR a PROMOTED row where stats
            # may be stale. We refresh ALL non-ABANDONED to be safe — query
            # is cheap and idempotent.
            stmt = select(Hypothesis).where(
                Hypothesis.status.in_(["PROPOSED", "ACTIVE", "PROMOTED"])
            )
        result = await s.execute(stmt)
        rows = list(result.scalars().all())
        print(f"Candidates to refresh: {len(rows)}")

        if not rows:
            return 0

        svc = HypothesisService(s)
        changed = []
        unchanged = []
        for h in rows:
            old = (h.alpha_count, h.pass_count, h.sharpe_max)
            stats = await svc.refresh_stats(h.id)
            new = (stats.alpha_count, stats.pass_count, stats.sharpe_max)
            if old != new:
                changed.append((h.id, old, new))
            else:
                unchanged.append(h.id)

        if not dry_run:
            await s.commit()
            print(f"COMMITTED {len(changed)} stat updates, {len(unchanged)} unchanged")
        else:
            await s.rollback()
            print(f"[DRY-RUN] would update {len(changed)} rows, {len(unchanged)} already accurate")

        for hid, old, new in changed:
            print(f"  hid={hid}: alpha_count {old[0]}→{new[0]} "
                  f"pass_count {old[1]}→{new[1]} sharpe_max {old[2]}→{new[2]}")

    await engine.dispose()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hid", type=str, default=None,
                    help="Comma-separated hypothesis IDs (default: all non-ABANDONED)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    only_hids = None
    if args.hid:
        only_hids = [int(x.strip()) for x in args.hid.split(",") if x.strip()]

    sys.exit(asyncio.run(main(only_hids=only_hids, dry_run=args.dry_run)))
