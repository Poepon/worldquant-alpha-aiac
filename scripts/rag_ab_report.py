"""RAG category-overlap A/B report — PASS-per-real-sim by arm (2026-05-21).

Measures whether P0's dataset-category-overlap step-1 retrieval improves mining,
by comparing the two arms the `ENABLE_RAG_CATEGORY_AB` harness assigns per round:
  - control  : layer1_pillar suppresses the dataset-category derivation (pre-P0)
  - category : the P0 dataset-category-overlap behavior

Metric — PASS-per-real-BRAIN-sim by arm:
  numerator   = alphas (task-generated) with quality_status IN (PASS, PASS_PROVISIONAL)
                AND metrics->>'_rag_ab_arm' = arm
  denominator = real BRAIN sims for that arm =
        [arm-stamped alphas that actually hit BRAIN (NOT _pre_brain_skip)]
      + [alpha_failures.rag_ab_arm = arm AND error_type NOT IN (PRESIM_SKIP, DEDUP_SKIP)]
  (failures dominate ~40:1, so they ARE most of the denominator.)

Secondary — cost-per-PASS by arm: llm_call_log.cost_usd attributed to an arm via
the DETERMINISTIC assignment (task_id + round_idx) % 2 (== node_rag_query's rule),
restricted to tasks that have arm-stamped rows (the A/B window).

Significance: two-proportion z-test + Wilson 95% CI on each arm's pass-rate, with
an explicit "insufficient sample" note (don't conclude on thin data).

Usage:
    python scripts/rag_ab_report.py
    python scripts/rag_ab_report.py --days 14
"""
from __future__ import annotations

import argparse
import asyncio
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from backend.database import AsyncSessionLocal

_ARMS = ("control", "category")
_MIN_DENOM = 100  # below this per arm → flag "insufficient sample"


def _wilson_ci(passes: int, n: int, z: float = 1.96):
    if n == 0:
        return (0.0, 0.0)
    p = passes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def _two_proportion_z(p1: int, n1: int, p2: int, n2: int):
    """Return (z, two-sided p) for H0: rate1 == rate2. p via normal approx."""
    if n1 == 0 or n2 == 0:
        return (0.0, 1.0)
    r1, r2 = p1 / n1, p2 / n2
    pool = (p1 + p2) / (n1 + n2)
    se = math.sqrt(pool * (1 - pool) * (1 / n1 + 1 / n2))
    if se == 0:
        return (0.0, 1.0)
    z = (r1 - r2) / se
    # two-sided p from standard normal CDF (erf)
    p = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))
    return (z, p)


async def _report(days: int) -> dict:
    out = {"days": days, "arms": {}}
    async with AsyncSessionLocal() as db:
        win = f"created_at >= now() - interval '{int(days)} days'"
        for arm in _ARMS:
            # numerator: PASS alphas for this arm
            num = (await db.execute(text(f"""
                SELECT count(*) FROM alphas
                WHERE task_id IS NOT NULL
                  AND quality_status IN ('PASS','PASS_PROVISIONAL')
                  AND metrics->>'_rag_ab_arm' = :arm
                  AND {win}
            """), {"arm": arm})).scalar() or 0

            # denominator part 1: arm-stamped alphas that hit BRAIN (exclude pre-brain skips)
            den_alphas = (await db.execute(text(f"""
                SELECT count(*) FROM alphas
                WHERE task_id IS NOT NULL
                  AND metrics->>'_rag_ab_arm' = :arm
                  AND COALESCE(metrics->>'_pre_brain_skip','') <> 'true'
                  AND {win}
            """), {"arm": arm})).scalar() or 0

            # denominator part 2: arm failures that actually consumed a BRAIN sim
            den_fail = (await db.execute(text(f"""
                SELECT count(*) FROM alpha_failures
                WHERE rag_ab_arm = :arm
                  AND COALESCE(error_type,'') NOT IN ('PRESIM_SKIP','DEDUP_SKIP')
                  AND {win}
            """), {"arm": arm})).scalar() or 0

            denom = den_alphas + den_fail

            # cost-per-arm intentionally omitted: the arm is assigned by true
            # per-round randomization (node_rag_query), so it can't be derived
            # for llm_call_log rows (they're not arm-stamped). PASS-per-real-sim
            # below is the headline metric. (To get cost-by-arm later, stamp
            # the arm onto llm_call_log too.)
            cost = 0.0

            lo, hi = _wilson_ci(num, denom)
            out["arms"][arm] = {
                "passes": int(num),
                "real_sims": int(denom),
                "  (alphas)": int(den_alphas),
                "  (failures)": int(den_fail),
                "pass_rate": round(num / denom, 4) if denom else None,
                "wilson95": (round(lo, 4), round(hi, 4)) if denom else None,
                "cost_per_pass_usd": "n/a (random arm; llm_call_log not arm-stamped)",
            }

    c, k = out["arms"]["control"], out["arms"]["category"]
    z, p = _two_proportion_z(k["passes"], k["real_sims"], c["passes"], c["real_sims"])
    out["z"] = round(z, 3)
    out["p_value"] = round(p, 4)
    out["insufficient_sample"] = (c["real_sims"] < _MIN_DENOM or k["real_sims"] < _MIN_DENOM)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="RAG category-overlap A/B report")
    ap.add_argument("--days", type=int, default=30, help="lookback window (default 30)")
    args = ap.parse_args()
    r = asyncio.run(_report(args.days))

    print(f"=== RAG category-overlap A/B report (last {r['days']}d) ===")
    for arm in _ARMS:
        a = r["arms"][arm]
        print(f"\n[{arm}]")
        for k, v in a.items():
            print(f"  {k}: {v}")
    print(f"\nz={r['z']}  p={r['p_value']}  (category vs control pass-rate)")
    if r["insufficient_sample"]:
        print(f"  ⚠️ INSUFFICIENT SAMPLE (need ≥{_MIN_DENOM} real_sims per arm) — "
              f"accumulate more A/B rounds before concluding.")
    elif r["p_value"] < 0.05:
        better = "category" if (r["arms"]["category"]["pass_rate"] or 0) > (r["arms"]["control"]["pass_rate"] or 0) else "control"
        print(f"  ✓ significant (p<0.05): '{better}' arm higher PASS-per-sim.")
    else:
        print(f"  = no significant difference (p≥0.05) — P0 category-overlap not "
              f"shown to move PASS-per-sim at current n.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
