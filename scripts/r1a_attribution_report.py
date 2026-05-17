"""R1a hook attribution observation report (Phase 0, 2026-05-17).

Reports the 5 KPI fields defined in plan v1.3 §1.7:
    - total                     hook_version rows (success + fail combined)
    - non_null_pct              attribution != NULL / total
    - non_unknown_pct           actionable enum (hypothesis|implementation|both)
                                / non_null
    - errs_count                hook internal exception count (_r1a_hook_error)
    - production_crash_count    mining_tasks.status='FAILED' pct delta vs
                                30-day baseline (pre-flag-flip)

Phase 0 GO gate (plan v1.3 §7):
    total >= 200 (--min-triggers)
    non_null_pct >= 95%
    non_unknown_pct >= 70% (subject to mid-point review, SF-2 fix)
    errs_count < 10
    production_crash_count delta <= +10%

CLI:
    python scripts/r1a_attribution_report.py
        Full report, GO gate threshold 200, exit 0 if all PASS else 1.

    python scripts/r1a_attribution_report.py --min-triggers 200
        Override the GO gate threshold.

    python scripts/r1a_attribution_report.py --days 14
        Calendar-window debug mode (vs full-dataset default).

    python scripts/r1a_attribution_report.py --midpoint-check
        Triggers SF-2 threshold recalibration suggestion at min_triggers/4.
        Silent (exit 0) until that data volume is reached.

Stall detection (SF-9 / MF-5 soft-guard):
    Always checks "is there a new hook_version row in the last 1h?" and
    emits a stderr [R1a-stall] warning if 0 — flag may have been
    flipped OFF, mining tasks PAUSED, or DB write broken. The exit code
    is NOT affected by stall (only the 5 KPIs gate it).

Plan reference: ~/.claude/plans/docs-master-implementation-plan-2026-05-compressed-shore.md §1.7
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from backend.database import AsyncSessionLocal  # noqa: E402

ACTIONABLE_ATTRS = ("hypothesis", "implementation", "both")
DEFAULT_MIN_TRIGGERS = 200
STALL_WINDOW_HOURS = 1


async def collect_attribution(days: Optional[int] = None) -> Dict[str, int]:
    """Return {attr_str_or_None: count} + an 'errs' tally as a separate row.

    v1.6 (2026-05-17): query independent `r1a_attribution_log` table
    instead of alpha.metrics. The log captures every evaluated alpha
    (50/round) — `alphas` table only had PROV/PASS subset (~1/round).

    `attr` keys: 'hypothesis', 'implementation', 'both', 'unknown', None
    `errs` key (special): rows where hook_error is set
    """
    if days is not None:
        sql = text("""
            SELECT attribution AS attr,
                   COUNT(*) AS n,
                   COUNT(*) FILTER (WHERE hook_error IS NOT NULL) AS errs
            FROM r1a_attribution_log
            WHERE created_at > now() - (:days || ' day')::interval
            GROUP BY 1 ORDER BY n DESC
        """)
        params = {"days": str(days)}
    else:
        sql = text("""
            SELECT attribution AS attr,
                   COUNT(*) AS n,
                   COUNT(*) FILTER (WHERE hook_error IS NOT NULL) AS errs
            FROM r1a_attribution_log
            GROUP BY 1 ORDER BY n DESC
        """)
        params = {}

    out: Dict[str, int] = {}
    errs = 0
    async with AsyncSessionLocal() as s:
        r = await s.execute(sql, params)
        for attr, n, e in r.all():
            out[attr if attr is not None else "__NULL__"] = int(n)
            errs += int(e or 0)
    out["__ERRS__"] = errs
    return out


async def check_stall() -> int:
    """Return count of hook log rows in last STALL_WINDOW_HOURS."""
    sql = text("""
        SELECT COUNT(*) FROM r1a_attribution_log
        WHERE created_at > now() - (:hrs || ' hour')::interval
    """)
    async with AsyncSessionLocal() as s:
        r = await s.execute(sql, {"hrs": str(STALL_WINDOW_HOURS)})
        return int(r.scalar() or 0)


async def production_crash_delta() -> Optional[float]:
    """Return (post_flip FAILED pct) - (30-day-pre-flip FAILED pct), or None.

    None if the flag override row is missing (flag never explicitly flipped
    via DB override, fallback to default False).
    """
    flip_sql = text("""
        SELECT updated_at FROM feature_flag_overrides
        WHERE flag_name='ENABLE_R1A_HOOK'
    """)
    async with AsyncSessionLocal() as s:
        r = await s.execute(flip_sql)
        flip_at = r.scalar()
    if flip_at is None:
        return None

    # CAST :flip to timestamptz because feature_flag_overrides.updated_at is
    # stored naive while mining_tasks.created_at carries a tz — asyncpg
    # rejects the heterogeneous comparison without explicit cast.
    baseline_sql = text("""
        SELECT (COUNT(*) FILTER (WHERE status='FAILED'))::float
               / NULLIF(COUNT(*), 0) AS pct
        FROM mining_tasks
        WHERE created_at > (cast(:flip as timestamptz)) - interval '30 day'
          AND created_at < cast(:flip as timestamptz)
    """)
    observed_sql = text("""
        SELECT (COUNT(*) FILTER (WHERE status='FAILED'))::float
               / NULLIF(COUNT(*), 0) AS pct
        FROM mining_tasks
        WHERE created_at >= cast(:flip as timestamptz)
    """)
    async with AsyncSessionLocal() as s:
        baseline = (await s.execute(baseline_sql, {"flip": flip_at})).scalar() or 0.0
        observed = (await s.execute(observed_sql, {"flip": flip_at})).scalar() or 0.0
    return float(observed) - float(baseline)


def compute_kpis(dist: Dict[str, int]) -> Dict[str, float]:
    total = sum(v for k, v in dist.items() if k != "__ERRS__")
    null_count = dist.get("__NULL__", 0)
    non_null = total - null_count
    non_unknown = sum(dist.get(a, 0) for a in ACTIONABLE_ATTRS)
    errs = dist.get("__ERRS__", 0)
    return {
        "total": float(total),
        "non_null_pct": (non_null / total) if total else 0.0,
        "non_unknown_pct": (non_unknown / non_null) if non_null else 0.0,
        "errs_count": float(errs),
    }


def midpoint_suggestion(kpis: Dict[str, float]) -> str:
    """SF-2 fix: suggest a recalibrated non_unknown_pct threshold."""
    obs = kpis["non_unknown_pct"]
    if obs >= 0.70:
        return f"non_unknown_pct={obs:.1%} ≥ 70% target — no recalibration needed."
    elif obs >= 0.40:
        return (
            f"non_unknown_pct={obs:.1%} below 70% target. "
            f"Suggest relaxing GO threshold to ≥ {max(int(obs * 100 // 5) * 5, 40)}% "
            f"(heuristic landed in UNKNOWN bucket — see alignment.py:351-380)."
        )
    else:
        return (
            f"non_unknown_pct={obs:.1%} severely below target. "
            f"Investigate: hypothesis field empty? sharpe >= 0.5 dominates? "
            f"Suggest relaxing GO threshold to ≥ 20-30%."
        )


async def main_async(args: argparse.Namespace) -> int:
    dist = await collect_attribution(days=args.days)
    kpis = compute_kpis(dist)

    # Stall detection — always run regardless of mode
    stall_count = await check_stall()
    if stall_count == 0:
        print(
            f"[R1a-stall] no new hook rows in last {STALL_WINDOW_HOURS}h, "
            f"check flag state + mining_tasks.status",
            file=sys.stderr,
        )

    # production_crash_delta — only if flag flip recorded
    crash_delta = await production_crash_delta()

    # Report
    print(f"R1a attribution report ({'days=' + str(args.days) if args.days else 'full dataset'})")
    print(f"  total            = {int(kpis['total'])}")
    for attr in ("hypothesis", "implementation", "both", "unknown"):
        print(f"  attr={attr:<14} = {dist.get(attr, 0)}")
    print(f"  attr=NULL          = {dist.get('__NULL__', 0)} (hook fail path)")
    print(f"  non_null_pct     = {kpis['non_null_pct']:.1%}")
    print(f"  non_unknown_pct  = {kpis['non_unknown_pct']:.1%}")
    print(f"  errs_count       = {int(kpis['errs_count'])}")
    if crash_delta is not None:
        print(f"  prod_crash_delta = {crash_delta:+.1%} vs 30-day baseline")
    else:
        print(f"  prod_crash_delta = N/A (flag never flipped via DB override)")

    # SF-9 mid-point check
    if args.midpoint_check:
        midpoint_threshold = args.min_triggers // 4
        if kpis["total"] >= midpoint_threshold:
            print(f"\n[mid-point @ total={int(kpis['total'])} ≥ {midpoint_threshold}]")
            print(f"  {midpoint_suggestion(kpis)}")
        else:
            return 0  # Silent below midpoint threshold

    # GO gate evaluation
    failed: list[str] = []
    if kpis["total"] < args.min_triggers:
        failed.append(f"total={int(kpis['total'])} < min_triggers={args.min_triggers}")
    if kpis["non_null_pct"] < 0.95:
        failed.append(f"non_null_pct={kpis['non_null_pct']:.1%} < 95%")
    if kpis["non_unknown_pct"] < 0.70:
        failed.append(f"non_unknown_pct={kpis['non_unknown_pct']:.1%} < 70% (run --midpoint-check for recalibration)")
    if kpis["errs_count"] >= 10:
        failed.append(f"errs_count={int(kpis['errs_count'])} >= 10")
    if crash_delta is not None and crash_delta > 0.10:
        failed.append(f"prod_crash_delta={crash_delta:+.1%} > +10%")

    if failed:
        print(f"\nGO gate: FAIL", file=sys.stderr)
        for f in failed:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print(f"\nGO gate: PASS — Phase 0 R1a observation complete")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--days", type=int, default=None,
                   help="Calendar-window mode (default: full dataset)")
    p.add_argument("--min-triggers", type=int, default=DEFAULT_MIN_TRIGGERS,
                   help=f"GO gate trigger threshold (default {DEFAULT_MIN_TRIGGERS})")
    p.add_argument("--midpoint-check", action="store_true",
                   help="Emit SF-2 threshold recalibration suggestion at min_triggers/4")
    args = p.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
