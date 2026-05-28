"""Lightweight optimization sweep for alpha 15621 (sharpe 1.87 → target ≥2.0).

Baseline:
  expression  = group_neutralize(rank(ts_zscore(divide(cashflow_op, enterprise_value), 60)), industry)
  region      = USA / TOP3000 / delay=0 / decay=0 / neutralization=NONE / truncation=0.08
  sharpe=1.87 fitness=1.67 returns=11.35% turnover=14.19%  margin=16bps
  blocker     = BRAIN LOW_SHARPE limit=2.0

Sweep grid (≈12 sims, ~3 USER slots concurrent):
  (a) decay 0/4/8/16/32              — sharpe lever (smooth signal)
  (b) ts_zscore window 30/45/90/120  — timescale tuning
  (c) outer neutralization SUBINDUSTRY/INDUSTRY/SECTOR — extra orthogonalization
  (d) best-of(a) × best-of(b)        — combined point if both helped

Picks the variant with highest sharpe; flags any that crosses 2.0.
"""
from __future__ import annotations

import asyncio
import re
import sys
from dataclasses import dataclass
from typing import Optional

sys.path.insert(0, '.')
from backend.adapters.brain_adapter import BrainAdapter


BASELINE_EXPR = "group_neutralize(rank(ts_zscore(divide(cashflow_op, enterprise_value), 60)), industry)"
BASELINE = dict(region="USA", universe="TOP3000", delay=0, decay=0,
                neutralization="NONE", truncation=0.08, test_period="P2Y0M")
# delay-aware (2026-05-28): BRAIN's LOW_SHARPE gate is stricter on delay-0
# (2.0 vs delay-1's 1.5/1.25). Source the right value from settings so this
# script stays in sync with the central threshold helper.
from backend.config import settings as _settings
TARGET_SHARPE = _settings.eval_thresholds(BASELINE["delay"])["sharpe_min"]


@dataclass
class Variant:
    tag: str
    expr: str
    overrides: dict


def build_variants() -> list[Variant]:
    v: list[Variant] = []
    # (a) decay sweep
    for d in (4, 8, 16, 32):
        v.append(Variant(tag=f"decay={d}", expr=BASELINE_EXPR, overrides={"decay": d}))
    # (b) window sweep — swap the literal 60 inside ts_zscore(_, 60).
    # Use anchored string replace: the literal ", 60)" only appears at the end
    # of the ts_zscore call (the inner divide() comma is followed by a space
    # then `enterprise_value)` not `60)`), so this is safe.
    for w in (30, 45, 90, 120):
        new_expr = BASELINE_EXPR.replace(", 60)", f", {w})", 1)
        assert new_expr != BASELINE_EXPR, f"window swap {w} failed"
        v.append(Variant(tag=f"window={w}", expr=new_expr, overrides={}))
    # (c) outer neutralization sweep (baseline outer = NONE)
    for n in ("SUBINDUSTRY", "INDUSTRY", "SECTOR"):
        v.append(Variant(tag=f"neut={n}", expr=BASELINE_EXPR, overrides={"neutralization": n}))
    return v


def _extract_metrics(sim: dict) -> dict:
    """BrainAdapter.simulate_alpha returns a heterogeneous dict; extract the IS
    metrics we care about. Returns None values if not present."""
    if not isinstance(sim, dict):
        return {}
    is_m = sim.get("is") or sim.get("metrics") or {}
    if isinstance(is_m, dict):
        # may be nested e.g. is_m["metrics"] in some responses
        if "sharpe" not in is_m and "metrics" in is_m:
            is_m = is_m["metrics"]
    return {
        "sharpe": is_m.get("sharpe"),
        "fitness": is_m.get("fitness"),
        "returns": is_m.get("returns"),
        "turnover": is_m.get("turnover"),
        "margin": is_m.get("margin"),
        "checks": is_m.get("checks") or sim.get("checks"),
        "success": sim.get("success", is_m.get("sharpe") is not None),
        "error": sim.get("error"),
    }


async def run_one(brain: BrainAdapter, sem: asyncio.Semaphore, v: Variant) -> tuple[Variant, dict]:
    async with sem:
        params = {**BASELINE, **v.overrides}
        try:
            sim = await brain.simulate_alpha(expression=v.expr, **params)
        except Exception as e:  # noqa: BLE001
            return v, {"success": False, "error": f"{type(e).__name__}: {e}"}
        return v, _extract_metrics(sim)


async def main():
    variants = build_variants()
    print(f"[opt-15621] sweeping {len(variants)} variants (3 USER slots concurrent)")
    print(f"[opt-15621] baseline: sharpe=1.87 fit=1.67  target sharpe ≥ {TARGET_SHARPE}")
    print()
    async with BrainAdapter() as brain:
        sem = asyncio.Semaphore(3)
        results = await asyncio.gather(*(run_one(brain, sem, v) for v in variants))

    # report
    print(f"{'tag':25s}  {'sharpe':>7s}  {'fit':>5s}  {'turn':>5s}  {'margin':>6s}  notes")
    print("-" * 80)
    by_sharpe = []
    for v, m in results:
        if not m.get("success"):
            print(f"{v.tag:25s}  ERROR: {m.get('error','?')[:50]}")
            continue
        s = m.get("sharpe"); f = m.get("fitness"); t = m.get("turnover"); mg = m.get("margin")
        s_str = f"{s:.3f}" if s is not None else "  -  "
        f_str = f"{f:.2f}" if f is not None else " -  "
        t_str = f"{t:.3f}" if t is not None else "  -  "
        mg_str = f"{mg*10000:.1f}bps" if mg is not None else "  -  "
        flag = "  🎯 >= 2.0!" if (s is not None and s >= TARGET_SHARPE) else ""
        print(f"{v.tag:25s}  {s_str:>7s}  {f_str:>5s}  {t_str:>5s}  {mg_str:>6s}  {flag}")
        if s is not None:
            by_sharpe.append((s, v.tag, m))
    by_sharpe.sort(reverse=True)

    print()
    print(f"BASELINE                   1.870  1.67  0.142  16.0bps")
    print()
    if not by_sharpe:
        print("[opt-15621] no successful sims")
        return
    top_s, top_tag, top_m = by_sharpe[0]
    delta = top_s - 1.87
    print(f"[opt-15621] best variant: {top_tag} sharpe={top_s:.3f} (Δ {delta:+.3f} vs baseline)")
    if top_s >= TARGET_SHARPE:
        print(f"[opt-15621] 🎯 BRAIN gate cleared (sharpe ≥ {TARGET_SHARPE})")
    else:
        print(f"[opt-15621] short of BRAIN gate by {TARGET_SHARPE - top_s:.3f}")


if __name__ == "__main__":
    asyncio.run(main())
