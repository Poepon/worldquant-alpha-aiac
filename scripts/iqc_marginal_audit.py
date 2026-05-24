"""Batch IQC marginal-contribution audit.

Calls BRAIN before-and-after-performance for every can_submit=True alpha
in the DB (default scope: IQC2026S1 competition), records standalone-vs-merged
stats deltas, sorts by Δsharpe descending. Output: docs/iqc_audit/audit_<date>.md
+ JSON dump.

NOTE (2026-05-24): BRAIN removed the competition `score` from this endpoint, so
the audit ranks by Δsharpe (marginal sharpe contribution to the merged portfolio)
instead of the retired Δscore.

Usage:
  venv/Scripts/python.exe scripts/iqc_marginal_audit.py
  venv/Scripts/python.exe scripts/iqc_marginal_audit.py --competition IQC2026S1
  venv/Scripts/python.exe scripts/iqc_marginal_audit.py --limit 10  # test on 10
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, ".")

from backend.adapters.brain_adapter import BrainAdapter
from backend.database import AsyncSessionLocal
from backend.models import Alpha
from backend.services.alpha_service import AlphaService


async def main(competition: str | None, limit: int | None) -> None:
    print(f"=== IQC marginal-contribution audit (competition={competition}) ===\n")

    async with AsyncSessionLocal() as db:
        stmt = (
            select(Alpha)
            .where(Alpha.can_submit == True)  # noqa: E712
            .where(Alpha.date_submitted.is_(None))
            .order_by(Alpha.is_sharpe.desc())
        )
        if limit:
            stmt = stmt.limit(limit)
        alphas = (await db.execute(stmt)).scalars().all()
        print(f"Auditing {len(alphas)} can_submit + unsubmitted alphas\n")

        results = []
        failures = []

        async with BrainAdapter() as brain:
            svc = AlphaService(db)
            for i, alpha in enumerate(alphas):
                tag = f"[{i + 1}/{len(alphas)}]"
                try:
                    r = await svc.get_marginal_contribution(
                        alpha_pk=alpha.id,
                        competition=competition,
                        brain_adapter=brain,
                    )
                except Exception as e:
                    failures.append({"alpha_pk": alpha.id, "brain_id": alpha.alpha_id, "error": str(e)[:200]})
                    print(f"  {tag} {alpha.alpha_id} EXC {e}")
                    continue
                if r is None:
                    failures.append({"alpha_pk": alpha.id, "brain_id": alpha.alpha_id, "error": "None returned"})
                    print(f"  {tag} {alpha.alpha_id} returned None")
                    continue
                stats = r["raw"].get("stats") or {}
                before = stats.get("before") or {}
                after = stats.get("after") or {}
                entry = {
                    "alpha_pk": alpha.id,
                    "brain_id": alpha.alpha_id,
                    "expression": (alpha.expression or "")[:200],
                    "is_sharpe": float(alpha.is_sharpe or 0),
                    "is_fitness": float(alpha.is_fitness or 0),
                    "is_turnover": float(alpha.is_turnover or 0),
                    "merged_sharpe": after.get("sharpe"),
                    "merged_fitness": after.get("fitness"),
                    "partition_name": r.get("partition_name"),
                    "deltas": r["deltas"],
                }
                results.append(entry)
                dsh = r["deltas"].get("sharpe")
                dsh_str = f"{dsh:+}" if isinstance(dsh, (int, float)) else "—"
                _msh = after.get("sharpe")
                print(
                    f"  {tag} {alpha.alpha_id} IS_sh={alpha.is_sharpe:.2f} "
                    f"merged_sh={_msh if isinstance(_msh, (int, float)) else '—'} Δsharpe={dsh_str}"
                )

    # Sort by Δsharpe desc (None to bottom)
    results.sort(
        key=lambda r: (r["deltas"].get("sharpe") if isinstance(r["deltas"].get("sharpe"), (int, float)) else -99999),
        reverse=True,
    )

    # Output
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    docs_dir = Path("docs/iqc_audit")
    docs_dir.mkdir(parents=True, exist_ok=True)

    json_path = docs_dir / f"audit_{today}.json"
    json_path.write_text(json.dumps({"competition": competition, "results": results, "failures": failures}, indent=2), encoding="utf-8")

    md_path = docs_dir / f"audit_{today}.md"
    lines = [
        f"# IQC marginal-contribution audit — {today}",
        "",
        f"**Competition**: `{competition or 'users/self'}`",
        f"**Audited alphas (can_submit=True, unsubmitted)**: {len(alphas)}",
        f"**Successfully fetched**: {len(results)}",
        f"**Failures**: {len(failures)}",
        "",
        "Ranked by `Δsharpe` (descending — positive = improves the merged portfolio).",
        "",
        "| # | brain_id | IS sharpe | merged sharpe | Δsharpe | Δfitness | Δturnover | Δpnl |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(results, 1):
        d = r["deltas"]
        def fmt(v, prec=2, money=False):
            if not isinstance(v, (int, float)):
                return "—"
            if money:
                return f"{v:+,.0f}"
            return f"{v:+.{prec}f}"
        lines.append(
            f"| {i} | `{r['brain_id']}` | "
            f"{r['is_sharpe']:.2f} | "
            f"{r['merged_sharpe'] if isinstance(r['merged_sharpe'], (int, float)) else '—'} | "
            f"{fmt(d.get('sharpe'))} | {fmt(d.get('fitness'))} | "
            f"{fmt(d.get('turnover'), prec=4)} | "
            f"{fmt(d.get('pnl'), prec=0, money=True)} |"
        )
    lines.append("")
    if failures:
        lines.append("## Failures")
        lines.append("")
        for f in failures:
            lines.append(f"- `{f['brain_id']}` (alpha_pk={f['alpha_pk']}): {f['error']}")
        lines.append("")

    # Summary stats
    sharpe_deltas = [r["deltas"].get("sharpe") for r in results if isinstance(r["deltas"].get("sharpe"), (int, float))]
    if sharpe_deltas:
        lines.insert(7, "")
        lines.insert(7, f"**Net positive (Δsharpe > 0)**: {sum(1 for s in sharpe_deltas if s > 0)} / {len(sharpe_deltas)}")
        lines.insert(8, f"**Average Δsharpe**: {sum(sharpe_deltas)/len(sharpe_deltas):+.3f}")
        lines.insert(9, f"**Best Δsharpe**: {max(sharpe_deltas):+.3f}")
        lines.insert(10, f"**Worst Δsharpe**: {min(sharpe_deltas):+.3f}")
    md_path.write_text("\n".join(lines), encoding="utf-8")

    print()
    print(f"=== Done ===")
    print(f"  results: {len(results)}, failures: {len(failures)}")
    print(f"  json: {json_path}")
    print(f"  md:   {md_path}")
    if sharpe_deltas:
        pos = sum(1 for s in sharpe_deltas if s > 0)
        print(f"  Net positive Δsharpe: {pos}/{len(sharpe_deltas)}")
        print(f"  Best: {max(sharpe_deltas):+.3f}  Worst: {min(sharpe_deltas):+.3f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--competition", default="IQC2026S1")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    asyncio.run(main(args.competition, args.limit))
