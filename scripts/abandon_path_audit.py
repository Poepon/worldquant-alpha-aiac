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


# V-25.A (2026-05-13): exclude V-19.7 zombie-cleanup rows. Those 290
# SUPERSEDED rows were a one-shot manual transition on 2026-05-06 — they
# carry abandon_reason starting with "V-19.7 zombie cleanup". Including
# them in lifecycle stats produces a fake 43.4% retirement-rate signal
# that has nothing to do with the live B6 / G-refine pipeline.
ZOMBIE_REASON_PREFIX = "V-19.7 zombie"


async def lifecycle_breakdown(db, days: int, *, exclude_zombies: bool = True) -> dict:
    extra = (
        "AND COALESCE(abandon_reason, '') NOT LIKE :zp"
        if exclude_zombies else ""
    )
    sql = text(f"""
        SELECT status, COUNT(*) FROM hypotheses
        WHERE created_at > NOW() - INTERVAL '{days} days'
        {extra}
        GROUP BY status
    """)
    params = {"zp": f"{ZOMBIE_REASON_PREFIX}%"} if exclude_zombies else {}
    out = {}
    for row in (await db.execute(sql, params)).all():
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


async def linkage_breakdown(db, days: int) -> dict:
    """V-25.A — 3-way orphan classification using status semantics.

    Key insight: status itself is the "was tried" signal. mark_active
    only flips PROPOSED → ACTIVE when B5 sees alpha_count > 0 in the
    round — i.e. code_gen produced ≥1 candidate. So:

      ACTIVE → at minimum the candidates were generated; whether they
              FAILed at validate / simulate is captured in
              alpha_failures (which currently has no hypothesis_id —
              see V-25.B), so we can't count failed-but-tried alphas
              directly. ACTIVE+no_pass = "tried, all failed validation
              / simulation / quality — mining noise, not a bug".
      PROMOTED → ≥1 PASS reached alphas table.
      PROPOSED → mark_active never fired = 0 candidates generated for
                this hid. This is the "never_tried" bucket — usually
                V-22.13 reuse failure replacing it before code_gen ran.
      ABANDONED → B6 should_abandon fired (currently 0 in live data)
      SUPERSEDED → G-refine fired (currently 0 in live data after
                  excluding V-19.7 zombies)
    """
    sql = text(f"""
        WITH base AS (
            SELECT h.id AS hid, h.status
            FROM hypotheses h
            WHERE h.created_at > NOW() - INTERVAL '{days} days'
              AND COALESCE(h.abandon_reason, '') NOT LIKE :zp
        ),
        pass_link AS (
            SELECT hypothesis_id AS hid, COUNT(*) AS n
            FROM alphas
            WHERE hypothesis_id IS NOT NULL
              AND quality_status IN ('PASS', 'PASS_PROVISIONAL')
            GROUP BY hypothesis_id
        )
        SELECT
            base.status,
            COUNT(*) FILTER (WHERE COALESCE(pass_link.n, 0) > 0)
                AS linked_with_pass,
            COUNT(*) FILTER (
                WHERE base.status = 'ACTIVE'
                  AND COALESCE(pass_link.n, 0) = 0
            ) AS tried_no_pass,
            COUNT(*) FILTER (
                WHERE base.status = 'PROPOSED'
            ) AS never_tried,
            COUNT(*) AS total
        FROM base
        LEFT JOIN pass_link ON pass_link.hid = base.hid
        GROUP BY base.status
        ORDER BY total DESC
    """)
    rows = (await db.execute(sql, {"zp": f"{ZOMBIE_REASON_PREFIX}%"})).all()
    return [dict(r._mapping) for r in rows]


async def main(days: int) -> None:
    print(f"=== V-24.A Hypothesis abandon path audit (last {days}d) ===\n")
    print(f"HYPOTHESIS_ABANDON_ROUNDS = {HYPOTHESIS_ABANDON_ROUNDS}\n")
    print(f"V-25.A: V-19.7 zombie-cleanup rows (one-shot 2026-05-06)\n"
          f"        EXCLUDED from lifecycle and linkage stats below.\n")

    async with AsyncSessionLocal() as db:
        lifecycle = await lifecycle_breakdown(db, days)
        lifecycle_raw = await lifecycle_breakdown(db, days, exclude_zombies=False)
        clusters = await hypothesis_alpha_clusters(db, days)
        chains = await supersede_chain_depth(db, days)
        qualified = await b6_qualified_count(db, days)
        linkage = await linkage_breakdown(db, days)

    # 1. Status breakdown
    total = sum(lifecycle.values()) or 1
    total_raw = sum(lifecycle_raw.values()) or 1
    zombies = total_raw - total
    print("## Lifecycle status distribution (excl. V-19.7 zombies)")
    for s in ("PROPOSED", "ACTIVE", "PROMOTED", "SUPERSEDED", "ABANDONED"):
        n = lifecycle.get(s, 0)
        n_raw = lifecycle_raw.get(s, 0)
        pct = n / total * 100
        suffix = f"  [+{n_raw - n} zombie]" if n_raw != n else ""
        print(f"  {s:12s} {n:4d}  ({pct:5.1f}%){suffix}")
    print(f"  {'TOTAL':12s} {total:4d}  (excluded {zombies} V-19.7 zombies)")
    print()

    # 1b. V-25.A linkage breakdown — distinguish tried-no-pass vs never-tried
    print("## Alpha linkage by status (V-25.A — orphan composition)")
    print(f"  {'status':<12s} {'with_PASS':>10s} {'tried_no_PASS':>14s} "
          f"{'never_tried':>12s} {'total':>7s}")
    for row in linkage:
        st = row["status"]
        if st == "ABANDONED":
            continue  # all zombies, excluded
        print(
            f"  {st:<12s} {row['linked_with_pass']:>10d} "
            f"{row['tried_no_pass']:>14d} {row['never_tried']:>12d} "
            f"{row['total']:>7d}"
        )
    print(f"\n  legend:")
    print(f"    with_PASS    = hypothesis produced ≥1 PASS alpha (PROMOTED expected)")
    print(f"    tried_no_PASS = pending_alphas was non-empty but 0 made it to alphas table")
    print(f"                   — typical of ACTIVE with all-FAIL rounds (mining noise,")
    print(f"                   NOT a bug). FAIL alphas live in alpha_failures which has")
    print(f"                   no hypothesis_id column (see V-25.B).")
    print(f"    never_tried  = hypothesis created then immediately replaced before any")
    print(f"                   alpha attempt (V-22.13 reuse failure — real bug, V-25.C)")
    print()

    # 2. Retirement breakdown — V-25.A: excludes V-19.7 zombies that
    # were previously inflating this to 43.4%. Real B6/G-refine retirement
    # rate is 0% (mechanism never fired end-to-end in production).
    abandoned = lifecycle.get("ABANDONED", 0)
    superseded = lifecycle.get("SUPERSEDED", 0)
    retired = abandoned + superseded
    print("## Retirement composition (Trigger 2 numerator, excl. zombies)")
    print(f"  ABANDONED (direct):     {abandoned:4d}")
    print(f"  SUPERSEDED (via refine):{superseded:4d}")
    print(f"  Total retired:          {retired:4d} ({retired/total*100:5.1f}%)")
    if retired:
        super_share = superseded / retired * 100
        print(f"  G-refine share of retirements: {super_share:.1f}%")
    else:
        print(f"  ⚠ 0 retirements in live data — B6 / G-refine mechanism not")
        print(f"  firing end-to-end. Previous '43.4%' was V-19.7 zombie cleanup.")
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
        # Should not reach here after V-25.A zombie filter unless G-refine
        # actually starts producing SUPERSEDED rows in production.
        print("  ✅ B6 mechanism producing real SUPERSEDED — G-refine working.")
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
