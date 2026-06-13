#!/usr/bin/env python
"""Backfill alphas.dataset_id by parsing fields out of the expression text.

Companion to scripts/backfill_alpha_dataset_id.py — that one relies on
fields_used being a populated array, which BRAIN-imported alphas don't have
(fields_used=[] for ~4998 rows). This one tokenizes the expression and
intersects with the datafield catalog (universe-invariant per region) to
recover fields, then reuses backend.dataset_attribution.derive_dataset_id
for dominant-dataset attribution.

Only fills rows where the derived dataset is unique among contributing fields.
Conflicts (cross-dataset) keep dataset_id NULL, same policy as the live
inline stamper in persistence._incremental_save_alphas.

Usage:
    python scripts/backfill_alpha_dataset_id_expr.py                # dry-run
    python scripts/backfill_alpha_dataset_id_expr.py --apply        # write
    python scripts/backfill_alpha_dataset_id_expr.py --apply --batch 500
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from collections import Counter, defaultdict
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.dataset_attribution import build_field_dataset_map, derive_dataset_id  # noqa: E402

# Identifiers start with letter or underscore — excludes pure numeric constants
# and quoted-string args. Operator names (`ts_zscore`, `rank`, `vec_avg`, …)
# will be in this set too but get filtered out by the intersection with the
# datafield catalog (only real field_ids survive).
TOKEN_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")


def extract_field_tokens(expression: str, field_map: Dict[str, str]) -> List[str]:
    if not expression:
        return []
    tokens = TOKEN_RE.findall(expression)
    return [t for t in tokens if t.lower() in field_map]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write UPDATEs (default: dry-run)")
    ap.add_argument("--batch", type=int, default=500, help="UPDATE batch size")
    ap.add_argument(
        "--update-fields-used",
        action="store_true",
        help="also write the extracted field list into alphas.fields_used",
    )
    ap.add_argument(
        "--backup-table",
        default="alphas_expr_backfill_backup_20260528",
        help="snapshot table for rows that get written (skipped if exists)",
    )
    args = ap.parse_args()

    from sqlalchemy import select, text, update

    from backend.database import AsyncSessionLocal
    from backend.models import Alpha

    print("=" * 72)
    print(f"Backfill alphas.dataset_id from expression  mode={'APPLY' if args.apply else 'DRY-RUN'}")
    print("=" * 72)

    async with AsyncSessionLocal() as db:
        # Candidate: dataset_id NULL AND expression non-empty AND fields_used empty.
        # We deliberately skip the 39 rows already handled by the fields_used-based
        # backfill (those have non-empty fields_used).
        stmt = select(
            Alpha.id, Alpha.region, Alpha.universe, Alpha.expression, Alpha.fields_used
        ).where(Alpha.dataset_id.is_(None), Alpha.expression.isnot(None))
        rows = (await db.execute(stmt)).all()
        # filter to those with empty fields_used (preserves the field-based path's domain)
        rows = [
            r for r in rows
            if r.expression and r.expression.strip()
            and (r.fields_used is None or (isinstance(r.fields_used, list) and len(r.fields_used) == 0))
        ]
        print(f"\ncandidate rows (NULL dataset_id, has expression, empty fields_used): {len(rows)}")
        if not rows:
            print("nothing to backfill.")
            return

        # Build field→dataset map per (region, universe) once.
        ru_keys = {(r.region, r.universe) for r in rows}
        maps: Dict[Tuple[str, str], Dict[str, str]] = {}
        for region, universe in sorted(ru_keys):
            maps[(region, universe)] = await build_field_dataset_map(db, region, universe)
            print(f"  field-map {region}/{universe}: {len(maps[(region, universe)])} fields")

        # Derive per row.
        derived: List[Tuple[int, str, List[str]]] = []  # (id, ds_name, fields_extracted)
        dist: Counter = Counter()
        unresolved_no_field = 0
        unresolved_no_match = 0
        for r in rows:
            fmap = maps.get((r.region, r.universe), {})
            if not fmap:
                unresolved_no_match += 1
                continue
            fields = extract_field_tokens(r.expression, fmap)
            if not fields:
                unresolved_no_field += 1
                continue
            ds = derive_dataset_id(fields, fmap)
            if ds:
                derived.append((r.id, ds, fields))
                dist[f"{r.region}:{ds}"] += 1
            else:
                unresolved_no_field += 1

        print(f"\nderived: {len(derived)}")
        print(f"unresolved — no field token in expression: {unresolved_no_field}")
        print(f"unresolved — empty field-map for region:   {unresolved_no_match}")
        print("\nderived dataset distribution (top 20):")
        for k, n in dist.most_common(20):
            print(f"  {k:<32} {n}")

        if not args.apply:
            print("\nDRY-RUN — no writes. Re-run with --apply to backfill.")
            return

        # Backup snapshot (skip if exists).
        backup_table = args.backup_table
        exists = (await db.execute(text(
            "SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=:t"
        ), {"t": backup_table})).first()
        if exists:
            print(f"\n[backup] {backup_table} already exists — skip snapshot")
        else:
            ids_to_backup = [aid for aid, _, _ in derived]
            await db.execute(text(
                f"CREATE TABLE {backup_table} AS SELECT * FROM alphas WHERE id = ANY(:ids)"
            ), {"ids": ids_to_backup})
            await db.commit()
            n_back = (await db.execute(text(f"SELECT COUNT(*) FROM {backup_table}"))).scalar()
            print(f"\n[backup] created {backup_table} ({n_back} rows)")

        # Apply in batches.
        n_written = 0
        for i in range(0, len(derived), args.batch):
            chunk = derived[i : i + args.batch]
            for aid, ds, fields in chunk:
                values: Dict[str, object] = {"dataset_id": ds}
                if args.update_fields_used:
                    # store deduped, lower-cased field list
                    seen: set[str] = set()
                    deduped: List[str] = []
                    for f in fields:
                        lf = f.lower()
                        if lf not in seen:
                            seen.add(lf)
                            deduped.append(lf)
                    values["fields_used"] = deduped
                await db.execute(update(Alpha).where(Alpha.id == aid).values(**values))
            await db.commit()
            n_written += len(chunk)
            print(f"  committed {n_written}/{len(derived)}")
        print(f"\nAPPLIED: backfilled dataset_id on {n_written} rows.")


if __name__ == "__main__":
    asyncio.run(main())
