"""V-24.B — Pre-simulate filter audit.

After ENABLE_PRE_SIMULATE_FILTER=True, this script answers:

  1. How many alphas did the filter skip in the last N days?
  2. What's the PASS rate on KEPT alphas vs the baseline (filter OFF)?
  3. Are any "FAIL-predicted but actually PASS" cases visible (false
     negatives — the cost of running the filter)?

The runtime logs the skipped count per round (logger.info from
evaluation.py:421). For DB-level numbers we use pending_alphas
simulation_error = "pre-simulate filter skip: ..." which gets persisted
into alpha_failures.error_message.

Usage:
  venv/Scripts/python.exe scripts/pre_simulate_filter_audit.py
  venv/Scripts/python.exe scripts/pre_simulate_filter_audit.py --days 7

Recommended workflow:
  1. Enable filter at threshold=0.10 (V-24.B default).
  2. Run mining 7 days.
  3. Run this audit — compare skipped count vs PASS rate trend.
  4. If PASS rate stable and savings ≥ 5% simulate calls, bump
     threshold to 0.15 (skip 17% FAIL, lose 3.5% PASS) for ~3x more
     BRAIN savings.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from backend.config import settings
from backend.database import AsyncSessionLocal


async def filter_skip_count(db, days: int) -> dict:
    """Count alpha_failures rows whose error_message tags pre-simulate skip."""
    sql = text(f"""
        SELECT COUNT(*) AS skipped,
               COUNT(DISTINCT task_id) AS tasks_affected
        FROM alpha_failures
        WHERE created_at > NOW() - INTERVAL '{days} days'
          AND error_message LIKE 'pre-simulate filter skip%'
    """)
    row = (await db.execute(sql)).first()
    return {"skipped": row[0], "tasks": row[1]}


async def total_simulate_attempts(db, days: int) -> int:
    """Approximate total simulate attempts = alphas + alpha_failures rows
    excluding the pre-simulate skips. Each row represents one BRAIN call
    actually made (or attempted)."""
    sql = text(f"""
        SELECT
            (SELECT COUNT(*) FROM alphas
             WHERE created_at > NOW() - INTERVAL '{days} days') +
            (SELECT COUNT(*) FROM alpha_failures
             WHERE created_at > NOW() - INTERVAL '{days} days'
               AND COALESCE(error_message, '') NOT LIKE 'pre-simulate%')
    """)
    return int((await db.execute(sql)).scalar() or 0)


async def pass_rate(db, days: int) -> dict:
    sql = text(f"""
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE quality_status='PASS') AS pass_n,
               COUNT(*) FILTER (WHERE quality_status='PASS_PROVISIONAL') AS prov_n
        FROM alphas
        WHERE created_at > NOW() - INTERVAL '{days} days'
    """)
    row = (await db.execute(sql)).first()
    total, p, prov = row[0], row[1], row[2]
    return {
        "total": total,
        "pass_n": p,
        "prov_n": prov,
        "pass_rate": (p / total) if total else 0.0,
        "pass_or_prov_rate": ((p + prov) / total) if total else 0.0,
    }


async def main(days: int) -> int:
    print(f"=== V-24.B Pre-simulate filter audit (last {days}d) ===\n")
    print(f"ENABLE_PRE_SIMULATE_FILTER = {settings.ENABLE_PRE_SIMULATE_FILTER}")
    print(f"PRE_SIMULATE_FILTER_THRESHOLD = {settings.PRE_SIMULATE_FILTER_THRESHOLD}\n")

    async with AsyncSessionLocal() as db:
        skip = await filter_skip_count(db, days)
        simulated = await total_simulate_attempts(db, days)
        pr = await pass_rate(db, days)

    total_candidates = skip["skipped"] + simulated
    pct_saved = (skip["skipped"] / total_candidates * 100) if total_candidates else 0.0

    print("## Filter activity")
    print(f"  Skipped (pre-simulate):  {skip['skipped']:5d}")
    print(f"  Sent to BRAIN simulate:  {simulated:5d}")
    print(f"  Total candidates:        {total_candidates:5d}")
    print(f"  Simulate savings:        {pct_saved:5.1f}%")
    print(f"  Tasks affected:          {skip['tasks']}")
    print()

    print("## PASS rate on kept alphas")
    print(f"  alphas table rows: {pr['total']}")
    print(f"  PASS:              {pr['pass_n']} ({pr['pass_rate']*100:.2f}%)")
    print(f"  PASS_PROVISIONAL:  {pr['prov_n']}")
    print(f"  PASS+PROV rate:    {pr['pass_or_prov_rate']*100:.2f}%")
    print()

    print("## Verdict")
    if skip["skipped"] == 0:
        print(f"  ⚠ No filter activity in {days}d. Either:")
        print(f"    - ENABLE_PRE_SIMULATE_FILTER was OFF during this window")
        print(f"    - No mining tasks ran")
        print(f"    - Worker isn't loading the new config (restart worker)")
        return 1
    if pct_saved < 3:
        print(f"  ⚠ Savings {pct_saved:.1f}% < 3% — threshold too conservative.")
        print(f"    Consider bumping PRE_SIMULATE_FILTER_THRESHOLD to 0.15.")
    elif pct_saved > 25:
        print(f"  ⚠ Savings {pct_saved:.1f}% > 25% — may be over-filtering.")
        print(f"    Check if PASS rate dropped vs prior period; if so, dial back.")
    else:
        print(f"  ✅ Healthy filter activity ({pct_saved:.1f}% saved).")
        print(f"  Compare PASS+PROV rate {pr['pass_or_prov_rate']*100:.1f}% against")
        print(f"  the pre-filter baseline to decide if bumping to threshold 0.15")
        print(f"  (~17% savings, 3.5pp PASS recall cost) is worthwhile.")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7)
    args = p.parse_args()
    raise SystemExit(asyncio.run(main(args.days)))
