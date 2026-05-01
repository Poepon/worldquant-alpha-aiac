"""Backfill: scan all alphas with quality_status ∈ {PASS, PASS_PROVISIONAL}
and call BRAIN GET /alphas/{id} to compute can_submit + cached failed/pending
checks. Sequential (1 req/sec) to spare BRAIN quota. Idempotent.

Run:
  python scripts/backfill_can_submit.py                  # dry-run preview
  python scripts/backfill_can_submit.py --confirm        # write
  python scripts/backfill_can_submit.py --tier 3 --confirm    # only T3
  python scripts/backfill_can_submit.py --status PASS --confirm  # only PASS
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from typing import List, Tuple

from sqlalchemy import select

from backend.adapters.brain_adapter import BrainAdapter
from backend.database import AsyncSessionLocal
from backend.models import Alpha
from backend.services.alpha_service import AlphaService


async def main(confirm: bool, tier: int | None, statuses: List[str]) -> None:
    async with AsyncSessionLocal() as db:
        q = (
            select(Alpha.id, Alpha.alpha_id, Alpha.factor_tier, Alpha.quality_status, Alpha.can_submit)
            .where(Alpha.quality_status.in_(statuses))
            .where(Alpha.alpha_id.isnot(None))
        )
        if tier is not None:
            q = q.where(Alpha.factor_tier == tier)
        q = q.order_by(Alpha.id.desc())
        rows = (await db.execute(q)).all()

        print(f"Scope: status={statuses} tier={tier or 'any'} → {len(rows)} candidate alpha(s).")
        already = sum(1 for r in rows if r.can_submit is not None)
        print(f"  already-checked: {already} (will re-check unless --skip-existing in future).")

        if not rows:
            return
        if not confirm:
            print("\nDry-run only. Pass --confirm to actually call BRAIN and write.")
            for r in rows[:10]:
                print(f"  pk={r.id} brain={r.alpha_id} tier={r.factor_tier} status={r.quality_status} current_can_submit={r.can_submit}")
            if len(rows) > 10:
                print(f"  ... and {len(rows) - 10} more.")
            return

        ok_n = fail_n = skip_n = 0
        sample_failures: List[Tuple[int, int]] = []
        svc = AlphaService(db)
        async with BrainAdapter() as ba:
            for i, r in enumerate(rows, 1):
                await asyncio.sleep(1.0)
                try:
                    res = await svc.refresh_can_submit(r.id, brain_adapter=ba)
                except Exception as e:
                    skip_n += 1
                    print(f"  [{i}/{len(rows)}] pk={r.id} brain={r.alpha_id} ERROR: {e}")
                    continue
                if res is None:
                    skip_n += 1
                    print(f"  [{i}/{len(rows)}] pk={r.id} brain={r.alpha_id} skipped (BRAIN no data)")
                    continue
                if res["can_submit"]:
                    ok_n += 1
                else:
                    fail_n += 1
                    if len(sample_failures) < 5:
                        sample_failures.append((r.id, len(res["failed_checks"])))
                if i % 10 == 0 or i == len(rows):
                    print(f"  progress {i}/{len(rows)}: ok={ok_n} fail={fail_n} skip={skip_n}")

        print(f"\nDone. can_submit=True: {ok_n} | False: {fail_n} | skipped: {skip_n}")
        if sample_failures:
            print(f"Sample alphas with FAIL:")
            for pk, n in sample_failures:
                print(f"  pk={pk} fail_count={n}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm", action="store_true", help="actually call BRAIN and write")
    ap.add_argument("--tier", type=int, default=None, choices=[1, 2, 3])
    ap.add_argument("--status", nargs="+", default=["PASS", "PASS_PROVISIONAL"],
                    help="quality_status values to include (default: PASS + PASS_PROVISIONAL)")
    args = ap.parse_args()
    asyncio.run(main(args.confirm, args.tier, args.status))
