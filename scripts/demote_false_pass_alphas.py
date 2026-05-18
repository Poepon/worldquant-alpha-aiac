"""Find PASS alphas whose BRAIN /check fields contradict their PASS status.

This script applies the post tier-system removal (2026-05-18) flat hard-gate:
every BRAIN check applies uniformly to every PASS alpha. The pre-removal
per-tier skip logic (T1 ignored all BRAIN-submission checks, T2 ignored
SELF_CORRELATION) is gone — the flat ``EVAL_*`` band in config.py now
applies the same thresholds everywhere.

For each alpha currently PASS, if ``metrics.checks`` contains any FAIL
entry (excluding the advisory PROD_CORRELATION), the alpha is demoted to
PASS_PROVISIONAL with an audit-log entry.

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


# Post tier-system removal: PROD_CORRELATION is the only advisory check we
# exclude — it's evaluated separately via correlation_service, not as a hard
# gate. Every other BRAIN check counts.
_ADVISORY_CHECKS = {"PROD_CORRELATION"}


async def survey() -> List[Dict]:
    """Return the demotion candidates."""
    from backend.database import AsyncSessionLocal
    from backend.models import Alpha

    async with AsyncSessionLocal() as db:
        q = (
            select(Alpha.id, Alpha.alpha_id, Alpha.quality_status,
                   Alpha.is_sharpe, Alpha.metrics)
            .where(Alpha.quality_status == "PASS")
        )
        rows = (await db.execute(q)).all()

    candidates: List[Dict] = []
    for row in rows:
        alpha_id_db, brain_id, status, sharpe, metrics = row
        all_fails = _failed_checks(metrics or {})
        hard_fails = [(n, v, l) for n, v, l in all_fails if n not in _ADVISORY_CHECKS]
        if hard_fails:
            candidates.append({
                "id": alpha_id_db,
                "alpha_id": brain_id,
                "current_status": status,
                "sharpe": sharpe,
                "fails": hard_fails,
            })
    return candidates


async def preview() -> None:
    cands = await survey()
    print("=" * 84)
    print("False-PASS demotion preview")
    print("=" * 84)
    if not cands:
        print("No demotion candidates — every PASS alpha clears its BRAIN checks.")
        return

    print(f"Found {len(cands)} PASS alphas with hard BRAIN-check failures:\n")
    for c in cands:
        fail_strs = []
        for name, val, lim in c["fails"]:
            v = f"{val:.3f}" if isinstance(val, (int, float)) else str(val)
            l = f"{lim:.3f}" if isinstance(lim, (int, float)) else str(lim)
            fail_strs.append(f"{name}=FAIL(val={v}, lim={l})")
        print(f"  alpha #{c['id']:<5} brain={c['alpha_id']:<10} sharpe={c['sharpe']:.2f}")
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
                    reason=f"false_pass_audit: BRAIN checks failed [{fail_summary}]",
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
