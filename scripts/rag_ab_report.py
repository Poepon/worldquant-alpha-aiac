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


# --- continuous-metric stats (Welch t, Cohen's d, required-n) --------------
# Why: PASS-per-sim is a ~1.5%-base binary → needs thousands of sims/arm to read
# out (months). A continuous quality signal (in-sample sharpe / composite score)
# has far more power — readable at n~50-100. These are summary-stat (no row pull)
# helpers; p is a normal approximation (df is typically >30 here, error small).
_Z_ALPHA_2 = 1.959964   # two-sided alpha=0.05
_Z_POWER_80 = 0.841621  # power=0.80


def _welch_from_summary(m1, v1, n1, m2, v2, n2):
    """Welch's t (unequal variance) from summary stats → (t, two-sided p, df).

    p via normal approx (consistent with _two_proportion_z; df reported so the
    reader can judge). v1/v2 are SAMPLE variances. Degenerate inputs → (0,1,0)."""
    if n1 < 2 or n2 < 2:
        return (0.0, 1.0, 0.0)
    se2 = v1 / n1 + v2 / n2
    if se2 <= 0:
        return (0.0, 1.0, 0.0)
    t = (m1 - m2) / math.sqrt(se2)
    # Welch–Satterthwaite df (for transparency; p uses normal approx)
    df = se2 * se2 / ((v1 / n1) ** 2 / (n1 - 1) + (v2 / n2) ** 2 / (n2 - 1))
    p = 2 * (1 - 0.5 * (1 + math.erf(abs(t) / math.sqrt(2))))
    return (t, p, df)


def _cohens_d(m1, v1, n1, m2, v2, n2):
    """Pooled-SD Cohen's d effect size from summary stats. 0 on degenerate."""
    if n1 < 2 or n2 < 2:
        return 0.0
    sp2 = ((n1 - 1) * v1 + (n2 - 1) * v2) / (n1 + n2 - 2)
    if sp2 <= 0:
        return 0.0
    return (m1 - m2) / math.sqrt(sp2)


def _required_n_per_arm(d, z_alpha2=_Z_ALPHA_2, z_power=_Z_POWER_80):
    """Approx per-arm n to detect effect size |d| at the given alpha/power
    (two-sample, normal approx). Tiny |d| → huge n (returns None if ~0)."""
    if not d or abs(d) < 1e-6:
        return None
    return int(math.ceil(2 * ((z_alpha2 + z_power) / abs(d)) ** 2))


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

        # --- continuous quality signal per arm (higher statistical power) ---
        # in-sample sharpe (typed column) is the primary signal; _score composite
        # secondary; plus higher-frequency sub-bars (sharpe>0.5/1.0, OPTIMIZE/PASS).
        # Same denominator as PASS (arm-stamped alphas that actually hit BRAIN).
        cont = {}
        for arm in _ARMS:
            row = (await db.execute(text(f"""
                SELECT count(*) n,
                       avg(is_sharpe) sharpe_mean,
                       var_samp(is_sharpe) sharpe_var,
                       avg((metrics->>'_score')::numeric)
                           FILTER (WHERE (metrics->>'_score') ~ '^-?[0-9.]+$') score_mean,
                       count(*) FILTER (WHERE is_sharpe > 0.5) n_s05,
                       count(*) FILTER (WHERE is_sharpe > 1.0) n_s10,
                       count(*) FILTER (WHERE quality_status IN ('OPTIMIZE','PASS','PASS_PROVISIONAL')) n_op
                FROM alphas
                WHERE task_id IS NOT NULL
                  AND metrics->>'_rag_ab_arm' = :arm
                  AND COALESCE(metrics->>'_pre_brain_skip','') <> 'true'
                  AND is_sharpe IS NOT NULL
                  AND {win}
            """), {"arm": arm})).first()
            n = int(row[0] or 0)
            cont[arm] = {
                "n": n,
                "sharpe_mean": round(float(row[1]), 4) if row[1] is not None else None,
                "sharpe_var": float(row[2]) if row[2] is not None else None,
                "score_mean": round(float(row[3]), 4) if row[3] is not None else None,
                "sharpe_gt_0.5_rate": round(int(row[4]) / n, 4) if n else None,
                "sharpe_gt_1.0_rate": round(int(row[5]) / n, 4) if n else None,
                "optimize_or_pass_rate": round(int(row[6]) / n, 4) if n else None,
            }
        out["continuous"] = cont

    c, k = out["arms"]["control"], out["arms"]["category"]
    z, p = _two_proportion_z(k["passes"], k["real_sims"], c["passes"], c["real_sims"])
    out["z"] = round(z, 3)
    out["p_value"] = round(p, 4)
    out["insufficient_sample"] = (c["real_sims"] < _MIN_DENOM or k["real_sims"] < _MIN_DENOM)

    # Continuous in-sample-sharpe contrast (Welch t + Cohen's d + required-n).
    cc, ck = out["continuous"]["control"], out["continuous"]["category"]
    if cc["sharpe_var"] is not None and ck["sharpe_var"] is not None and cc["n"] >= 2 and ck["n"] >= 2:
        t, pc, df = _welch_from_summary(ck["sharpe_mean"], ck["sharpe_var"], ck["n"],
                                        cc["sharpe_mean"], cc["sharpe_var"], cc["n"])
        d = _cohens_d(ck["sharpe_mean"], ck["sharpe_var"], ck["n"],
                      cc["sharpe_mean"], cc["sharpe_var"], cc["n"])
        out["sharpe_welch_t"] = round(t, 3)
        out["sharpe_p_value"] = round(pc, 4)
        out["sharpe_welch_df"] = round(df, 1)
        out["sharpe_cohens_d"] = round(d, 3)
        out["sharpe_required_n_per_arm"] = _required_n_per_arm(d)
    else:
        out["sharpe_welch_t"] = None
        out["sharpe_p_value"] = None
        out["sharpe_cohens_d"] = None
        out["sharpe_required_n_per_arm"] = None
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
    print(f"\nz={r['z']}  p={r['p_value']}  (category vs control PASS-per-sim)")
    if r["insufficient_sample"]:
        print(f"  ⚠️ PASS-per-sim INSUFFICIENT SAMPLE (need ≥{_MIN_DENOM} real_sims per arm; "
              f"~1.5% base rate ⇒ needs thousands — see continuous block below for a "
              f"higher-power read).")
    elif r["p_value"] < 0.05:
        better = "category" if (r["arms"]["category"]["pass_rate"] or 0) > (r["arms"]["control"]["pass_rate"] or 0) else "control"
        print(f"  ✓ significant (p<0.05): '{better}' arm higher PASS-per-sim.")
    else:
        print(f"  = no significant difference (p≥0.05) — P0 category-overlap not "
              f"shown to move PASS-per-sim at current n.")

    # --- continuous quality signal (higher power than rare PASS) ---
    print("\n=== continuous quality signal (in-sample sharpe; higher power) ===")
    for arm in _ARMS:
        cm = r["continuous"][arm]
        print(f"\n[{arm}]")
        for k, v in cm.items():
            print(f"  {k}: {v}")
    d = r.get("sharpe_cohens_d")
    pc = r.get("sharpe_p_value")
    req = r.get("sharpe_required_n_per_arm")
    if d is None:
        print("\n  ⚠️ not enough arm-stamped sims with is_sharpe to compare yet.")
    else:
        print(f"\nis_sharpe: Welch t={r['sharpe_welch_t']} p={pc} df={r.get('sharpe_welch_df')} "
              f"Cohen_d={d}")
        if pc < 0.05:
            better = "category" if (r['continuous']['category']['sharpe_mean'] or 0) > (r['continuous']['control']['sharpe_mean'] or 0) else "control"
            print(f"  ✓ significant (p<0.05): '{better}' arm higher in-sample sharpe "
                  f"(effect size d={d}).")
        else:
            msg = (f"  = no significant difference (p≥0.05). To detect the observed "
                   f"effect (d={d}) at 80% power you'd need ~{req} sims/arm")
            print(msg + "." if req else
                  "  = effectively zero effect (d≈0) — category-overlap does not move "
                  "in-sample sharpe; further RAG investment unlikely to pay off.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
