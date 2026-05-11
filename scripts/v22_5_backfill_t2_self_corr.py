"""V-22.5 backfill — re-evaluate historical can_submit=True alphas against
the OS-portfolio self-correlation gate.

V-22.5 (e29bf16) enabled T2 self_corr gate at PASS time. Alphas marked
can_submit=True BEFORE that deploy never went through this check. IQC
audit (2026-05-11) showed all 13 net-positive Δscore candidates had
self_corr 0.85-0.99 vs portfolio — BRAIN would reject every one of them.

This script:
  1. Scans active can_submit=True + unsubmitted alphas
  2. Fetches IS PnL series + computes corr vs OS cache via
     CorrelationService.calc_self_corr
  3. corr ≥ threshold (default 0.7):
       - quality_status: PASS → PASS_PROVISIONAL
       - can_submit: True → False
       - metrics._v22_5_backfill: {at, corr, source, reason}
  4. corr < threshold: leave alone (genuinely submittable)
  5. unknown source: leave alone (defensive; couldn't verify either way)

Usage:
  venv/Scripts/python.exe scripts/v22_5_backfill_t2_self_corr.py            # dry-run
  venv/Scripts/python.exe scripts/v22_5_backfill_t2_self_corr.py --apply    # commit
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, update

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.adapters.brain_adapter import BrainAdapter
from backend.database import AsyncSessionLocal
from backend.models import Alpha
from backend.services.correlation_service import CorrelationService


THRESHOLD = 0.7  # match TIER2_SELF_CORR_MAX default


async def main(apply: bool, threshold: float) -> None:
    print(f"=== V-22.5 backfill — T2 self-corr gate ===")
    print(f"Threshold: {threshold}    Mode: {'APPLY' if apply else 'DRY-RUN'}\n")

    stats = {
        "scanned": 0,
        "downgraded": 0,
        "kept_safe": 0,         # corr < threshold, real submittable
        "kept_unknown": 0,      # source==unknown, defensive keep
        "pnl_empty": 0,         # IS PnL not available — skip
        "errors": 0,
    }
    rows_report = []

    async with AsyncSessionLocal() as db:
        stmt = (
            select(Alpha)
            .where(Alpha.can_submit == True)  # noqa: E712
            .where(Alpha.date_submitted.is_(None))
            .order_by(Alpha.is_sharpe.desc().nulls_last())
        )
        alphas = (await db.execute(stmt)).scalars().all()
        print(f"Found {len(alphas)} can_submit=True + unsubmitted alphas\n")

        async with BrainAdapter() as brain:
            svc = CorrelationService(brain)

            for i, alpha in enumerate(alphas):
                stats["scanned"] += 1
                tag = f"[{i + 1}/{len(alphas)}]"
                if not alpha.alpha_id:
                    print(f"  {tag} pk={alpha.id} no brain_id — skip")
                    continue

                # WARM-UP fetch — first call may return empty body if PnL
                # not cached. Second call typically returns full records.
                try:
                    payload = await brain.get_alpha_pnl(alpha.alpha_id)
                    if not payload or not payload.get("records"):
                        # Retry once with brief pause
                        await asyncio.sleep(1.0)
                        payload = await brain.get_alpha_pnl(alpha.alpha_id)
                except Exception as e:
                    print(f"  {tag} {alpha.alpha_id} PnL fetch EXC: {e}")
                    stats["errors"] += 1
                    continue

                if not payload or not payload.get("records"):
                    stats["pnl_empty"] += 1
                    print(f"  {tag} {alpha.alpha_id} PnL empty after retry — skip")
                    continue

                # Compute self-corr vs OS cache
                try:
                    corr, source = await svc.calc_self_corr(
                        alpha.alpha_id, alpha.region or "USA"
                    )
                except Exception as e:
                    print(f"  {tag} {alpha.alpha_id} calc_self_corr EXC: {e}")
                    stats["errors"] += 1
                    continue

                if source == "empty" or source == "unknown":
                    stats["kept_unknown"] += 1
                    print(f"  {tag} {alpha.alpha_id} corr={corr:.3f} source={source} — defensive keep")
                    continue

                report = {
                    "pk": alpha.id,
                    "brain_id": alpha.alpha_id,
                    "is_sharpe": float(alpha.is_sharpe or 0),
                    "corr": float(corr),
                    "source": source,
                    "action": "kept_safe" if corr < threshold else "downgraded",
                }
                rows_report.append(report)

                if corr < threshold:
                    stats["kept_safe"] += 1
                    print(f"  {tag} {alpha.alpha_id} corr={corr:.3f} ✓ SAFE (sharpe={alpha.is_sharpe:.2f})")
                    continue

                # Downgrade
                stats["downgraded"] += 1
                if apply:
                    new_metrics = dict(alpha.metrics or {})
                    new_metrics["_v22_5_backfill"] = {
                        "at": datetime.now(timezone.utc).isoformat(),
                        "corr": float(corr),
                        "source": source,
                        "threshold": threshold,
                        "reason": "V-22.5 retroactive self-corr gate",
                        "prev_quality_status": alpha.quality_status,
                        "prev_can_submit": True,
                    }
                    new_status = "PASS_PROVISIONAL" if alpha.quality_status == "PASS" else alpha.quality_status
                    await db.execute(
                        update(Alpha)
                        .where(Alpha.id == alpha.id)
                        .values(
                            quality_status=new_status,
                            can_submit=False,
                            metrics=new_metrics,
                        )
                    )
                print(f"  {tag} {alpha.alpha_id} corr={corr:.3f} ✗ DOWNGRADE (sharpe={alpha.is_sharpe:.2f})")

            if apply:
                await db.commit()
                print("\n[apply] DB committed.\n")
            else:
                print("\n[dry-run] no changes. Re-run with --apply to commit.\n")

    # Summary
    print("=== Summary ===")
    for k, v in stats.items():
        print(f"  {k:18s} {v}")
    if stats["scanned"]:
        print(f"  downgrade_rate     {100 * stats['downgraded'] / stats['scanned']:.1f}%")

    # Write report
    if rows_report:
        out_dir = Path("docs/v22_5_backfill")
        out_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
        out = out_dir / f"backfill_{today}.md"
        lines = [
            f"# V-22.5 self-corr backfill — {today}",
            f"**Threshold**: {threshold}  **Mode**: {'APPLY' if apply else 'DRY-RUN'}",
            "",
            f"**Scanned**: {stats['scanned']}  **Downgraded**: {stats['downgraded']}  "
            f"**Kept safe**: {stats['kept_safe']}  **Kept unknown**: {stats['kept_unknown']}",
            "",
            "| # | brain_id | IS sharpe | corr | action |",
            "|---|---|---|---|---|",
        ]
        rows_report.sort(key=lambda r: -r["corr"])
        for i, r in enumerate(rows_report, 1):
            mark = "🔴 DOWNGRADE" if r["action"] == "downgraded" else "✅ KEEP"
            lines.append(
                f"| {i} | `{r['brain_id']}` | {r['is_sharpe']:.2f} | {r['corr']:.3f} | {mark} |"
            )
        out.write_text("\n".join(lines), encoding="utf-8")
        print(f"\nReport: {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--threshold", type=float, default=THRESHOLD)
    args = ap.parse_args()
    asyncio.run(main(args.apply, args.threshold))
