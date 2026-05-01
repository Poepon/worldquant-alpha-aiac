"""Backfill apply step (PR2) — write the dryrun plan to the database.

Reads `backfill_plan.json` produced by backfill_factor_tier_dryrun.py and
applies the changes in order:
  1. alphas.factor_tier (batch UPDATE per tier)
  2. alphas.parent_alpha_id (per-row UPDATE for resolved links; orphans stay NULL)
  3. alphas.quality_status — routed through alpha_service.apply_quality_status_change
     so the audit log is written for every transition
  4. knowledge_entries.meta_data.alpha_id_ref (per-row JSONB merge for matched entries)

Two safety mechanisms (plan §12 — sandbox-aware):
  * --confirm flag is required before any UPDATE runs. Without it, the script
    just prints the plan summary and exits. This prevents accidental mass writes.
  * --dry-sql flag emits the SQL to stdout instead of executing — useful when
    the harness sandbox blocks bulk UPDATE on alphas.

Usage:
    # Print summary (no writes; sandbox-safe):
    python -m backend.migrations.backfill_factor_tier_apply

    # Apply directly to the dev DB:
    python -m backend.migrations.backfill_factor_tier_apply --confirm

    # Emit SQL for terminal-side execution if sandbox refuses:
    python -m backend.migrations.backfill_factor_tier_apply --dry-sql > apply.sql
    psql -U postgres -d alpha_gpt -f apply.sql
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Dict, Iterable, List

from loguru import logger
from sqlalchemy import text


def _chunked(items: List, n: int) -> Iterable[List]:
    """Yield successive n-sized chunks of items."""
    for i in range(0, len(items), n):
        yield items[i : i + n]


def _summarise(plan: Dict) -> None:
    print("=" * 70)
    print("Backfill APPLY plan summary")
    print("=" * 70)
    print(f"Generated: {plan.get('generated_at', '?')}")
    print()
    tier_changes = plan.get("tier_classification", {}).get("changes", [])
    quality_changes = plan.get("quality_recompute", {}).get("changes", [])
    parent_links = plan.get("parent_links", {}).get("parent_links", [])
    orphans = plan.get("parent_links", {}).get("orphans", [])
    kb_changes = plan.get("kb_alpha_id_ref", {}).get("changes", [])
    kb_matched = sum(1 for c in kb_changes if c.get("matched_alpha_id") is not None)

    print(f"  alphas.factor_tier updates: {len(tier_changes)}")
    print(f"  alphas.quality_status updates: {len(quality_changes)}")
    print(f"  alphas.parent_alpha_id resolved: {len(parent_links)}")
    print(f"  orphan T2/T3 (parent NULL): {len(orphans)}")
    print(f"  KB alpha_id_ref backfill: {kb_matched} of {len(kb_changes)} matched")


async def _apply(plan_path: Path, batch_size: int = 500) -> None:
    """Execute the plan. Each section is its own transaction."""
    from backend.database import AsyncSessionLocal
    from backend.services.alpha_service import AlphaService

    plan = json.loads(plan_path.read_text(encoding="utf-8"))

    async with AsyncSessionLocal() as db:
        # 1. factor_tier — group by new_tier so we can do batch UPDATE per tier
        tier_changes = plan.get("tier_classification", {}).get("changes", [])
        by_tier: Dict[int, List[int]] = {}
        for ch in tier_changes:
            t = ch.get("new_tier")
            if t is None:
                continue  # NULL tier — leave column NULL (default)
            by_tier.setdefault(t, []).append(ch["id"])
        for new_tier, ids in by_tier.items():
            for chunk in _chunked(ids, batch_size):
                await db.execute(
                    text("UPDATE alphas SET factor_tier = :t WHERE id = ANY(:ids)"),
                    {"t": new_tier, "ids": chunk},
                )
            logger.info(f"[backfill-apply] alphas.factor_tier → T{new_tier}: {len(ids)} rows")
        await db.commit()

        # 2. parent_alpha_id — resolved links
        parent_links = plan.get("parent_links", {}).get("parent_links", [])
        for link in parent_links:
            await db.execute(
                text("UPDATE alphas SET parent_alpha_id = :p WHERE id = :c"),
                {"p": link["parent_id"], "c": link["child_id"]},
            )
        if parent_links:
            await db.commit()
            logger.info(f"[backfill-apply] alphas.parent_alpha_id: {len(parent_links)} resolved")

        # 3. quality_status via apply_quality_status_change (audit log)
        quality_changes = plan.get("quality_recompute", {}).get("changes", [])
        alpha_service = AlphaService(db)
        applied = 0
        for ch in quality_changes:
            try:
                changed = await alpha_service.apply_quality_status_change(
                    alpha_id=ch["id"],
                    new_status=ch["new_status"],
                    reason=f"backfill_recalc: tier={ch.get('tier')} sharpe={ch.get('is_sharpe')}",
                    source="backfill",
                )
                if changed:
                    applied += 1
            except Exception as e:
                logger.warning(f"[backfill-apply] quality change {ch.get('id')} failed: {e}")
        await db.commit()
        logger.info(
            f"[backfill-apply] alphas.quality_status: {applied}/{len(quality_changes)} "
            "changes applied with audit log"
        )

        # 4. KB meta_data.alpha_id_ref backfill (JSONB merge)
        kb_changes = plan.get("kb_alpha_id_ref", {}).get("changes", [])
        kb_applied = 0
        for ch in kb_changes:
            if ch.get("matched_alpha_id") is None:
                continue
            await db.execute(
                text(
                    "UPDATE knowledge_entries "
                    "SET meta_data = COALESCE(meta_data, '{}'::jsonb) || "
                    "    jsonb_build_object('alpha_id_ref', :aid::int) "
                    "WHERE id = :kid"
                ),
                {"aid": ch["matched_alpha_id"], "kid": ch["kb_id"]},
            )
            kb_applied += 1
        if kb_applied:
            await db.commit()
            logger.info(f"[backfill-apply] knowledge_entries.alpha_id_ref: {kb_applied} rows")

    logger.info("[backfill-apply] done")


def _emit_sql(plan_path: Path) -> None:
    """Print SQL statements to stdout (sandbox-safe)."""
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    print("BEGIN;")
    print()

    # 1. factor_tier
    by_tier: Dict[int, List[int]] = {}
    for ch in plan.get("tier_classification", {}).get("changes", []):
        t = ch.get("new_tier")
        if t is None:
            continue
        by_tier.setdefault(t, []).append(ch["id"])
    for new_tier, ids in by_tier.items():
        ids_str = ",".join(str(i) for i in ids)
        print(f"-- T{new_tier}: {len(ids)} rows")
        print(f"UPDATE alphas SET factor_tier = {new_tier} WHERE id IN ({ids_str});")
    print()

    # 2. parent_alpha_id
    parent_links = plan.get("parent_links", {}).get("parent_links", [])
    for link in parent_links:
        print(
            f"UPDATE alphas SET parent_alpha_id = {link['parent_id']} "
            f"WHERE id = {link['child_id']};"
        )
    print()

    # 3. quality_status — emit UPDATEs but note audit log won't be populated this way
    quality_changes = plan.get("quality_recompute", {}).get("changes", [])
    if quality_changes:
        print("-- WARNING: SQL path skips alpha_status_transitions audit. "
              "Prefer running with --confirm via Python instead for these changes.")
        for ch in quality_changes:
            ns = ch["new_status"].replace("'", "''")
            print(f"UPDATE alphas SET quality_status = '{ns}' WHERE id = {ch['id']};")
    print()

    # 4. KB alpha_id_ref
    kb_changes = plan.get("kb_alpha_id_ref", {}).get("changes", [])
    for ch in kb_changes:
        if ch.get("matched_alpha_id") is None:
            continue
        print(
            f"UPDATE knowledge_entries SET meta_data = COALESCE(meta_data, '{{}}'::jsonb) "
            f"|| jsonb_build_object('alpha_id_ref', {ch['matched_alpha_id']}) "
            f"WHERE id = {ch['kb_id']};"
        )

    print()
    print("COMMIT;")


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--plan",
        type=Path,
        default=Path("backfill_plan.json"),
        help="Path to backfill_plan.json from the dryrun step",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required to actually apply changes to the database",
    )
    parser.add_argument(
        "--dry-sql",
        action="store_true",
        help="Emit SQL statements to stdout instead of executing",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Number of rows per UPDATE batch for factor_tier (default 500)",
    )
    args = parser.parse_args()

    if not args.plan.exists():
        print(f"plan file not found: {args.plan}", file=sys.stderr)
        sys.exit(2)

    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    _summarise(plan)

    if args.dry_sql:
        _emit_sql(args.plan)
        return

    if not args.confirm:
        print()
        print("Pass --confirm to apply changes, or --dry-sql to emit SQL.")
        return

    try:
        asyncio.run(_apply(args.plan, batch_size=args.batch_size))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    cli()
