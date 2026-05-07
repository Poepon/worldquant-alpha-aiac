"""Manual verification: can ts_decay_linear save pk=6606?

pk=6606: multiply(-1, divide(subtract(close, open), open))
  sh=1.58 fit=0.85 to=0.81  → fail HIGH_TURNOVER + LOW_FITNESS

Hypothesis: ts_decay_linear(., d) lowers turnover ~30-50% per d=4-8 step,
may bump fitness via noise reduction. If turnover drops < 0.7 + sharpe
holds > 1.25 + fit ≥ 0.95 → first-ever can_submit=true.

4 decay variants. Concurrency=2 to leave 1 BRAIN slot for active mining.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.adapters.brain_adapter import BrainAdapter

BASE_EXPR = "multiply(-1, divide(subtract(close, open), open))"
SETTINGS = {
    "region": "USA", "universe": "TOP3000",
    "delay": 1, "neutralization": "NONE", "truncation": 0.08,
}
DECAYS = [2, 4, 8, 16]


def extract_metrics(sim_result: dict) -> dict:
    m = sim_result or {}
    is_data = m.get("is", {}) or {}
    checks = is_data.get("checks", []) or []
    failed = [c for c in checks if c.get("result") == "FAIL"]
    pending = [c for c in checks if c.get("result") == "PENDING"]
    can_submit = bool(checks) and len(failed) == 0
    return {
        "status": m.get("status"),
        "sharpe": is_data.get("sharpe", m.get("sharpe", 0)),
        "fitness": is_data.get("fitness", m.get("fitness", 0)),
        "turnover": is_data.get("turnover", m.get("turnover", 0)),
        "can_submit": can_submit,
        "failed_check_names": [c.get("name") for c in failed],
        "pending_check_names": [c.get("name") for c in pending],
    }


async def sim_one(adapter: BrainAdapter, decay: int, sem: asyncio.Semaphore):
    expr = f"ts_decay_linear({BASE_EXPR}, {decay})"
    async with sem:
        print(f"  → decay={decay}: {expr[:90]}")
        r = await adapter.simulate_alpha(
            expression=expr, decay=decay, **SETTINGS,
        )
        if not r.get("success"):
            print(f"    ✗ FAIL: {r.get('error', 'unknown')[:120]}")
            return {"decay": decay, "success": False, "error": r.get("error")}
        m = extract_metrics(r)
        ok = "✓" if m["can_submit"] else "✗"
        print(f"    {ok} sh={m['sharpe']:.2f} fit={m['fitness']:.2f} to={m['turnover']:.2f} "
              f"failed={m['failed_check_names']} pending={m['pending_check_names']}")
        return {"decay": decay, "expression": expr, "alpha_id": r.get("alpha_id"), **m}


async def main():
    print(f"BASE: {BASE_EXPR}  (pk=6606: sh=1.58 fit=0.85 to=0.81 — fail HIGH_TURNOVER+LOW_FITNESS)")
    print(f"Variants: 4 decay × 1 = 4 simulates, BRAIN concurrency limit=2 (leave slot for active mining)\n")

    sem = asyncio.Semaphore(2)
    async with BrainAdapter() as adapter:
        await adapter.authenticate()
        results = await asyncio.gather(
            *[sim_one(adapter, d, sem) for d in DECAYS],
            return_exceptions=True,
        )

    print("\n" + "=" * 70)
    print("SUMMARY (decay sweep on pk=6606 base)")
    print("=" * 70)
    print(f"{'decay':>6} {'sh':>6} {'fit':>6} {'to':>6} {'can_sub':>8}  failed")
    print("-" * 70)
    print(f"{'orig':>6} {1.58:>6.2f} {0.85:>6.2f} {0.81:>6.2f} {'False':>8}  HIGH_TURNOVER, LOW_FITNESS")
    promote_count = 0
    for r in results:
        if isinstance(r, Exception):
            print(f"  exception: {r}")
            continue
        if not r.get("success", True):
            print(f"  decay={r['decay']}: {r.get('error', '?')[:60]}")
            continue
        cs = "True" if r.get("can_submit") else "False"
        if r.get("can_submit"):
            promote_count += 1
        failed = ",".join(r.get("failed_check_names", []))
        print(f"{r['decay']:>6} {r.get('sharpe', 0):>6.2f} {r.get('fitness', 0):>6.2f} {r.get('turnover', 0):>6.2f} {cs:>8}  {failed}")
    print(f"\ncan_submit=true variants: {promote_count}/{len(DECAYS)}")

    out = Path(__file__).parent.parent / "docs" / "decay_verify_pk6606.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"base_expr": BASE_EXPR, "results": [r if not isinstance(r, Exception) else {"error": str(r)} for r in results]}, f, indent=2, default=str)
    print(f"\nsaved: {out}")


if __name__ == "__main__":
    asyncio.run(main())
