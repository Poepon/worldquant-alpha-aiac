"""Reconcile SUCCESS_PATTERN KB from BRAIN-validated alphas (2026-05-20).

Ingests PASS / PASS_PROVISIONAL alphas whose expression skeleton is NOT yet
in the active SUCCESS_PATTERN knowledge base, so the R8 RAG corpus picks up
BRAIN-validated success patterns that never went through the live mining
record_success_pattern path (synced from BRAIN, or lost to a persistence
anomaly and recovered via sync).

Design decisions (per session 2026-05-20 discussion):
  - Provenance, not gatekeeping: BRAIN-validated patterns are legitimate RAG
    signal regardless of source. We tag source='sync_reconcile' for honest
    analytics / attribution, but do NOT exclude foreign alphas from learning
    (they even debias the pre-sim classifier — an unfiltered sample).
  - The real filter is skeleton QUALITY, not source: gate on operator nesting
    >= --min-nesting (default 2). Generic single-op skeletons like
    `ts_arg_max(FIELD, NUM)` carry near-zero RAG signal (they abstract away the
    field/window that actually matters) and would dilute the corpus.
  - Idempotent: gates on "skeleton not already in active SUCCESS_PATTERN", so
    re-runs skip already-ingested patterns. record_success_pattern dedups by
    skeleton, so multiple alphas sharing a new skeleton collapse into one entry
    with usage_count + rolling-average metrics.

Usage:
    python scripts/reconcile_success_patterns.py            # dry-run (default)
    python scripts/reconcile_success_patterns.py --apply
    python scripts/reconcile_success_patterns.py --apply --min-nesting 2
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from backend.database import AsyncSessionLocal
from backend.knowledge_extraction import expression_to_skeleton
from backend.agents.services.rag_service import RAGService
from backend.models import Alpha, KnowledgeEntry


async def _reconcile(apply: bool, min_nesting: int) -> dict:
    # 1. Snapshot the active SUCCESS_PATTERN skeletons ONCE (eligibility gate).
    #    record_success_pattern's own _find_similar_success handles create-vs-
    #    bump within the run, so we only need the original set here.
    async with AsyncSessionLocal() as db:
        kb_rows = (
            await db.execute(
                select(KnowledgeEntry.pattern).where(
                    KnowledgeEntry.entry_type == "SUCCESS_PATTERN",
                    KnowledgeEntry.is_active == True,  # noqa: E712
                    KnowledgeEntry.pattern.isnot(None),
                )
            )
        ).all()
        kb_skeletons = {r[0] for r in kb_rows}

        alpha_rows = (
            await db.execute(
                select(Alpha).where(
                    Alpha.quality_status.in_(["PASS", "PASS_PROVISIONAL"]),
                    Alpha.expression.isnot(None),
                )
            )
        ).scalars().all()

    stats = Counter()
    new_skeletons: set = set()
    rag = RAGService(None)  # record_success_pattern uses its own AsyncSessionLocal

    for a in alpha_rows:
        stats["scanned"] += 1
        try:
            sk = expression_to_skeleton(a.expression)
        except Exception:
            stats["skip_skeleton_error"] += 1
            continue
        if not sk:
            stats["skip_skeleton_error"] += 1
            continue
        if sk in kb_skeletons:
            stats["skip_already_in_kb"] += 1
            continue
        if sk.count("(") < min_nesting:
            stats["skip_generic"] += 1
            continue

        stats["eligible"] += 1
        new_skeletons.add(sk)
        if apply:
            metrics = {
                "sharpe": a.is_sharpe,
                "fitness": a.is_fitness,
                "turnover": a.is_turnover,
            }
            ok = await rag.record_success_pattern(
                expression=a.expression,
                metrics=metrics,
                region=a.region,
                dataset_id=a.dataset_id,
                alpha_id=a.alpha_id,
                source="sync_reconcile",
            )
            if ok:
                stats["ingested_calls"] += 1
            else:
                stats["ingest_failed"] += 1

    return {
        "scanned": stats["scanned"],
        "eligible_alphas": stats["eligible"],
        "distinct_new_skeletons": len(new_skeletons),
        "skipped_already_in_kb": stats["skip_already_in_kb"],
        "skipped_generic_below_min_nesting": stats["skip_generic"],
        "skipped_skeleton_error": stats["skip_skeleton_error"],
        "ingested_calls": stats["ingested_calls"] if apply else 0,
        "ingest_failed": stats["ingest_failed"] if apply else 0,
        "kb_skeletons_before": len(kb_skeletons),
        "applied": apply,
        "min_nesting": min_nesting,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile SUCCESS_PATTERN KB from BRAIN-validated alphas")
    parser.add_argument("--apply", action="store_true", help="actually write (default: dry-run)")
    parser.add_argument("--min-nesting", type=int, default=2,
                        help="minimum operator nesting ('(' count) for a skeleton to qualify (default 2)")
    args = parser.parse_args()

    result = asyncio.run(_reconcile(apply=args.apply, min_nesting=args.min_nesting))

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== reconcile_success_patterns [{mode}] (min_nesting={args.min_nesting}) ===")
    for k, v in result.items():
        print(f"  {k}: {v}")
    if not args.apply:
        print(f"  → would ingest {result['distinct_new_skeletons']} new SUCCESS_PATTERN "
              f"entries from {result['eligible_alphas']} alphas. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
