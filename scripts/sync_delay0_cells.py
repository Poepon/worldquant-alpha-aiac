"""Sync delay-0 dataset + datafield cells from BRAIN (pre-req for native
delay-0 mining, ②/B 2026-05-26).

Native delay-0 mining needs the delay-0 field roster present in the DB
(datafield_cell_stats at delay=0) — it is genuinely DIFFERENT from delay-1
(the probe showed analyst4 delay-1 field IDs don't even exist at delay-0:
"unknown variable anl4_adjusted_netincome_ft"). This populates the (universe,
delay=0) cells so a delay-0 FLAT session's _get_dataset_fields(delay=0) has
fields to offer the LLM.

Reuses the production upsert/reconcile helpers (zero logic duplication, no
drift vs the delay-1 sync). Single BRAIN auth + single DB session; sequential
per-dataset to stay gentle on the /data-fields rate limit (we hit a 429 on
that endpoint earlier). Idempotent — re-running upserts the same cells.

Usage:
  venv/Scripts/python.exe scripts/sync_delay0_cells.py [--region USA] [--universe TOP3000]
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select

sys.path.insert(0, ".")

from backend.adapters.brain_adapter import BrainAdapter
from backend.database import AsyncSessionLocal
from backend.models import DatasetMetadata
from backend.tasks.sync_tasks import (
    _reconcile_dataset_fields,
    _upsert_dataset_def_and_cell,
)


async def main(region: str, universe: str, delay: int):
    print(f"=== delay-{delay} cell sync | region={region} universe={universe} ===", flush=True)
    async with AsyncSessionLocal() as db:
        async with BrainAdapter() as brain:
            datasets = await brain.get_datasets(region=region, delay=delay, universe=universe)
            print(f"BRAIN returned {len(datasets)} datasets at delay={delay}", flush=True)
            if not datasets:
                print("  (no datasets — nothing to sync)")
                return

            # 1) upsert dataset defs + per-(universe, delay) dataset cells
            new_def = upd_def = 0
            for ds in datasets:
                category = ds.get("category")
                if isinstance(category, dict):
                    category = category.get("id")
                subcategory = ds.get("subcategory")
                if isinstance(subcategory, dict):
                    subcategory = subcategory.get("id")
                def_created, _cell_created = await _upsert_dataset_def_and_cell(
                    db, ds, region=region, universe=universe, delay=delay,
                    category=category, subcategory=subcategory,
                )
                new_def += int(def_created)
                upd_def += int(not def_created)
            await db.commit()
            print(f"dataset defs: {new_def} new, {upd_def} existing | dataset cells upserted", flush=True)

            # 2) field cells per dataset (sequential — gentle on /data-fields 429)
            grand_total = 0
            for ds in datasets:
                dsid = ds.get("id")
                ddef = (await db.execute(select(DatasetMetadata).where(
                    DatasetMetadata.dataset_id == dsid, DatasetMetadata.region == region,
                ))).scalar_one_or_none()
                if ddef is None:
                    print(f"  [skip] {dsid}: no def row after upsert", flush=True)
                    continue
                fields = await brain.get_datafields(
                    dataset_id=dsid, region=region, delay=delay, universe=universe,
                )
                stats = await _reconcile_dataset_fields(
                    db, ddef, fields, region=region, universe=universe, delay=delay,
                )
                await db.commit()
                grand_total += stats["returned"]
                print(f"  {dsid:<16} returned={stats['returned']:>4} "
                      f"new={stats['new']:>4} updated/active={stats['updated']:>4}", flush=True)

    print(f"\n=== DONE | delay={delay} | {len(datasets)} datasets | "
          f"{grand_total} field rows from BRAIN ===", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="USA")
    ap.add_argument("--universe", default="TOP3000")
    ap.add_argument("--delay", type=int, default=0)
    args = ap.parse_args()
    asyncio.run(main(args.region, args.universe, args.delay))
