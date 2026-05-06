"""Phase 3 readiness check — automatic GO/NO-GO signal for main-loop
inversion (Plan v5+ §C-Phase 3).

Per docs/phase3_evaluation_2026-05-06.md, Phase 3 launch requires 5 gates.
This script reports current status against each. Run periodically
(suggested: weekly during 5-7 月 observation period; mandatory before any
Q3 Phase 3 kickoff meeting).

Gates:
  1. N ≥ 20 LEVEL=2 task COMPLETED
  2. Phase 2 PASS rate ≥ Phase 1 - 20% (V-1 灰度 criterion)
  3. ≥ 1 hypothesis with rounds_active ≥ 3 (cross-round trajectory data)
  4. BRAIN quarterly quota ≥ 500 simulates (manual; script only flags)
  5. LLM API pricing stable (manual; script only flags)

Usage:
    python scripts/phase3_readiness_check.py              # full report
    python scripts/phase3_readiness_check.py --json       # machine-readable
    python scripts/phase3_readiness_check.py --variant 2  # filter v=2 tasks
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text


_PG_URL = "postgresql+asyncpg://postgres:postgres@localhost:5433/alpha_gpt"


async def gate_1_phase2_task_count(conn) -> dict:
    """N ≥ 20 LEVEL=2 task COMPLETED."""
    r = await conn.execute(text("""
        SELECT COUNT(*) AS n FROM mining_tasks
        WHERE config->>'hypothesis_centric_variant' = '2'
          AND status = 'COMPLETED'
    """))
    n = int(r.scalar() or 0)
    return {
        "gate": "Phase 2 task count",
        "threshold": 20,
        "actual": n,
        "passed": n >= 20,
        "note": f"COMPLETED LEVEL=2 tasks (any time)",
    }


async def gate_2_pass_rate_parity(conn) -> dict:
    """Phase 2 PASS rate ≥ Phase 1 - 20% across all completed tasks."""
    r = await conn.execute(text("""
        WITH stats AS (
            SELECT mt.config->>'hypothesis_centric_variant' AS variant,
                   COUNT(a.id) FILTER (WHERE a.quality_status IN ('PASS','PASS_PROVISIONAL')) AS pass_n,
                   COUNT(af.id) AS fail_n
            FROM mining_tasks mt
            LEFT JOIN alphas a ON a.task_id = mt.id
            LEFT JOIN alpha_failures af ON af.task_id = mt.id
            WHERE mt.config->>'hypothesis_centric_variant' IN ('1','2')
              AND mt.status = 'COMPLETED'
            GROUP BY mt.config->>'hypothesis_centric_variant'
        )
        SELECT variant, pass_n, fail_n FROM stats
    """))
    rows = list(r.fetchall())
    by_variant = {row.variant: {"pass": row.pass_n, "fail": row.fail_n} for row in rows}
    p1 = by_variant.get("1", {"pass": 0, "fail": 0})
    p2 = by_variant.get("2", {"pass": 0, "fail": 0})

    p1_total = (p1["pass"] or 0) + (p1["fail"] or 0)
    p2_total = (p2["pass"] or 0) + (p2["fail"] or 0)

    if p1_total == 0 or p2_total == 0:
        return {
            "gate": "Phase 2 PASS rate parity",
            "passed": False,
            "actual": "insufficient data — need both v=1 and v=2 COMPLETED tasks",
            "v1_pass_rate": None, "v2_pass_rate": None,
            "note": f"v=1 total alphas: {p1_total}, v=2 total alphas: {p2_total}",
        }

    p1_rate = p1["pass"] / p1_total
    p2_rate = p2["pass"] / p2_total
    threshold = p1_rate * 0.8  # within 20% margin
    return {
        "gate": "Phase 2 PASS rate parity",
        "v1_pass_rate": round(p1_rate * 100, 2),
        "v2_pass_rate": round(p2_rate * 100, 2),
        "threshold_pct": round(threshold * 100, 2),
        "passed": p2_rate >= threshold,
        "note": f"v=2 must be ≥ {threshold * 100:.2f}% (= v=1 - 20%)",
    }


async def gate_3_cross_round_data(conn) -> dict:
    """≥ 1 hypothesis with cross-round trajectory data (≥3 rounds active)."""
    # rounds_active = distinct minute-buckets of alpha created_at
    r = await conn.execute(text("""
        SELECT COUNT(*) AS n FROM (
            SELECT hypothesis_id, COUNT(DISTINCT date_trunc('minute', created_at)) AS rounds
            FROM alphas
            WHERE hypothesis_id IS NOT NULL
            GROUP BY hypothesis_id
            HAVING COUNT(DISTINCT date_trunc('minute', created_at)) >= 3
        ) sub
    """))
    n = int(r.scalar() or 0)
    return {
        "gate": "Cross-round trajectory data",
        "threshold": 1,
        "actual": n,
        "passed": n >= 1,
        "note": "hypotheses with ≥3 distinct minute-rounds of alpha generation",
    }


async def gate_4_brain_quota_manual() -> dict:
    """BRAIN quarterly quota — manual flag, no DB record."""
    return {
        "gate": "BRAIN quarterly quota",
        "passed": None,  # manual
        "actual": "MANUAL CHECK REQUIRED",
        "note": "Verify ≥ 500 simulate budget for Q3 — see WorldQuant BRAIN dashboard",
    }


async def gate_5_llm_pricing_manual() -> dict:
    """LLM API pricing stable — manual flag."""
    return {
        "gate": "LLM API pricing stability",
        "passed": None,  # manual
        "actual": "MANUAL CHECK REQUIRED",
        "note": "Verify DeepSeek pricing not surged since 2026-05-06 baseline",
    }


async def main(json_out: bool = False) -> int:
    engine = create_async_engine(_PG_URL, echo=False)
    async with engine.begin() as conn:
        gates = [
            await gate_1_phase2_task_count(conn),
            await gate_2_pass_rate_parity(conn),
            await gate_3_cross_round_data(conn),
            await gate_4_brain_quota_manual(),
            await gate_5_llm_pricing_manual(),
        ]
    await engine.dispose()

    auto_passed = [g for g in gates if g["passed"] is True]
    auto_failed = [g for g in gates if g["passed"] is False]
    manual = [g for g in gates if g["passed"] is None]

    overall = "GO" if (len(auto_failed) == 0 and len(auto_passed) >= 3) else "NO-GO"

    if json_out:
        print(json.dumps({
            "overall": overall,
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "auto_passed_count": len(auto_passed),
            "auto_failed_count": len(auto_failed),
            "manual_pending_count": len(manual),
            "gates": gates,
        }, indent=2, default=str))
        return 0 if overall == "GO" else 1

    print("=" * 70)
    print(f"Phase 3 readiness check — {datetime.utcnow().isoformat()}Z")
    print("=" * 70)
    for g in gates:
        if g["passed"] is True:
            mark = "✓ PASS"
        elif g["passed"] is False:
            mark = "✗ FAIL"
        else:
            mark = "? MANUAL"
        print(f"\n  {mark}  {g['gate']}")
        for k, v in g.items():
            if k in ("gate", "passed"):
                continue
            print(f"        {k}: {v}")

    print()
    print("=" * 70)
    print(f"  Auto-passed: {len(auto_passed)}/3 (need ≥3)")
    print(f"  Auto-failed: {len(auto_failed)} (need 0)")
    print(f"  Manual pending: {len(manual)} (verify externally)")
    print(f"  Overall: {overall}")
    print("=" * 70)
    return 0 if overall == "GO" else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="machine-readable JSON output")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(json_out=args.json)))
