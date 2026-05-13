"""V-25.C — V-22.13 cross-round reuse audit.

V-22.13 reuses a same-task hypothesis across rounds when history_len <
HYPOTHESIS_ABANDON_ROUNDS. Today task=536 reused correctly (alpha=3,
unique_hids=1) but task=533/535/551 each round created a fresh hid
(alpha=N, unique_hids=N). Root cause unknown.

This script approximates the reuse failure rate from DB state (no
runtime log dependency), so it works on existing data. After
generation.py's V-25.C diagnostic INFO logs land, parse celery.log
directly with grep/awk for cleaner attribution.

Heuristic:
  For each variant=2 mining_task in the window, count:
    - n_alpha   = alphas linked to this task with hypothesis_id NOT NULL
    - n_hids    = distinct hypothesis_id values across those alphas
    - reuse_rate = n_alpha / n_hids (≥2 means at least one reuse)

Usage:
  venv/Scripts/python.exe scripts/v22_13_reuse_audit.py
  venv/Scripts/python.exe scripts/v22_13_reuse_audit.py --days 14

Output: per-task reuse table + aggregate skip-path distribution
(once V-25.C logs accumulate, this script gains a parse_celery_log mode).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from backend.database import AsyncSessionLocal


async def per_task_reuse(db, days: int):
    sql = text(f"""
        SELECT t.id AS task_id, t.created_at, t.status,
               COUNT(a.id) FILTER (WHERE a.hypothesis_id IS NOT NULL) AS n_alpha,
               COUNT(DISTINCT a.hypothesis_id) AS n_hids
        FROM mining_tasks t
        LEFT JOIN alphas a ON a.task_id = t.id
        WHERE t.created_at > NOW() - INTERVAL '{days} days'
          AND t.config->>'hypothesis_centric_variant' = '2'
          AND t.agent_mode = 'AUTONOMOUS_TIER1'
        GROUP BY t.id
        HAVING COUNT(a.id) FILTER (WHERE a.hypothesis_id IS NOT NULL) > 0
        ORDER BY t.created_at DESC
    """)
    return list((await db.execute(sql)).all())


async def aggregate_reuse(db, days: int) -> dict:
    """Roll up: how many alpha-producing variant=2 tasks reused at all?"""
    sql = text(f"""
        WITH per_task AS (
            SELECT t.id,
                   COUNT(a.id) FILTER (WHERE a.hypothesis_id IS NOT NULL) AS n_alpha,
                   COUNT(DISTINCT a.hypothesis_id) AS n_hids
            FROM mining_tasks t
            LEFT JOIN alphas a ON a.task_id = t.id
            WHERE t.created_at > NOW() - INTERVAL '{days} days'
              AND t.config->>'hypothesis_centric_variant' = '2'
              AND t.agent_mode = 'AUTONOMOUS_TIER1'
            GROUP BY t.id
            HAVING COUNT(a.id) FILTER (WHERE a.hypothesis_id IS NOT NULL) > 0
        )
        SELECT
            COUNT(*) AS n_tasks,
            COUNT(*) FILTER (WHERE n_alpha > n_hids) AS reused,
            COUNT(*) FILTER (WHERE n_alpha = n_hids) AS no_reuse,
            SUM(n_alpha) AS total_alpha,
            SUM(n_hids) AS total_hids
        FROM per_task
    """)
    row = (await db.execute(sql)).first()
    if not row or row.n_tasks == 0:
        return {"n_tasks": 0}
    return {
        "n_tasks": row.n_tasks,
        "reused_tasks": row.reused,
        "no_reuse_tasks": row.no_reuse,
        "reuse_task_rate": row.reused / row.n_tasks if row.n_tasks else 0.0,
        "total_alpha": row.total_alpha,
        "total_hids": row.total_hids,
        "alpha_per_hid": (row.total_alpha / row.total_hids) if row.total_hids else 0.0,
    }


async def main(days: int) -> int:
    print(f"=== V-25.C V-22.13 reuse audit (variant=2, last {days}d) ===\n")
    async with AsyncSessionLocal() as db:
        tasks = await per_task_reuse(db, days)
        agg = await aggregate_reuse(db, days)

    if agg.get("n_tasks", 0) == 0:
        print(f"No variant=2 AUTONOMOUS_TIER1 tasks produced any alpha in {days}d.")
        return 1

    print("## Per-task reuse pattern")
    print(f"  {'task_id':>8s} {'status':<12s} {'n_alpha':>8s} {'n_hids':>7s} "
          f"{'reused?':>8s}  {'created_at'}")
    for r in tasks:
        reused = "yes" if r.n_alpha > r.n_hids else "no"
        marker = "✅" if reused == "yes" else "❌"
        print(f"  {r.task_id:>8d} {r.status:<12s} {r.n_alpha:>8d} {r.n_hids:>7d} "
              f"{reused + ' ' + marker:>8s}  {r.created_at}")
    print()

    print("## Aggregate")
    print(f"  Tasks with ≥1 linked alpha:    {agg['n_tasks']}")
    print(f"  Tasks where reuse happened:    {agg['reused_tasks']} "
          f"({agg['reuse_task_rate']*100:.1f}%)")
    print(f"  Tasks where reuse never fired: {agg['no_reuse_tasks']}")
    print(f"  Total alpha linked:            {agg['total_alpha']}")
    print(f"  Total distinct hids:           {agg['total_hids']}")
    print(f"  Alpha per hid (higher=better): {agg['alpha_per_hid']:.2f}")
    print()

    print("## Skip-path distribution (requires V-25.C logs)")
    print("  After generation.py's V-25.C INFO logs accumulate in")
    print("  celery worker logs, run:")
    print("    grep 'V-22.13 skip' logs/celery.log | "
          "awk '{for(i=1;i<=NF;i++) if($i~/^reason=/) print $i}' | sort | uniq -c")
    print("  Expected: a single dominant path_X gives the next debug step.")
    print()

    print("## Verdict")
    if agg["alpha_per_hid"] < 1.5:
        print(f"  ❌ Reuse failure rate high: only {agg['alpha_per_hid']:.2f} alpha/hid")
        print(f"  expected (with V-22.13 working): ≥ 3.0 (3 rounds × multiple alphas)")
        print(f"  Most likely path_a_no_state (LangGraph scalar drop) — fix by")
        print(f"  adding fallback list[0] lookup in generation.py:463, same as")
        print(f"  persistence.py:391-395 already does.")
    elif agg["alpha_per_hid"] < 3:
        print(f"  ⚠ Partial reuse ({agg['alpha_per_hid']:.2f} alpha/hid). Some rounds")
        print(f"  reuse, others create fresh. Look at skip-path log distribution.")
    else:
        print(f"  ✅ Reuse healthy ({agg['alpha_per_hid']:.2f} alpha/hid).")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=14)
    args = p.parse_args()
    raise SystemExit(asyncio.run(main(args.days)))
