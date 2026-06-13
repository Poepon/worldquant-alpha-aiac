"""Sync datafield + dataset cells for multiple USA universes × delays in one
pass (2026-05-26). Reuses the production upsert/reconcile helpers (zero logic
duplication, no drift vs the delay-1 beat sync); single BRAIN auth + single DB
session; sequential per (universe, delay, dataset) to stay gentle on the
/data-fields rate limit (we hit 429s on big syncs). Idempotent — re-running
upserts the same cells.

Runs as a standalone process with a FRESH BrainAdapter, independent of the
(possibly degraded) FLAT mining worker.

Usage:
  venv/Scripts/python.exe scripts/sync_all_usa_cells.py \
      [--region USA] [--universes TOP1000,TOP500,TOP200,TOPSP500] [--delays 1,0]
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


async def _sync_one(db, brain, region: str, universe: str, delay: int):
    datasets = await brain.get_datasets(region=region, delay=delay, universe=universe)
    if not datasets:
        print(f"  [{universe} d{delay}] no datasets (unavailable for this account)", flush=True)
        return 0, 0
    # 1) dataset defs + per-(universe, delay) dataset cells
    for ds in datasets:
        cat = ds.get("category")
        cat = cat.get("id") if isinstance(cat, dict) else cat
        sub = ds.get("subcategory")
        sub = sub.get("id") if isinstance(sub, dict) else sub
        await _upsert_dataset_def_and_cell(
            db, ds, region=region, universe=universe, delay=delay,
            category=cat, subcategory=sub,
        )
    await db.commit()
    # 2) field cells per dataset (sequential — gentle on /data-fields 429)
    total = 0
    for ds in datasets:
        dsid = ds.get("id")
        ddef = (await db.execute(select(DatasetMetadata).where(
            DatasetMetadata.dataset_id == dsid, DatasetMetadata.region == region,
        ))).scalar_one_or_none()
        if ddef is None:
            continue
        fields = await brain.get_datafields(
            dataset_id=dsid, region=region, delay=delay, universe=universe,
        )
        stats = await _reconcile_dataset_fields(
            db, ddef, fields, region=region, universe=universe, delay=delay,
        )
        await db.commit()
        total += stats["returned"]
    print(f"  [{universe} d{delay}] {len(datasets)} datasets, {total} field rows synced", flush=True)
    return len(datasets), total


async def main(region: str, universes: list[str], delays: list[int]):
    print(f"=== multi-universe cell sync | region={region} | universes={universes} | delays={delays} ===", flush=True)
    grand = 0
    async with AsyncSessionLocal() as db:
        async with BrainAdapter() as brain:
            for u in universes:
                for d in delays:
                    _, t = await _sync_one(db, brain, region, u, d)
                    grand += t
    print(f"=== DONE | {len(universes)} universes × {len(delays)} delays | {grand} field rows total ===", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="USA")
    ap.add_argument("--universes", default="TOP1000,TOP500,TOP200,TOPSP500")
    ap.add_argument("--delays", default="1,0")
    args = ap.parse_args()
    _univs = [u.strip() for u in args.universes.split(",") if u.strip()]
    _delays = [int(x.strip()) for x in args.delays.split(",") if x.strip()]
    asyncio.run(main(args.region, _univs, _delays))
