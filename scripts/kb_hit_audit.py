"""V-24.D — KB pattern hit-rate audit.

Reports which KB entries are actually getting used by the LLM at retrieve
time. Before V-24.D, `usage_count` was bumped only on record-time pattern
re-discovery; retrieval was free-rides. V-24.D's _track_retrieval_hit
now bumps usage_count + updated_at on every selected entry, so this
script can distinguish:

  - Hot patterns: high usage_count + recent updated_at → real signal
  - Lukewarm: medium usage_count, stale updated_at → was useful once,
    now eclipsed by newer entries
  - Cold: low usage_count, old updated_at → pruning candidates

Use:
  venv/Scripts/python.exe scripts/kb_hit_audit.py
  venv/Scripts/python.exe scripts/kb_hit_audit.py --cold-days 30
  venv/Scripts/python.exe scripts/kb_hit_audit.py --top 100 --bottom 100

Output: top/bottom tables + cold-rate summary by entry_type/tier.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from backend.database import AsyncSessionLocal


async def summary(db, cold_days: int) -> dict:
    sql = text(f"""
        SELECT
            entry_type,
            factor_tier,
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE usage_count = 0) AS never_used,
            COUNT(*) FILTER (
                WHERE updated_at < NOW() - INTERVAL '{cold_days} days'
            ) AS cold,
            AVG(usage_count)::numeric(10,2) AS mean_usage,
            MAX(usage_count) AS max_usage
        FROM knowledge_entries
        WHERE is_active = true
        GROUP BY entry_type, factor_tier
        ORDER BY entry_type, factor_tier NULLS LAST
    """)
    rows = (await db.execute(sql)).all()
    return rows


async def top_patterns(db, top_n: int):
    sql = text(f"""
        SELECT id, entry_type, factor_tier, usage_count, updated_at,
               LEFT(COALESCE(pattern, description, ''), 80) AS sample
        FROM knowledge_entries
        WHERE is_active = true
        ORDER BY usage_count DESC NULLS LAST
        LIMIT :top_n
    """)
    return (await db.execute(sql, {"top_n": top_n})).all()


async def bottom_patterns(db, bottom_n: int, cold_days: int):
    """Patterns most likely pruning candidates: 0 usage AND old."""
    sql = text(f"""
        SELECT id, entry_type, factor_tier, usage_count, updated_at,
               LEFT(COALESCE(pattern, description, ''), 80) AS sample
        FROM knowledge_entries
        WHERE is_active = true
          AND usage_count = 0
          AND created_at < NOW() - INTERVAL '{cold_days} days'
        ORDER BY created_at ASC
        LIMIT :bottom_n
    """)
    return (await db.execute(sql, {"bottom_n": bottom_n})).all()


async def main(top_n: int, bottom_n: int, cold_days: int) -> None:
    print(f"=== V-24.D KB hit-rate audit (cold threshold: {cold_days}d) ===\n")
    async with AsyncSessionLocal() as db:
        sm = await summary(db, cold_days)
        tops = await top_patterns(db, top_n)
        bots = await bottom_patterns(db, bottom_n, cold_days)

    # Summary table
    print("## Aggregate by (entry_type, factor_tier)")
    print(f"  {'type':<22s} {'tier':>4s} {'total':>6s} {'never_used':>10s} "
          f"{'cold':>5s} {'mean_uc':>8s} {'max_uc':>7s}")
    grand_total = 0
    grand_never = 0
    grand_cold = 0
    for r in sm:
        tier_s = str(r.factor_tier) if r.factor_tier is not None else "—"
        print(f"  {r.entry_type:<22s} {tier_s:>4s} {r.total:>6d} "
              f"{r.never_used:>10d} {r.cold:>5d} "
              f"{r.mean_usage:>8.2f} {r.max_usage:>7d}")
        grand_total += r.total
        grand_never += r.never_used
        grand_cold += r.cold
    print(f"  {'TOTAL':<22s}  {'  ':>4s} {grand_total:>6d} "
          f"{grand_never:>10d} {grand_cold:>5d}")
    print()

    if grand_total:
        never_pct = grand_never / grand_total * 100
        cold_pct = grand_cold / grand_total * 100
        print(f"  never_used:  {grand_never}/{grand_total} ({never_pct:.1f}%)")
        print(f"  cold (no activity in {cold_days}d): {grand_cold}/{grand_total} "
              f"({cold_pct:.1f}%)")
        print()

    # Top
    print(f"## Top {top_n} by usage_count")
    print(f"  {'id':>6s} {'type':<22s} {'tier':>4s} {'uc':>5s} {'updated':<20s} sample")
    for r in tops:
        tier_s = str(r.factor_tier) if r.factor_tier is not None else "—"
        ts = r.updated_at.strftime("%Y-%m-%d %H:%M") if r.updated_at else "—"
        print(f"  {r.id:>6d} {r.entry_type:<22s} {tier_s:>4s} {r.usage_count:>5d} "
              f"{ts:<20s} {r.sample!s}")
    print()

    # Bottom (pruning candidates)
    print(f"## Bottom {bottom_n} pruning candidates (uc=0, created >{cold_days}d ago)")
    if not bots:
        print("  (none — no obviously-cold patterns to prune)")
    else:
        for r in bots:
            ts = r.updated_at.strftime("%Y-%m-%d %H:%M") if r.updated_at else "—"
            tier_s = str(r.factor_tier) if r.factor_tier is not None else "—"
            print(f"  {r.id:>6d} {r.entry_type:<22s} {tier_s:>4s} "
                  f"{ts:<20s} {r.sample!s}")
    print()

    # Verdict
    if grand_total:
        if cold_pct > 60:
            print(f"## Verdict")
            print(f"  ⚠ {cold_pct:.0f}% KB rows have had no activity in {cold_days}d.")
            print(f"  Consider running a scheduled soft-deactivate sweep on")
            print(f"  knowledge_entries WHERE updated_at < NOW() - INTERVAL '{cold_days} days'")
            print(f"  AND usage_count = 0.")
        elif never_pct > 30:
            print(f"## Verdict")
            print(f"  ⚠ {never_pct:.0f}% KB rows never retrieved. Possibly:")
            print(f"    - record_pattern path runs too aggressively (over-recording)")
            print(f"    - retrieve filters too strict (region/dataset/variant)")
            print(f"    - patterns recorded for non-existent ops (V-22.8 sweep)")
        else:
            print(f"## Verdict")
            print(f"  ✅ Healthy KB activity — {100 - cold_pct:.0f}% rows seen "
                  f"recently, mean usage {sum(r.mean_usage for r in sm)/max(len(sm),1):.1f}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--top", type=int, default=15)
    p.add_argument("--bottom", type=int, default=15)
    p.add_argument("--cold-days", type=int, default=30)
    args = p.parse_args()
    asyncio.run(main(args.top, args.bottom, args.cold_days))
