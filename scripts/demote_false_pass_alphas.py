"""Find PASS alphas whose BRAIN /check fields contradict their PASS status.

Why this exists: backfill_factor_tier_dryrun.py's _recompute_quality_status
only inspects sharpe / fitness / turnover / sub_universe — it never looks
at metrics.checks for CONCENTRATED_WEIGHT or LOW_SHARPE etc. Some alphas
were marked PASS during early eval iterations BEFORE
node_evaluate added the concentrated_weight rule (Post-Step1, 2026-04-30),
so the backfill couldn't catch them.

This script applies the FULL hard-gate (mirrors evaluation.node_evaluate's
T2/T3 logic where check_concentrated=True). For each alpha currently PASS:
- if metrics.checks contains CONCENTRATED_WEIGHT=FAIL → demote to PROVISIONAL
- if metrics.checks contains LOW_SHARPE=FAIL → demote to PROVISIONAL
- if metrics.checks contains LOW_SUB_UNIVERSE_SHARPE=FAIL → demote to PROVISIONAL

Tier 1 alphas SKIP concentrated and low_sharpe checks per design (raw signal
seeds, not submission-ready). Only T2/T3/None are subject to full gate.

Modes:
  --preview   list affected alphas without writing
  --confirm   actually apply demotions via apply_quality_status_change
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select


def _failed_checks(metrics: dict) -> List[Tuple[str, Optional[float], Optional[float]]]:
    """Extract failing check entries from metrics. Returns
    [(name, value, limit), ...] for entries with result=='FAIL'."""
    if not isinstance(metrics, dict):
        return []
    checks = metrics.get("checks") or []
    out: List[Tuple[str, Optional[float], Optional[float]]] = []
    for c in checks:
        if not isinstance(c, dict):
            continue
        if c.get("result") == "FAIL":
            out.append((c.get("name", "?"), c.get("value"), c.get("limit")))
    return out


def _gate_applies_check(check_name: str, factor_tier: Optional[int]) -> bool:
    """Per-tier gate logic mirroring evaluation.node_evaluate's check_*
    settings AND the project-level tier thresholds (which deliberately
    diverge from BRAIN's submission-level thresholds).

    T1 PASS bar (project-level): sharpe>=0.8, fitness>=0.5, turnover<=0.70,
    sub_universe>=0.1. Concentrated_weight and self_corr explicitly skipped.
    BRAIN's metrics.checks reflects SUBMISSION-level thresholds (sharpe>=1.25
    etc.) which are stricter — those FAILs DON'T mean a T1 alpha isn't a
    valid T1 PASS, just that it can't be submitted as-is. T1 is a seed
    factory, not a submission candidate, so we skip the entire BRAIN check
    panel for T1.

    T2 still skips self_corr (same-seed wrapper variants are inherently
    correlated). Other BRAIN checks (concentrated, low_sharpe at 1.25 bar)
    apply to T2 because T2 is supposed to be more polished than T1.

    T3 mirrors evaluation.node_evaluate fully — every BRAIN check applies.
    """
    if factor_tier == 1:
        # Skip ALL BRAIN-submission checks for T1 — project thresholds rule.
        return False
    if factor_tier == 2 and check_name == "SELF_CORRELATION":
        return False
    return True


async def survey() -> List[Dict]:
    """Return the demotion candidates. Each: {id, alpha_id, factor_tier,
    quality_status, fails: [(name, val, lim), ...]}."""
    from backend.database import AsyncSessionLocal
    from backend.models import Alpha

    async with AsyncSessionLocal() as db:
        q = (
            select(Alpha.id, Alpha.alpha_id, Alpha.factor_tier,
                   Alpha.quality_status, Alpha.is_sharpe, Alpha.metrics)
            .where(Alpha.quality_status == "PASS")
        )
        rows = (await db.execute(q)).all()

    candidates: List[Dict] = []
    for row in rows:
        alpha_id_db, brain_id, tier, status, sharpe, metrics = row
        all_fails = _failed_checks(metrics or {})
        # Filter to checks that ACTUALLY apply at this tier
        tier_fails = [
            (n, v, l) for n, v, l in all_fails
            if _gate_applies_check(n, tier)
            and n not in ("PROD_CORRELATION",)  # ignore production-corr (advisory)
        ]
        if tier_fails:
            candidates.append({
                "id": alpha_id_db,
                "alpha_id": brain_id,
                "factor_tier": tier,
                "current_status": status,
                "sharpe": sharpe,
                "fails": tier_fails,
            })
    return candidates


async def preview() -> None:
    cands = await survey()
    print("=" * 84)
    print("False-PASS demotion preview")
    print("=" * 84)
    if not cands:
        print("No demotion candidates — every PASS alpha clears its tier-applicable checks.")
        return

    print(f"Found {len(cands)} PASS alphas with applicable BRAIN-check failures:\n")
    for c in cands:
        tier_str = f"T{c['factor_tier']}" if c["factor_tier"] else "NULL"
        fail_strs = []
        for name, val, lim in c["fails"]:
            v = f"{val:.3f}" if isinstance(val, (int, float)) else str(val)
            l = f"{lim:.3f}" if isinstance(lim, (int, float)) else str(lim)
            fail_strs.append(f"{name}=FAIL(val={v}, lim={l})")
        print(f"  alpha #{c['id']:<5} brain={c['alpha_id']:<10} tier={tier_str:<5} sharpe={c['sharpe']:.2f}")
        print(f"    fails: {' | '.join(fail_strs)}")


async def apply() -> None:
    from backend.database import AsyncSessionLocal
    from backend.services.alpha_service import AlphaService

    cands = await survey()
    if not cands:
        print("Nothing to demote.")
        return

    print(f"Demoting {len(cands)} alphas from PASS → PASS_PROVISIONAL...")

    async with AsyncSessionLocal() as db:
        svc = AlphaService(db)
        applied = 0
        for c in cands:
            fail_summary = ",".join(name for name, _, _ in c["fails"])
            try:
                changed = await svc.apply_quality_status_change(
                    alpha_id=c["id"],
                    new_status="PASS_PROVISIONAL",
                    reason=f"false_pass_audit: applicable BRAIN checks failed [{fail_summary}]",
                    source="manual_api",
                )
                if changed:
                    applied += 1
            except Exception as e:
                print(f"  alpha #{c['id']} demote failed: {e}")
        await db.commit()
    print(f"Demoted {applied}/{len(cands)} (others were already non-PASS)")


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", action="store_true",
                        help="Apply the demotions (default is dry-run preview)")
    args = parser.parse_args()
    try:
        asyncio.run(apply() if args.confirm else preview())
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    cli()
