"""One-shot backfill: pattern_hash + requires_role + import_batch + pattern_operators
for all existing knowledge_entries rows that pre-date Phase 0 v1.5.

Why this script exists:
  - MF-8 (v1.2 review): `backend/agents/knowledge_seed.py:471-486` and
    `backend/external_knowledge.py:466-491` (pre-Q1-4) both omitted
    `pattern_hash`, so existing rows have NULL hash. Without this backfill,
    Q1-4's new `_pattern_hash_exists(hash)` dedupe would re-import the same
    101 patterns whose existing hash is NULL.
  - SF-12 + SF-FC (v1.3 + v1.5): every legacy row needs the new permanent
    audit hooks — `import_batch="legacy_seed"`, `requires_role="both"` (most
    permissive, since the user/consultant distinction is unverifiable today),
    and `pattern_operators` (a sorted list of operator names extracted from
    the pattern text by `parse_pattern_operators` — used by future
    role-recategorization without rewriting any alpha text).

Atomicity (SF-13):
  Per-row UPDATE writes pattern_hash + the three meta_data keys in ONE
  statement. PostgreSQL guarantees row-level atomicity, so a crash mid-run
  leaves each row either fully backfilled or fully untouched. The script is
  IDEMPOTENT: re-running skips rows that already have all four fields set.

Scope (SF-15):
  All `is_active=True` rows where ANY of {pattern_hash, requires_role,
  import_batch, pattern_operators} is missing. Includes:
    - knowledge_seed.py ALPHA_101_PATTERNS (10 rows, source='101_alphas')
    - external_knowledge.py _BASE_ACADEMIC_PATTERNS (5 rows, source='paper')
    - knowledge_seed.py CATEGORY/PITFALL/COMBO/REGION seed (~30+ rows,
      various sources) — all marked requires_role='both' (most permissive,
      they're not alpha sim targets so role distinction is moot).

Usage:
  python scripts/backfill_kb_requires_role.py [--dry-run]

Exit 0 on success; 1 if any row fails to UPDATE.

Plan reference: §2.5 v1.5 (MF-8 + SF-12 + SF-13 + SF-15)
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from backend.database import AsyncSessionLocal  # noqa: E402
from backend.external_knowledge import (  # noqa: E402
    LEGACY_SEED_IMPORT_BATCH,
    parse_pattern_operators,
)
from backend.models.knowledge import compute_pattern_hash  # noqa: E402


# Most-permissive role marker: legacy rows have no way to tell user vs
# consultant apart, so they default to 'both'. Future audit + hardcoded
# CONSULTANT_ONLY_OPERATORS allow downgrading specific rows.
DEFAULT_ROLE = "both"


async def fetch_backfill_targets() -> List[Tuple[int, str, str]]:
    """Return [(id, pattern, region), ...] for rows missing ANY of the 4 fields.

    MEDIUM-N1 fix (re-review a425937..HEAD): SELECT existing
    ``meta_data->>'region'`` so backfill_row can re-hash with the row's
    real region (mirroring the FixA pattern from commit 52fab57). Rows
    without a region default to "USA" — academically reasonable for
    legacy 101-Alpha / paper / forum sources.
    """
    sql = text("""
        SELECT id, pattern, COALESCE(meta_data ->> 'region', 'USA') AS region
        FROM knowledge_entries
        WHERE is_active = TRUE
          AND (
            pattern_hash IS NULL
            OR (meta_data ->> 'requires_role') IS NULL
            OR (meta_data ->> 'import_batch') IS NULL
            OR (meta_data -> 'pattern_operators') IS NULL
            OR (meta_data ->> 'region') IS NULL
          )
        ORDER BY id
    """)
    async with AsyncSessionLocal() as s:
        r = await s.execute(sql)
        return [
            (int(row[0]), row[1] or "", (row[2] or "USA").upper())
            for row in r.all()
        ]


async def backfill_row(
    row_id: int, pattern: str, region: str, *, dry_run: bool
) -> bool:
    """Atomically write all 5 fields to one row. Returns True on success.

    MEDIUM-N1 fix (re-review a425937..HEAD): pattern_hash is computed
    WITH region so cross-region rows don't collide on the global UNIQUE
    index. Because we match by ``id`` (PK) and UPDATE pattern_hash
    in-place via ``COALESCE`` only when NULL, this is idempotent: a row
    that was previously hashed with a different region keeps its old
    hash (no dup row created); a row with NULL hash gets the new
    region-scoped hash on first run.
    """
    region = (region or "USA").upper()
    phash = compute_pattern_hash(pattern, region, None)
    pops = parse_pattern_operators(pattern)
    # Per-row single UPDATE: PostgreSQL row-level atomicity guarantees the
    # 5 fields land together. ON CONFLICT not needed — id is PK, no
    # concurrent insert can collide here.
    # ::text casts are required because asyncpg's strict type inference can't
    # pick a `jsonb_build_object(text, anyelement)` overload from bare $N params.
    # MEDIUM-N1: also write meta_data["region"]/["regions"] so R8
    # hierarchical RAG L3 region filter (hierarchical_rag.py:228
    # dual-read) treats backfilled rows as region-scoped.
    sql = text("""
        UPDATE knowledge_entries
        SET
            pattern_hash = COALESCE(pattern_hash, cast(:phash as varchar)),
            meta_data = COALESCE(meta_data, '{}'::jsonb)
                || jsonb_build_object('requires_role', cast(:role as text))
                || jsonb_build_object('import_batch', cast(:batch as text))
                || jsonb_build_object('pattern_operators', cast(:pops_json as jsonb))
                || jsonb_build_object('region', cast(:region as text))
                || jsonb_build_object('regions', cast(:regions_json as jsonb))
        WHERE id = :id
    """)
    params = {
        "id": row_id,
        "phash": phash,
        "role": DEFAULT_ROLE,
        "batch": LEGACY_SEED_IMPORT_BATCH,
        "pops_json": __import__("json").dumps(pops),
        "region": region,
        "regions_json": __import__("json").dumps([region]),
    }
    if dry_run:
        print(f"  [dry-run] would UPDATE id={row_id} hash={phash[:8]}… "
              f"role={DEFAULT_ROLE} batch={LEGACY_SEED_IMPORT_BATCH} "
              f"region={region} pops={pops}")
        return True
    try:
        async with AsyncSessionLocal() as s:
            await s.execute(sql, params)
            await s.commit()
        return True
    except Exception as ex:
        print(f"  [error] id={row_id}: {ex}", file=sys.stderr)
        return False


async def main_async(args: argparse.Namespace) -> int:
    targets = await fetch_backfill_targets()
    print(f"Backfill targets: {len(targets)} rows missing one or more of "
          f"{{pattern_hash, requires_role, import_batch, pattern_operators}}")

    if not targets:
        print("All rows already backfilled — nothing to do.")
        return 0

    if args.dry_run:
        print("Running in --dry-run mode (no DB writes)")

    ok = 0
    failed = 0
    for row_id, pattern, region in targets:
        if await backfill_row(row_id, pattern, region, dry_run=args.dry_run):
            ok += 1
        else:
            failed += 1

    print(f"\nDone: {ok} OK, {failed} failed")

    if not args.dry_run:
        # Verify by re-querying targets
        remaining = await fetch_backfill_targets()
        print(f"Post-backfill remaining targets: {len(remaining)}")
        if remaining:
            print("WARN: some rows still missing fields — investigate", file=sys.stderr)
            for rid, _ in remaining[:10]:
                print(f"  id={rid}", file=sys.stderr)

    return 0 if failed == 0 else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would be UPDATEd without touching DB")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
