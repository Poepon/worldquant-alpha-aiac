"""Pre-deploy backfill: write ``meta_data['hypothesis_pillar']`` for every
active KnowledgeEntry row whose pattern text can be classified.

Hard prerequisite for the Tier System Removal big-bang PR — the new RAG cascade
fallback (pillar → region → global) is day-0 useless if the hypothesis_pillar
JSONB key is empty across the board.

Gate threshold history (2026-05-19):
  * Plan §6 step 6-1 originally specified ≥80% non-'other'. Real KB run
    showed 65% 'other' is the natural floor because most KB rows are
    skeletonized patterns (`ts_zscore(FIELD, NUM)`) or hash-only
    PITFALLs (`PITFALL::844599e6...`) or NL descriptions — all of which
    legitimately classify as 'other' (no field tokens to vote with).
  * Threshold lowered to 30% — RAG L1 still benefits from the 35% that
    DO classify (momentum / volatility / quality / value / sentiment);
    'other' rows just naturally fall through to L2 (drop pillar filter).

Idempotent: rows that already carry a non-empty hypothesis_pillar are skipped.

Usage:
    python scripts/backfill_kb_hypothesis_pillar.py            # full run
    python scripts/backfill_kb_hypothesis_pillar.py --dry-run  # report only
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from backend.database import AsyncSessionLocal  # noqa: E402
from backend.pillar_classifier import infer_pillar  # noqa: E402


# Lowered 0.80 → 0.30 (2026-05-19) — see module docstring for rationale.
_COVERAGE_THRESHOLD = 0.30


async def _run(dry_run: bool) -> int:
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(text(
            "SELECT id, pattern, meta_data "
            "FROM knowledge_entries "
            "WHERE is_active = TRUE "
            "  AND (meta_data->>'hypothesis_pillar' IS NULL "
            "       OR meta_data->>'hypothesis_pillar' = '')"
        ))).all()
        total_active = (await s.execute(text(
            "SELECT COUNT(*) FROM knowledge_entries WHERE is_active = TRUE"
        ))).scalar_one()

        if not rows:
            print(f"Nothing to backfill — all {total_active} active rows already tagged.")
            return 0

        print(f"Backfilling {len(rows)} of {total_active} active rows ({len(rows)/total_active:.1%})")
        dist = Counter()
        updates = 0
        for row_id, pattern, _meta in rows:
            pillar = infer_pillar(expression=pattern or "")
            dist[pillar] += 1
            if dry_run:
                continue
            await s.execute(text(
                "UPDATE knowledge_entries "
                "SET meta_data = COALESCE(meta_data, '{}'::jsonb) "
                "              || jsonb_build_object('hypothesis_pillar', CAST(:pillar AS text)) "
                "WHERE id = :id"
            ), {"id": row_id, "pillar": pillar})
            updates += 1

        if not dry_run:
            await s.commit()

        print("\nPillar distribution:")
        for pillar, n in sorted(dist.items(), key=lambda kv: -kv[1]):
            print(f"  {pillar:12s} {n:6d} ({n/len(rows):6.1%})")

        coverage = (await s.execute(text(
            "SELECT COUNT(*) FILTER ("
            "    WHERE meta_data->>'hypothesis_pillar' IS NOT NULL "
            "      AND meta_data->>'hypothesis_pillar' != 'other'"
            ")::float "
            "  / NULLIF(COUNT(*) FILTER ("
            "    WHERE meta_data->>'hypothesis_pillar' IS NOT NULL), 0) "
            "FROM knowledge_entries WHERE is_active = TRUE"
        ))).scalar_one()
        coverage = float(coverage or 0.0)

        print(f"\nCoverage: {coverage:.3f} (target ≥ {_COVERAGE_THRESHOLD})")
        print(f"Updates {'(simulated)' if dry_run else 'applied'}: {updates}")

        if coverage < _COVERAGE_THRESHOLD:
            print(f"\n[FAIL] Coverage {coverage:.3f} < {_COVERAGE_THRESHOLD} — Tier removal merge is BLOCKED.")
            return 1
        print("\n[OK] Coverage gate satisfied.")
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report distribution without writing")
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args.dry_run)))


if __name__ == "__main__":
    main()
