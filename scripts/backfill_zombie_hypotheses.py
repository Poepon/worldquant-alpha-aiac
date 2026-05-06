"""Backfill zombie ACTIVE hypotheses → SUPERSEDED.

V-19.7 (2026-05-06): pre-fix, B3 persisted N hypotheses per round but B4
linked alphas only to the PRIMARY (first). V-19.6 stopped non-primary from
being falsely PROMOTED, but they remained zombie ACTIVE forever:
  - alpha_count = 0 (no alphas linked)
  - lifecycle frozen (no transition possible — they don't own alphas, so
    mark_active/promoted/abandoned via _process_hypothesis_feedback never
    fires for them)

Post-V-19.7, B3 only persists the primary. This script transitions existing
zombies → SUPERSEDED so they're excluded from list_active sampling and the
ACTIVE state count reflects reality.

Definition of zombie:
  status = 'ACTIVE' AND alpha_count = 0 AND no child_hypothesis_id refs
  (the last condition is redundant — only ACTIVE rows produced via B5
  mark_active have alpha_count, but defensive)

Usage:
    python scripts/backfill_zombie_hypotheses.py [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select, update


_PG_URL = "postgresql+asyncpg://postgres:postgres@localhost:5433/alpha_gpt"


async def main(dry_run: bool = False) -> int:
    from backend.models import Hypothesis

    engine = create_async_engine(_PG_URL, echo=False)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        # Find zombies: ACTIVE + alpha_count=0
        r = await s.execute(
            select(Hypothesis).where(
                Hypothesis.status == "ACTIVE",
                Hypothesis.alpha_count == 0,
            )
        )
        zombies = list(r.scalars().all())
        print(f"Zombie ACTIVE rows (alpha_count=0): {len(zombies)}")

        if not zombies:
            print("No zombies to clean up.")
            return 0

        # Sample
        for h in zombies[:5]:
            print(f"  hid={h.id} status={h.status} created={h.created_at} "
                  f"stmt={h.statement[:60]!r}")
        if len(zombies) > 5:
            print(f"  ... and {len(zombies) - 5} more")

        if dry_run:
            print(f"\n[DRY-RUN] would transition {len(zombies)} ACTIVE → SUPERSEDED")
            return 0

        zombie_ids = [h.id for h in zombies]
        await s.execute(
            update(Hypothesis)
            .where(Hypothesis.id.in_(zombie_ids))
            .values(
                status="SUPERSEDED",
                abandon_reason="V-19.7 zombie cleanup — pre-fix non-primary siblings "
                               "never received alphas; transitioned ACTIVE → SUPERSEDED "
                               "to clear list_active sampling pool.",
            )
        )
        await s.commit()
        print(f"\nCommitted: transitioned {len(zombie_ids)} rows ACTIVE → SUPERSEDED")
    await engine.dispose()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(dry_run=args.dry_run)))
