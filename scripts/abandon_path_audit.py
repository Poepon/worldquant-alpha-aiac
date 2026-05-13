"""V-24.A — Hypothesis abandon path audit.

Quantifies the B6 abandon mechanism's real-world behaviour. The strict
ABANDONED column reads 0 in production, but B6 itself fires — the
downstream G-refinement loop converts most abandon decisions into
SUPERSEDED edges (parent → refined child). This script answers:

  1. How many hypotheses accumulated ≥ N round history?
  2. Of those, how many had attribution=hypothesis for the last N?
     (i.e. would have qualified for B6 abandon)
  3. What's the terminal-path split: SUPERSEDED (refined) vs
     ABANDONED (refine failed / no LLM) vs still ACTIVE?

Runs standalone:
  venv/Scripts/python.exe scripts/abandon_path_audit.py
  venv/Scripts/python.exe scripts/abandon_path_audit.py --days 14

The "history" here is reconstructed from alphas + hypothesis_id linkage
since hypothesis_round_history lives only in MiningState memory. Per-
round attribution isn't persisted to a dedicated table; we approximate
by counting PASS / FAIL per (hypothesis_id, task_id, round) cluster
inferred from alpha.created_at clustering.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from backend.agents.graph.early_stop import HYPOTHESIS_ABANDON_ROUNDS
from backend.database import AsyncSessionLocal


async def lifecycle_breakdown(db, days: int) -> dict:
    sql = text(f"""
        SELECT status, COUNT(*) FROM hypotheses
        WHERE created_at > NOW() - INTERVAL '{days} days'
        GROUP BY status
    """)
    out = {}
    for row in (await db.execute(sql)).all():
        out[row[0]] = row[1]
    return out


async def hypothesis_alpha_clusters(db, days: int):
    """Pull (hid, task_id, alpha pass/fail counts) tuples for hypotheses
    created in the window. Approximates round history.
    """
    sql = text(f"""
        SELECT
            h.id, h.status, h.region, h.created_at,
            COUNT(a.id) FILTER (WHERE a.id IS NOT NULL) AS alpha_n,
            COUNT(a.id) FILTER (WHERE a.quality_status = 'PASS') AS pass_n,
            COUNT(a.id) FILTER (WHERE a.quality_status = 'FAIL') AS fail_n,
            COUNT(DISTINCT a.task_id) AS task_n,
            h.parent_hypothesis_id
        FROM hypotheses h
        LEFT JOIN alphas a ON a.hypothesis_id = h.id
        WHERE h.created_at > NOW() - INTERVAL '{days} days'
        GROUP BY h.id
        ORDER BY h.created_at
    """)
    return list((await db.execute(sql)).all())


async def supersede_chain_depth(db, days: int) -> dict:
    """How deep are the refinement chains? Each SUPERSEDED hypothesis
    points to a child; a long chain = many B6 fires per concept.
    """
    sql = text(f"""
        WITH RECURSIVE chain AS (
            SELECT id, parent_hypothesis_id, 1 AS depth
            FROM hypotheses
            WHERE parent_hypothesis_id IS NULL
              AND created_at > NOW() - INTERVAL '{days} days'
            UNION ALL
            SELECT h.id, h.parent_hypothesis_id, c.depth + 1
            FROM hypotheses h
            JOIN chain c ON h.parent_hypothesis_id = c.id
        )
        SELECT depth, COUNT(*) FROM chain GROUP BY depth ORDER BY depth
    """)
    out = {}
    for row in (await db.execute(sql)).all():
        out[row[0]] = row[1]
    return out


async def b6_qualified_count(db, days: int) -> dict:
    """Hypothesis with ≥ N alpha rounds AND 0 PASS = candidates that
    *would have* qualified for B6 abandon if attribution were
    hypothesis. Joined against terminal status to see the actual path.

    Caveat: we don't persist round-level attribution, so this counts
    hypotheses with ≥ N alphas and 0 PASS regardless of attribution.
    True B6 qualification is a subset (only hypothesis-attribution
    rounds count). The runtime [B6 abandon-skip] log distinguishes.
    """
    n = HYPOTHESIS_ABANDON_ROUNDS
    sql = text(f"""
        SELECT h.status,
               COUNT(*) FILTER (WHERE sub.alpha_n >= :n AND sub.pass_n = 0) AS qualified,
               COUNT(*) AS total
        FROM hypotheses h
        JOIN (
            SELECT hypothesis_id,
                   COUNT(*) AS alpha_n,
                   COUNT(*) FILTER (WHERE quality_status='PASS') AS pass_n
            FROM alphas
            WHERE hypothesis_id IS NOT NULL
              AND created_at > NOW() - INTERVAL '{days} days'
            GROUP BY hypothesis_id
        ) sub ON sub.hypothesis_id = h.id
        WHERE h.created_at > NOW() - INTERVAL '{days} days'
        GROUP BY h.status
    """)
    out = {}
    for r in (await db.execute(sql, {"n": n})).all():
        out[r[0]] = {"qualified": r[1], "total": r[2]}
    return out


async def main(days: int) -> None:
    print(f"=== V-24.A Hypothesis abandon path audit (last {days}d) ===\n")
    print(f"HYPOTHESIS_ABANDON_ROUNDS = {HYPOTHESIS_ABANDON_ROUNDS}\n")

    async with AsyncSessionLocal() as db:
        lifecycle = await lifecycle_breakdown(db, days)
        clusters = await hypothesis_alpha_clusters(db, days)
        chains = await supersede_chain_depth(db, days)
        qualified = await b6_qualified_count(db, days)

    # 1. Status breakdown
    total = sum(lifecycle.values()) or 1
    print("## Lifecycle status distribution")
    for s in ("PROPOSED", "ACTIVE", "PROMOTED", "SUPERSEDED", "ABANDONED"):
        n = lifecycle.get(s, 0)
        pct = n / total * 100
        print(f"  {s:12s} {n:4d}  ({pct:5.1f}%)")
    print(f"  {'TOTAL':12s} {total:4d}")
    print()

    # 2. Retirement breakdown (the metric Trigger 2 actually uses)
    abandoned = lifecycle.get("ABANDONED", 0)
    superseded = lifecycle.get("SUPERSEDED", 0)
    retired = abandoned + superseded
    print("## Retirement composition (Trigger 2 numerator)")
    print(f"  ABANDONED (direct):     {abandoned:4d}")
    print(f"  SUPERSEDED (via refine):{superseded:4d}")
    print(f"  Total retired:          {retired:4d} ({retired/total*100:5.1f}%)")
    if retired:
        super_share = superseded / retired * 100
        print(f"  G-refine share of retirements: {super_share:.1f}%")
    print()

    # 3. Chain depth — proxy for "B6 fired multiple times on same lineage"
    print("## Refinement chain depth (SUPERSEDED → child)")
    if chains:
        for depth, n in sorted(chains.items()):
            print(f"  depth {depth}: {n}")
    else:
        print("  (none)")
    print()

    # 4. B6-qualified candidates
    print(f"## B6-qualified hypotheses (≥{HYPOTHESIS_ABANDON_ROUNDS} alpha, 0 PASS)")
    print(f"  Caveat: attribution check is at runtime only — this is the")
    print(f"  upper bound. Real B6 fires require attribution=hypothesis.")
    if qualified:
        for status, stats in qualified.items():
            q, t = stats["qualified"], stats["total"]
            pct = q / t * 100 if t else 0
            print(f"  {status:12s} qualified={q:4d} / total={t:4d}  ({pct:.1f}%)")
    print()

    # 5. Alpha-rich vs alpha-poor hypotheses
    rich = sum(1 for r in clusters if r.alpha_n >= 5)
    poor = sum(1 for r in clusters if r.alpha_n < 5)
    zero = sum(1 for r in clusters if r.alpha_n == 0)
    print("## Alpha-linkage health")
    print(f"  Hypotheses with ≥5 alpha:  {rich:4d}")
    print(f"  Hypotheses with 1-4 alpha: {poor - zero:4d}")
    print(f"  Hypotheses with 0 alpha:   {zero:4d}  ← orphaned, no signal")
    print()

    # 5b. Time-bucketed orphaned rate — high orphan in older buckets is
    # likely pre-V-19.7 (2026-05-06) zombie sibling rows; recent should
    # be ~0 if the V-19.7 single-primary fix is holding.
    async with AsyncSessionLocal() as db:
        sql = text(f"""
            SELECT DATE(h.created_at) AS d,
                   COUNT(*) AS h_n,
                   COUNT(*) FILTER (WHERE a_cnt.n IS NULL OR a_cnt.n = 0) AS orphaned_n
            FROM hypotheses h
            LEFT JOIN (
                SELECT hypothesis_id, COUNT(*) n FROM alphas
                WHERE hypothesis_id IS NOT NULL GROUP BY hypothesis_id
            ) a_cnt ON a_cnt.hypothesis_id = h.id
            WHERE h.created_at > NOW() - INTERVAL '{days} days'
            GROUP BY d ORDER BY d DESC
        """)
        date_buckets = list((await db.execute(sql)).all())
    print("## Orphan rate by creation date (V-19.7 deployed 2026-05-06)")
    for row in date_buckets:
        rate = row.orphaned_n / row.h_n * 100 if row.h_n else 0
        flag = "✅" if rate < 30 else ("⚠" if rate < 70 else "❌")
        print(f"  {row.d}  h={row.h_n:4d}  orphaned={row.orphaned_n:4d}  ({rate:5.1f}% {flag})")
    print()

    # 6. Recommendation
    print("## Recommendation")
    if abandoned == 0 and superseded > 0:
        print("  ✅ B6 mechanism IS working — every fire converts to SUPERSEDED")
        print("    via G-refine loop. Trigger 2 retirement formula is correct.")
        print()
        print("  Secondary finding — orphan rate by creation date:")
        print("    pre-2026-05-06 buckets show high orphan % (V-19.7 multi-sibling")
        print("    bug); recent buckets should be <30% if V-19.7 is holding.")
        print()
        print("  If you want non-zero ABANDONED:")
        print("    - Disable G-refine for control experiments (compare both)")
        print("    - Lower refine_chain_depth ceiling so deep chains abandon")
    elif abandoned == 0 and superseded == 0:
        print("  ⚠ Neither ABANDONED nor SUPERSEDED fires. Possible causes:")
        print("    - Hypothesis lifecycle short-circuits before B6 sees it")
        print("    - hypothesis_round_history not accumulating ≥3 entries")
        print("    - All hypotheses get a PASS within 2 rounds (success path)")
        print("  Run grep '\\[B6 abandon-' logs/celery.log for runtime logs.")
    else:
        print(f"  ABANDONED={abandoned} SUPERSEDED={superseded} — mixed terminal paths.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=14)
    args = p.parse_args()
    asyncio.run(main(args.days))
