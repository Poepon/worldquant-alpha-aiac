"""Cold-start KB seed: SUCCESS_PATTERN entries from BRAIN-verified gold alphas.

Strict filter: can_submit=true AND sharpe>=1.5 AND fitness>=1.0 — only the
17 alphas BRAIN itself certifies as submittable get into the seed pool.
Zero contamination tolerance: anything BRAIN's checks rejected stays out.

Tradeoff: most gold alphas are user-authored complex expressions and
classify_tier=None. Tier-aware RAG (T1/T2/T3 task RAG_QUERY filtered by
factor_tier) won't retrieve these; tier-agnostic RAG (default path) will.
First mining tasks may produce thin few-shot context until natural
feedback_agent.learn_from_round backfills tier-classified patterns.

Run after `DELETE FROM knowledge_entries` (or pass --wipe-first).

Usage:
    python scripts/seed_kb_from_gold_alphas.py            # dry-run
    python scripts/seed_kb_from_gold_alphas.py --confirm  # write
    python scripts/seed_kb_from_gold_alphas.py --wipe-first --confirm
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from typing import List

from sqlalchemy import select, text

from backend.database import AsyncSessionLocal
from backend.factor_tier_classifier import classify_tier
from backend.knowledge_extraction import expression_to_skeleton, extract_operator_chain
from backend.models import Alpha, KnowledgeEntry
from backend.models.knowledge import compute_pattern_hash


SEED_FILTER = (
    Alpha.can_submit.is_(True)
    & (Alpha.is_sharpe >= 1.5)
    & (Alpha.is_fitness >= 1.0)
)


async def main(confirm: bool, wipe_first: bool) -> None:
    async with AsyncSessionLocal() as db:
        if wipe_first:
            existing_n = (await db.execute(text("SELECT COUNT(*) FROM knowledge_entries"))).scalar()
            print(f"Wipe target: {existing_n} existing knowledge_entries")
            if confirm:
                await db.execute(text("DELETE FROM knowledge_entries"))
                await db.commit()
                print(f"  → deleted {existing_n} rows.")
            else:
                print("  (dry-run, not deleted)")

        rows = (
            await db.execute(
                select(Alpha)
                .where(SEED_FILTER)
                .order_by(Alpha.is_sharpe.desc())
            )
        ).scalars().all()

        print(f"\nGold candidates (can_submit=True + sharpe>=1.5 + fit>=1.0): {len(rows)}")
        if not rows:
            print("No gold alphas — verify alphas have been backfilled with can_submit.")
            return

        plans: List[KnowledgeEntry] = []
        skipped_no_skeleton = 0
        skipped_no_tier = 0
        for a in rows:
            if not a.expression:
                continue
            try:
                skeleton = expression_to_skeleton(a.expression)
            except Exception as e:
                print(f"  skip #{a.id} (skeleton fail: {e})")
                skipped_no_skeleton += 1
                continue
            if not skeleton:
                skipped_no_skeleton += 1
                continue
            tier = classify_tier(a.expression)
            try:
                op_chain = extract_operator_chain(a.expression)
            except Exception:
                op_chain = []

            description = (
                f"BRAIN-verified gold alpha (can_submit=True). "
                f"sharpe={a.is_sharpe:.2f} fitness={a.is_fitness:.2f}. "
                f"Tier {tier or '?'} pattern."
            )

            entry = KnowledgeEntry(
                entry_type="SUCCESS_PATTERN",
                pattern=skeleton,
                pattern_hash=compute_pattern_hash(
                    skeleton, region=a.region, dataset_id=a.dataset_id
                ),
                description=description,
                factor_tier=tier,
                meta_data={
                    "source": "cold_start_can_submit_gold",
                    "alpha_id_ref": a.alpha_id,
                    "alpha_pk": a.id,
                    "region": a.region,
                    "dataset_id": a.dataset_id,
                    "universe": a.universe,
                    "sharpe": a.is_sharpe,
                    "fitness": a.is_fitness,
                    "turnover": a.is_turnover,
                    "can_submit": True,
                    "operator_chain": (op_chain or [])[:8],
                    "example_expression": a.expression[:300],
                    "factor_tier": tier,
                },
                usage_count=0,
                is_active=True,
                created_by="cold_start_script",
            )
            plans.append(entry)

        print(f"  prepared: {len(plans)} SUCCESS_PATTERN entries")
        print(f"  skipped (no skeleton): {skipped_no_skeleton}")

        # Tier breakdown
        tier_count = {}
        for p in plans:
            tier_count[p.factor_tier] = tier_count.get(p.factor_tier, 0) + 1
        print(f"  tier breakdown: {tier_count}")

        # Sample first 5
        print("\n  sample (top-5 by sharpe):")
        for p in plans[:5]:
            md = p.meta_data
            print(
                f"    pk={md.get('alpha_pk')} brain={md.get('alpha_id_ref')} "
                f"tier={p.factor_tier} sharpe={md.get('sharpe'):.2f} "
                f"region={md.get('region')} "
                f"skel={p.pattern[:80]}"
            )

        if not confirm:
            print("\nDry-run only. Re-run with --confirm to write.")
            return

        # Dedup by pattern_hash within batch (multiple gold alphas may share skeleton)
        seen_hashes = set()
        kept = []
        for p in plans:
            if p.pattern_hash in seen_hashes:
                continue
            seen_hashes.add(p.pattern_hash)
            kept.append(p)
        print(f"\nAfter intra-batch dedup by pattern_hash: {len(kept)} entries")

        for p in kept:
            db.add(p)
        await db.commit()
        print(f"Committed {len(kept)} SUCCESS_PATTERN entries.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm", action="store_true", help="actually write to DB")
    ap.add_argument("--wipe-first", action="store_true",
                    help="DELETE FROM knowledge_entries before seeding (use with --confirm)")
    args = ap.parse_args()
    asyncio.run(main(args.confirm, args.wipe_first))
