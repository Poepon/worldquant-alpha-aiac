"""P0 experiment — does BRAIN GET /alphas/{id} return fresh rolling IS metrics?

The plan §"指标时变性处理" assumes BRAIN returns IS metrics computed over a
rolling testPeriod=P2Y window, so each call gives "fresh" sharpe / fitness /
turnover that drift over time. The whole "tier_seed_load refreshes metrics"
design depends on this. If BRAIN actually returns the snapshot from the
original simulation date, the refresh is a no-op and the design needs to be
reworked to focus only on OS-active alphas (whose metrics genuinely accumulate
day by day).

This script picks 5-10 historical alphas with simulation dates spread across
7 / 14 / 30+ days ago, calls BrainAdapter.get_alpha for each, and compares
the returned is.sharpe / is.fitness against what's stored in the alphas
table. Output: per-alpha drift in absolute and relative terms, plus a
verdict ("rolling" if median |Δsharpe| > 0.05, "frozen" otherwise).

Usage:
    python -m scripts.probe_brain_metrics_freshness
    python -m scripts.probe_brain_metrics_freshness --max 5
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta
from typing import List

from loguru import logger
from sqlalchemy import select

from backend.adapters.brain_adapter import BrainAdapter
from backend.database import AsyncSessionLocal
from backend.models import Alpha


async def _pick_samples(max_n: int) -> List[Alpha]:
    """Pick alphas with diverse sim age. Aims for representation across
    7d / 14d / 30d / 60d+ buckets."""
    async with AsyncSessionLocal() as db:
        now = datetime.utcnow()
        targets = []
        for days in (7, 14, 30, 60, 90):
            cutoff_lo = now - timedelta(days=days + 5)
            cutoff_hi = now - timedelta(days=max(1, days - 5))
            q = (
                select(Alpha)
                .where(Alpha.alpha_id.isnot(None))
                .where(Alpha.is_sharpe.isnot(None))
                .where(Alpha.date_created.between(cutoff_lo, cutoff_hi))
                .order_by(Alpha.is_sharpe.desc())
                .limit(2)
            )
            rows = (await db.execute(q)).scalars().all()
            for r in rows:
                if r not in targets:
                    targets.append(r)
                if len(targets) >= max_n:
                    return targets
        return targets


async def main(max_n: int) -> None:
    samples = await _pick_samples(max_n)
    if not samples:
        print("No samples found. Have any alphas been simulated yet?")
        return

    print(f"Probing {len(samples)} alphas...\n")
    drifts: List[float] = []
    print(f"{'alpha_id':<22} {'age_days':>8} {'cached_S':>10} {'fresh_S':>10} "
          f"{'ΔS':>8} {'cached_F':>10} {'fresh_F':>10} {'ΔF':>8}")
    print("-" * 100)

    async with BrainAdapter() as adapter:
        for alpha in samples:
            try:
                fresh = await adapter.get_alpha(alpha.alpha_id)
            except Exception as e:
                print(f"  {alpha.alpha_id} fetch FAILED: {e}")
                continue
            if not fresh:
                print(f"  {alpha.alpha_id} returned empty")
                continue

            is_block = fresh.get("is") or {}
            fresh_s = is_block.get("sharpe")
            fresh_f = is_block.get("fitness")
            cached_s = alpha.is_sharpe
            cached_f = alpha.is_fitness
            age = (datetime.utcnow() - alpha.date_created).days if alpha.date_created else "?"

            delta_s = (fresh_s - cached_s) if (fresh_s is not None and cached_s is not None) else None
            delta_f = (fresh_f - cached_f) if (fresh_f is not None and cached_f is not None) else None
            if delta_s is not None:
                drifts.append(abs(delta_s))

            def _fmt(v):
                return f"{v:.3f}" if isinstance(v, (int, float)) else "—"

            print(
                f"  {alpha.alpha_id:<20} {age!s:>8} {_fmt(cached_s):>10} {_fmt(fresh_s):>10} "
                f"{_fmt(delta_s):>8} {_fmt(cached_f):>10} {_fmt(fresh_f):>10} {_fmt(delta_f):>8}"
            )

    print()
    if drifts:
        median = sorted(drifts)[len(drifts) // 2]
        max_drift = max(drifts)
        verdict = "rolling (refresh meaningful)" if median > 0.05 else "frozen (refresh is a no-op)"
        print(f"Verdict: {verdict}")
        print(f"  median |Δsharpe| = {median:.4f}")
        print(f"  max |Δsharpe|    = {max_drift:.4f}")
        if median <= 0.05:
            print()
            print("⚠ If BRAIN returns frozen snapshots, the tier_seed_load metric refresh")
            print("  doesn't help — consider scoping refresh to OS-active alphas only,")
            print("  or remove the refresh step entirely from node_tier_seed_load.")
    else:
        print("No drift samples collected (BRAIN may be unreachable).")


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max", type=int, default=10, help="Max alphas to probe")
    args = parser.parse_args()
    try:
        asyncio.run(main(args.max))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
