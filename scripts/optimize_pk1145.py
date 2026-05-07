"""Optimize pk=1145: subtract(0, days_from_last_change(pcr_oi_720))

Base metrics (already SAFE for submit, self_corr=0.384):
  sh=1.34  fit=1.28  to=0.05  bcs=true

Try 8 variants spanning 3 axes:
  A. Field swaps (different window of pcr_oi or pcr_vol)
  B. Wrappers (rank / group_neutralize / decay / zscore)
  C. Combined (rank+decay)

Goals: lift sharpe / fitness while keeping low turnover and clean BRAIN
checks. Each variant reaffirms (or improves on) the original signal.

Concurrency=2 — leave 1 BRAIN slot for active mining (task 299 if still up).
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.adapters.brain_adapter import BrainAdapter

# Original pk=1145 settings (USA, TOP3000, delay=1, neutralization=NONE,
# truncation=0.08 — we'll override neut for group_* variants).
BASE_REGION = "USA"
BASE_UNIVERSE = "TOP3000"
BASE_DELAY = 1
BASE_TRUNCATION = 0.08

VARIANTS = [
    # Axis A: field swaps
    {
        "id": "A1_oi_360",
        "expr": "subtract(0, days_from_last_change(pcr_oi_360))",
        "neut": "NONE",
        "rationale": "shorter window (360d) for sentiment decay",
    },
    {
        "id": "A2_oi_180",
        "expr": "subtract(0, days_from_last_change(pcr_oi_180))",
        "neut": "NONE",
        "rationale": "much shorter window (180d)",
    },
    {
        "id": "A3_vol_720",
        "expr": "subtract(0, days_from_last_change(pcr_vol_720))",
        "neut": "NONE",
        "rationale": "volume PCR (vs open-interest), same 720d window",
    },
    # Axis B: wrappers on the original
    {
        "id": "B1_rank",
        "expr": "rank(subtract(0, days_from_last_change(pcr_oi_720)))",
        "neut": "NONE",
        "rationale": "cross-sectional rank — spread weight via xs ranking",
    },
    {
        "id": "B2_group_neut_industry",
        "expr": "subtract(0, days_from_last_change(pcr_oi_720))",
        "neut": "INDUSTRY",
        "rationale": "industry residualize via BRAIN neutralization setting",
    },
    {
        "id": "B3_group_neut_subindustry",
        "expr": "subtract(0, days_from_last_change(pcr_oi_720))",
        "neut": "SUBINDUSTRY",
        "rationale": "tighter group residualize",
    },
    {
        "id": "B4_decay4",
        "expr": "ts_decay_linear(subtract(0, days_from_last_change(pcr_oi_720)), 4)",
        "neut": "NONE",
        "rationale": "smooth transitions — should drop turnover further",
    },
    # Axis C: combined
    {
        "id": "C1_rank_decay4",
        "expr": "ts_decay_linear(rank(subtract(0, days_from_last_change(pcr_oi_720))), 4)",
        "neut": "NONE",
        "rationale": "rank + decay — both spread + smooth",
    },
]


def extract(sim: dict) -> dict:
    is_data = sim.get("is", {}) or {}
    checks = is_data.get("checks", []) or []
    failed = [c for c in checks if c.get("result") == "FAIL"]
    pending = [c for c in checks if c.get("result") == "PENDING"]
    can_submit = bool(checks) and len(failed) == 0
    return {
        "status": sim.get("status"),
        "sharpe": is_data.get("sharpe", sim.get("sharpe", 0)),
        "fitness": is_data.get("fitness", sim.get("fitness", 0)),
        "turnover": is_data.get("turnover", sim.get("turnover", 0)),
        "can_submit": can_submit,
        "failed": [c.get("name") for c in failed],
        "pending": [c.get("name") for c in pending],
    }


async def sim_one(adapter: BrainAdapter, v: dict, sem: asyncio.Semaphore) -> dict:
    async with sem:
        print(f"  → {v['id']}: {v['expr'][:90]} (neut={v['neut']})")
        r = await adapter.simulate_alpha(
            expression=v["expr"],
            region=BASE_REGION, universe=BASE_UNIVERSE, delay=BASE_DELAY,
            decay=0,  # decay-linear lives in expression for B4/C1
            neutralization=v["neut"], truncation=BASE_TRUNCATION,
        )
        if not r.get("success"):
            print(f"    ✗ FAIL: {(r.get('error') or '')[:100]}")
            return {**v, "success": False, "error": r.get("error")}
        m = extract(r)
        ok = "✅" if m["can_submit"] else "✗"
        print(f"    {ok} sh={m['sharpe']:.2f} fit={m['fitness']:.2f} to={m['turnover']:.2f} "
              f"failed={m['failed']} pending={m['pending']}")
        return {**v, "success": True, "alpha_id": r.get("alpha_id"), **m}


async def main():
    print(f"BASE pk=1145: subtract(0, days_from_last_change(pcr_oi_720))")
    print(f"  sh=1.34  fit=1.28  to=0.05  bcs=true  (already SAFE for submit, self_corr=0.384)\n")
    print(f"{len(VARIANTS)} variants × 1 = {len(VARIANTS)} simulates, concurrency=2\n")

    sem = asyncio.Semaphore(2)
    async with BrainAdapter() as adapter:
        await adapter.authenticate()
        results = await asyncio.gather(
            *[sim_one(adapter, v, sem) for v in VARIANTS],
            return_exceptions=True,
        )

    print()
    print("=" * 100)
    print("SUMMARY (sorted by sharpe desc, ✅=can_submit)")
    print("=" * 100)
    print(f"{'id':>26}  {'sh':>5} {'fit':>5} {'to':>5}  {'sub':>3}  failed/pending")
    print("-" * 100)
    print(f"{'BASE_pk1145':>26}  {1.34:>5.2f} {1.28:>5.2f} {0.05:>5.2f}  {'?':>3}  (no failed, pending=SELF_CORR)")
    sortable = [r for r in results if isinstance(r, dict) and r.get("success")]
    sortable.sort(key=lambda r: (-(r.get("sharpe") or 0), -(r.get("fitness") or 0)))
    for r in sortable:
        cs = "✅" if r["can_submit"] else "✗"
        f = ",".join(r.get("failed", []))
        p = ",".join(r.get("pending", []))
        print(f"{r['id']:>26}  {r['sharpe']:>5.2f} {r['fitness']:>5.2f} {r['turnover']:>5.2f}  {cs:>3}  fail={f or 'none'} pend={p or 'none'}")

    failed_runs = [r for r in results if isinstance(r, dict) and not r.get("success")]
    for r in failed_runs:
        print(f"  {r['id']}: SIM FAIL — {(r.get('error') or '')[:80]}")

    # Save raw
    out = Path(__file__).parent.parent / "docs" / f"optimize_pk1145_{datetime.now(timezone.utc).strftime('%Y-%m-%d_%H%M')}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "base_pk": 1145,
            "base_expr": "subtract(0, days_from_last_change(pcr_oi_720))",
            "base_metrics": {"sharpe": 1.34, "fitness": 1.28, "turnover": 0.05, "can_submit": True},
            "results": [r if isinstance(r, dict) else {"error": str(r)} for r in results],
        }, f, indent=2, default=str)
    print(f"\nsaved: {out}")

    # Best pick
    best = next((r for r in sortable if r.get("can_submit")), None)
    if best:
        improvement = ((best.get("sharpe", 0) - 1.34) / 1.34) * 100
        print(f"\n→ BEST submittable variant: {best['id']}")
        print(f"   {best['expr']}  (neut={best['neut']})")
        print(f"   sh={best['sharpe']:.2f} ({improvement:+.0f}% vs base) "
              f"fit={best['fitness']:.2f} to={best['turnover']:.2f}")


if __name__ == "__main__":
    asyncio.run(main())
