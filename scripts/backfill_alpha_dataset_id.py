#!/usr/bin/env python
"""(A) Backfill alphas.dataset_id for FLAT-path rows left NULL.

The hypothesis-driven FLAT path never set MiningState.dataset_id, so every
FLAT alpha persisted with dataset_id NULL — invisible to dataset-level
steering / field-screening / the bandit. This derives the dominant dataset
per alpha from its fields_used (via the datafields catalog) and fills the NULL
column. Companion to the (B) inline stamp in persistence._incremental_save_alphas
(both share backend/dataset_attribution.py); (A) covers the historical rows,
(B) covers new rows going forward.

Only touches rows where dataset_id IS NULL — never overwrites the ONESHOT
path's intentional values.

Usage:
    python scripts/backfill_alpha_dataset_id.py            # dry-run (no writes)
    python scripts/backfill_alpha_dataset_id.py --apply    # write UPDATEs
    python scripts/backfill_alpha_dataset_id.py --apply --batch 500
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter, defaultdict
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.dataset_attribution import build_field_dataset_map, derive_dataset_id  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write UPDATEs (default: dry-run)")
    ap.add_argument("--batch", type=int, default=500, help="UPDATE batch size")
    args = ap.parse_args()

    from sqlalchemy import select, update

    from backend.database import AsyncSessionLocal
    from backend.models import Alpha

    print("=" * 64)
    print(f"Backfill alphas.dataset_id  mode={'APPLY' if args.apply else 'DRY-RUN'}")
    print("=" * 64)

    async with AsyncSessionLocal() as db:
        # Candidate rows: dataset_id NULL but fields_used present.
        stmt = select(
            Alpha.id, Alpha.region, Alpha.universe, Alpha.fields_used
        ).where(Alpha.dataset_id.is_(None), Alpha.fields_used.isnot(None))
        rows = (await db.execute(stmt)).all()
        print(f"\nCandidate rows (dataset_id NULL, fields_used present): {len(rows)}")
        if not rows:
            print("nothing to backfill.")
            return

        # Build a field→dataset map per (region, universe) once.
        ru_keys = {(r.region, r.universe) for r in rows}
        maps: Dict[Tuple[str, str], Dict[str, str]] = {}
        for region, universe in ru_keys:
            maps[(region, universe)] = await build_field_dataset_map(db, region, universe)
            print(f"  field-map {region}/{universe}: {len(maps[(region, universe)])} fields")

        # Derive per row.
        derived: List[Tuple[int, str]] = []
        dist: Counter = Counter()
        unresolved = 0
        for r in rows:
            fu = r.fields_used if isinstance(r.fields_used, list) else (r.fields_used or [])
            ds = derive_dataset_id(fu, maps.get((r.region, r.universe), {}))
            if ds:
                derived.append((r.id, ds))
                dist[f"{r.region}:{ds}"] += 1
            else:
                unresolved += 1

        print(f"\nderived: {len(derived)}  unresolved (kept NULL): {unresolved}")
        print("derived dataset distribution (top 20):")
        for k, n in dist.most_common(20):
            print(f"  {k:<30} {n}")

        if not args.apply:
            print("\nDRY-RUN — no writes. Re-run with --apply to backfill.")
            return

        # Apply in batches.
        n_written = 0
        for i in range(0, len(derived), args.batch):
            chunk = derived[i : i + args.batch]
            for aid, ds in chunk:
                await db.execute(update(Alpha).where(Alpha.id == aid).values(dataset_id=ds))
            await db.commit()
            n_written += len(chunk)
            print(f"  committed {n_written}/{len(derived)}")
        print(f"\nAPPLIED: backfilled dataset_id on {n_written} rows.")


if __name__ == "__main__":
    asyncio.run(main())
