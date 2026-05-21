"""Backfill meta_data['dataset_categories_used'] on SUCCESS_PATTERN KB rows (2026-05-21).

The 2026-05-21 RAG redesign keys step-1 retrieval on the SET of dataset-categories
an alpha's fields touch (set-overlap), not a single anchor dataset_id. Going-forward
``record_success_pattern`` stamps this; this script backfills the existing corpus so
the new L1 has signal on legacy rows too.

Derivation priority per row (highest-fidelity first):
  1. ``meta_data['alpha_id']`` / ``['alpha_id_ref']`` → JOIN ``alphas.fields_used``
     (the already-extracted, operator-stripped field set) → datafields → category set.
  2. ``meta_data['example_expression']`` (concrete) → extract fields → datafields.
  3. concrete ``pattern`` (any source, NOT a skeleton with FIELD/NUM placeholders)
     → extract fields → datafields.
  4. skeleton-only / nothing resolves → leave empty (no keyword guessing → no false
     positives; retrieval degrades gracefully via the L1 0-candidate fallback).

Safety:
  - SUCCESS_PATTERN only (failure rows ~95% lack resolvable signal — out of scope v1).
  - meta_data-only write → does NOT touch pattern_hash → no dedup/UNIQUE risk.
  - Keyed by row ``id`` (many legacy rows have NULL pattern_hash).
  - ``flag_modified`` so the in-place JSONB mutation persists.
  - Idempotent: rows that already have a non-empty dataset_categories_used are skipped
    (unless --force). Provenance stamped (dataset_categories_source / backfill_batch).
  - datafields catalog is USA-only → non-USA rows resolve to [] (acknowledged gap).

Usage:
    python scripts/backfill_kb_dataset_categories.py            # dry-run (default)
    python scripts/backfill_kb_dataset_categories.py --apply
    python scripts/backfill_kb_dataset_categories.py --apply --force   # re-derive all
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from backend.database import AsyncSessionLocal
from backend.models import Alpha, KnowledgeEntry
from backend.agents.services.rag_service import resolve_field_categories

_BACKFILL_BATCH = "2026-05-21-catset-v1"


def _row_region(md: dict) -> str:
    r = md.get("region")
    if not r:
        regs = md.get("regions") or []
        r = regs[0] if regs else None
    return (str(r).upper() if r else "USA")


def _is_skeleton(pattern: str) -> bool:
    """expression_to_skeleton replaces leaf fields with the literal token FIELD
    and numbers with NUM — such a pattern has no concrete field to resolve."""
    if not pattern:
        return True
    return ("FIELD" in pattern) or ("NUM" in pattern)


async def _backfill(apply: bool, force: bool) -> dict:
    stats = Counter()
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(KnowledgeEntry).where(
                KnowledgeEntry.entry_type == "SUCCESS_PATTERN",
                KnowledgeEntry.is_active == True,  # noqa: E712
            )
        )).scalars().all()
        stats["total_success_patterns"] = len(rows)

        # Pre-load the alphas referenced by these rows (tier 1 join source).
        alpha_ids = set()
        for r in rows:
            md = r.meta_data if isinstance(r.meta_data, dict) else {}
            for k in ("alpha_id", "alpha_id_ref"):
                v = md.get(k)
                if v:
                    alpha_ids.add(str(v))
        alpha_lookup: dict = {}
        if alpha_ids:
            a_rows = (await db.execute(
                select(Alpha.alpha_id, Alpha.fields_used, Alpha.region)
                .where(Alpha.alpha_id.in_(list(alpha_ids)))
            )).all()
            for aid, fields_used, region in a_rows:
                alpha_lookup[str(aid)] = (fields_used or [], region)

        before_nonempty = 0
        unresolved_fields_sample: set = set()

        for r in rows:
            md = dict(r.meta_data) if isinstance(r.meta_data, dict) else {}
            existing = md.get("dataset_categories_used")
            if existing:
                before_nonempty += 1
                if not force:
                    stats["skip_already_stamped"] += 1
                    continue

            region = _row_region(md)
            cats: list = []
            tier = None

            # Tier 1 — alpha_id(/ref) → alphas.fields_used
            aid = md.get("alpha_id") or md.get("alpha_id_ref")
            if aid and str(aid) in alpha_lookup:
                fields_used, a_region = alpha_lookup[str(aid)]
                if fields_used:
                    cats = await resolve_field_categories(
                        fields_used, (a_region or region), db
                    )
                    if cats:
                        tier = "alpha_fields"

            # Tier 2 — example_expression (concrete)
            if not cats:
                ex = md.get("example_expression")
                if ex and not _is_skeleton(ex):
                    cats = await resolve_field_categories(ex, region, db)
                    if cats:
                        tier = "example_expr"

            # Tier 3 — concrete pattern (any source)
            if not cats and r.pattern and not _is_skeleton(r.pattern):
                cats = await resolve_field_categories(r.pattern, region, db)
                if cats:
                    tier = "concrete_pattern"

            if cats:
                stats[f"tier_{tier}"] += 1
                stats["resolved"] += 1
                if apply:
                    md["dataset_categories_used"] = cats
                    md["dataset_categories_source"] = tier
                    md["backfill_batch"] = _BACKFILL_BATCH
                    r.meta_data = md
                    flag_modified(r, "meta_data")
            else:
                stats["unresolved_empty"] += 1
                # sample a few unresolved field tokens for the dry-run report
                if len(unresolved_fields_sample) < 25 and r.pattern and not _is_skeleton(r.pattern):
                    from backend.agents.hierarchical_rag import extract_fields_for_rag
                    for f in extract_fields_for_rag(r.pattern)[:3]:
                        unresolved_fields_sample.add(f)

        if apply:
            await db.commit()

    after_nonempty = before_nonempty + (stats["resolved"] if apply else 0)
    return {
        "total_success_patterns": stats["total_success_patterns"],
        "nonempty_before": before_nonempty,
        "resolved_this_run": stats["resolved"],
        "tier_alpha_fields": stats["tier_alpha_fields"],
        "tier_example_expr": stats["tier_example_expr"],
        "tier_concrete_pattern": stats["tier_concrete_pattern"],
        "unresolved_empty": stats["unresolved_empty"],
        "skip_already_stamped": stats["skip_already_stamped"],
        "coverage_before_pct": round(100.0 * before_nonempty / max(1, stats["total_success_patterns"]), 1),
        "coverage_after_pct": round(100.0 * after_nonempty / max(1, stats["total_success_patterns"]), 1) if apply else None,
        "unresolved_field_sample": sorted(unresolved_fields_sample)[:25],
        "applied": apply,
        "force": force,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill dataset_categories_used on SUCCESS_PATTERN KB rows"
    )
    parser.add_argument("--apply", action="store_true", help="actually write (default: dry-run)")
    parser.add_argument("--force", action="store_true",
                        help="re-derive even rows that already have dataset_categories_used")
    args = parser.parse_args()

    result = asyncio.run(_backfill(apply=args.apply, force=args.force))

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== backfill_kb_dataset_categories [{mode}] (force={args.force}) ===")
    for k, v in result.items():
        print(f"  {k}: {v}")
    if not args.apply:
        print(f"  → would stamp {result['resolved_this_run']} rows "
              f"(coverage {result['coverage_before_pct']}% → projected "
              f"{round(100.0*(result['nonempty_before']+result['resolved_this_run'])/max(1,result['total_success_patterns']),1)}%). "
              f"Re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
