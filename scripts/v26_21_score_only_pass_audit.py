"""V-26.21 — audit PASS alphas that took the score-only branch.

evaluation.py:903 reads:

    if hard_gate_pass and (meets_thresholds or score >= score_pass_threshold):

So an alpha can land PASS via the right-hand `score >= score_pass_threshold`
branch even when `meets_thresholds` is False (meaning BRAIN reported
failed_checks or the local thresholds were missed). The original review
flagged this as risky because the downstream BRAIN-aware downgrade only
covers LOW_FITNESS / LOW_SHARPE / CONCENTRATED_WEIGHT — alphas failing
HIGH_TURNOVER / LOW_TURNOVER / MATCHES_PYRAMID / HIGH_CORRELATION can
still slip through PASS.

This script quantifies the slip-through rate by joining alphas to the
captured `metrics._brain_failed_checks` snapshot (recorded at evaluate
time) and reporting how many PASS rows fail a check that the downgrade
heuristic currently ignores.

Usage:
    venv/Scripts/python.exe scripts/v26_21_score_only_pass_audit.py
    venv/Scripts/python.exe scripts/v26_21_score_only_pass_audit.py --days 7
    venv/Scripts/python.exe scripts/v26_21_score_only_pass_audit.py --json
"""
from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, and_, or_

from backend.database import AsyncSessionLocal
from backend.models import Alpha


# The set of BRAIN check names that currently trigger PASS → PROVISIONAL
# in evaluation.py:928-931. Anything outside this set silently slips by.
DOWNGRADE_COVERED = {"LOW_FITNESS", "LOW_SHARPE", "CONCENTRATED_WEIGHT"}

# Check names we'd want to add to the downgrade set (V-26.21 mitigation).
DOWNGRADE_PROPOSED = {
    "HIGH_TURNOVER",
    "LOW_TURNOVER",
    "MATCHES_PYRAMID",
    "HIGH_CORRELATION",
    "SELF_CORRELATION",
}


async def audit(days: int = 30, as_json: bool = False) -> dict:
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)

    async with AsyncSessionLocal() as db:
        stmt = (
            select(Alpha)
            .where(
                Alpha.quality_status == "PASS",
                Alpha.created_at >= cutoff,
            )
            .order_by(Alpha.created_at.desc())
        )
        rows = (await db.execute(stmt)).scalars().all()

    total_pass = len(rows)
    with_brain_data = 0
    slip_through_counts: Counter = Counter()
    slip_through_alphas: list[dict] = []
    covered_already = 0

    for a in rows:
        metrics = a.metrics or {}
        if not isinstance(metrics, dict):
            continue
        failed = metrics.get("_brain_failed_checks") or []
        if not failed:
            # No BRAIN check data — score-only branch may still apply but
            # we can't see it from this side. Skip.
            continue
        with_brain_data += 1

        fail_names = {c.get("name") for c in failed if isinstance(c, dict)}
        proposed_hits = fail_names & DOWNGRADE_PROPOSED
        covered_hits = fail_names & DOWNGRADE_COVERED

        if proposed_hits and not covered_hits:
            # Slipped through because the only failures are uncovered ones.
            for n in proposed_hits:
                slip_through_counts[n] += 1
            slip_through_alphas.append({
                "alpha_id": a.alpha_id,
                "sharpe": a.is_sharpe,
                "fitness": a.is_fitness,
                "turnover": a.is_turnover,
                "factor_tier": a.factor_tier,
                "region": a.region,
                "fail_names": sorted(fail_names),
                "created_at": a.created_at.isoformat() if a.created_at else None,
            })
        elif covered_hits:
            covered_already += 1

    report = {
        "window_days": days,
        "total_pass": total_pass,
        "pass_with_brain_data": with_brain_data,
        "covered_by_current_downgrade": covered_already,
        "slip_through_count": len(slip_through_alphas),
        "slip_through_by_check": dict(slip_through_counts),
        "slip_through_rate_pct": (
            round(100.0 * len(slip_through_alphas) / with_brain_data, 2)
            if with_brain_data else 0.0
        ),
        "examples": slip_through_alphas[:20],
    }

    if as_json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(f"V-26.21 score-only PASS audit ({days}d window)")
        print(f"  total PASS:                       {report['total_pass']}")
        print(f"  PASS with BRAIN check data:       {report['pass_with_brain_data']}")
        print(f"  already covered by downgrade:     {report['covered_by_current_downgrade']}")
        print(f"  slipped through (uncovered fail): {report['slip_through_count']} "
              f"({report['slip_through_rate_pct']}%)")
        if report["slip_through_by_check"]:
            print("  by check:")
            for k, v in sorted(report["slip_through_by_check"].items(), key=lambda x: -x[1]):
                print(f"    {k:24s}  {v}")
        if report["examples"]:
            print(f"  top {min(len(report['examples']), 5)} examples:")
            for ex in report["examples"][:5]:
                print(f"    alpha={ex['alpha_id']} sharpe={ex['sharpe']} "
                      f"turnover={ex['turnover']} fails={ex['fail_names']}")

    return report


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--json", action="store_true")
    args = p.parse_args()
    asyncio.run(audit(days=args.days, as_json=args.json))
