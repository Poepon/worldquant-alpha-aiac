"""Phase C per-node LLM-routing A/B evaluator (read-only, 2026-05-30).

Source: docs/per_function_llm_routing_plan_2026-05-29.md §6 (A/B) + §7 (Phase C).

The A/B is **single-node single-variable** (plan P1#9): two FLAT tasks run the
SAME region/universe/datasets in the SAME window —
  - control   : no llm_overrides → the node uses its default model
  - treatment : task.config["llm_overrides"] = {<node>: {model, provider}} →
                resolve_model_for routes ONLY that node, everyone else default
                (honoured independent of the global ENABLE_PER_FUNCTION_LLM_ROUTING
                flag, so the rest of the fleet is untouched).

This script does NOT launch or mutate anything — it READS the two tasks' alphas +
llm_call_log and reports whether the routed model moved the needle. It mirrors
``scripts/rag_ab_report.py`` (PASS-per-real-sim + continuous sharpe) but keys on
``task_id`` instead of a per-round arm stamp, and adds the per-node cost($)/latency
delta from ``llm_call_log`` (the whole point of routing is quality-per-dollar).

Decision: reuses ``backend.services.llm_mode_comparison.bootstrap_diff_ci`` for the
PASS-rate effect CI → GO / NO-GO / PARTIAL, with a cost-per-PASS guardrail and an
A/B-validity check (did the override actually take on the treatment arm?).

Usage:
    python scripts/phase_c_llm_routing_ab.py \
        --control-task 123 --treatment-task 124 --node code_gen \
        --out docs/phase_c_code_gen_ab_2026-06-XX.json

Insufficient sample (either arm's real_sims < --min-denom) → decision falls back
to the higher-power continuous in-sample-sharpe signal (Welch t + Cohen's d), and
the JSON carries ``insufficient_sample=true`` so the operator doesn't over-read a
thin binary.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from backend.database import AsyncSessionLocal  # noqa: E402
from backend.services.llm_mode_comparison import bootstrap_diff_ci  # noqa: E402

_MIN_DENOM = 100  # below this real_sims per arm → binary is "insufficient sample"
_PASS_STATUSES = ("PASS", "PASS_PROVISIONAL")

# --- continuous-metric stats (Welch t, Cohen's d, required-n) ---------------
# Copied verbatim from rag_ab_report.py — a ~1.5%-base binary needs thousands of
# sims/arm to read out; in-sample sharpe has far more power (readable at n~50-100).
_Z_ALPHA_2 = 1.959964
_Z_POWER_80 = 0.841621


def _welch_from_summary(m1, v1, n1, m2, v2, n2):
    """Welch's t (unequal variance) → (t, two-sided p, df). p via normal approx."""
    if n1 < 2 or n2 < 2:
        return (0.0, 1.0, 0.0)
    se2 = v1 / n1 + v2 / n2
    if se2 <= 0:
        return (0.0, 1.0, 0.0)
    t = (m1 - m2) / math.sqrt(se2)
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
    """Approx per-arm n to detect |d| at alpha=0.05 / power=0.80. None if ~0."""
    if not d or abs(d) < 1e-6:
        return None
    return int(math.ceil(2 * ((z_alpha2 + z_power) / abs(d)) ** 2))


# ---------------------------------------------------------------------------
# Pure assembly + decision (no DB → unit-testable)
# ---------------------------------------------------------------------------
def assemble_arm(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Shape one arm's raw counts into the report block + derived rates.

    ``raw`` keys: passes, den_alphas, den_fail, sharpe_n, sharpe_mean,
    sharpe_var, node_calls, node_tokens, node_cost_usd, node_latency_ms,
    node_models (list), total_cost_usd, total_tokens.
    """
    passes = int(raw.get("passes", 0) or 0)
    real_sims = int(raw.get("den_alphas", 0) or 0) + int(raw.get("den_fail", 0) or 0)
    pass_rate = (passes / real_sims) if real_sims else None
    # Preserve None for cost: a brand-new model under A/B is the COMMON case
    # for an unpriced model (derive_cost_usd → NULL), and coercing None→0.0
    # would make an expensive routed model look free → cost guardrail defeated.
    # Keep None all the way to decide_ab so cost_flag stays "unknown".
    _raw_total = raw.get("total_cost_usd")
    total_cost = float(_raw_total) if _raw_total is not None else None
    _raw_node = raw.get("node_cost_usd")
    node_cost = float(_raw_node) if _raw_node is not None else None
    cost_per_pass = (total_cost / passes) if (passes and total_cost is not None) else None
    return {
        "passes": passes,
        "real_sims": real_sims,
        "  (alphas)": int(raw.get("den_alphas", 0) or 0),
        "  (failures)": int(raw.get("den_fail", 0) or 0),
        "pass_rate": round(pass_rate, 4) if pass_rate is not None else None,
        "sharpe_n": int(raw.get("sharpe_n", 0) or 0),
        "sharpe_mean": (round(float(raw["sharpe_mean"]), 4)
                        if raw.get("sharpe_mean") is not None else None),
        "sharpe_var": (float(raw["sharpe_var"])
                       if raw.get("sharpe_var") is not None else None),
        "node_models": list(raw.get("node_models") or []),
        "node_calls": int(raw.get("node_calls", 0) or 0),
        "node_tokens": int(raw.get("node_tokens", 0) or 0),
        "node_cost_usd": round(node_cost, 4) if node_cost is not None else None,
        "node_avg_latency_ms": (round(float(raw["node_latency_ms"]), 1)
                                if raw.get("node_latency_ms") is not None else None),
        "total_cost_usd": round(total_cost, 4) if total_cost is not None else None,
        "total_tokens": int(raw.get("total_tokens", 0) or 0),
        "cost_per_pass_usd": round(cost_per_pass, 4) if cost_per_pass is not None else None,
        # per-model call counts on the routed node — drives the routed_share
        # contamination check (treatment that fell back to default on some calls).
        "node_model_counts": dict(raw.get("node_model_counts") or {}),
    }


def decide_ab(
    control: Dict[str, Any],
    treatment: Dict[str, Any],
    *,
    node: str,
    effect_floor: float = -0.002,
    cost_tolerance: float = 0.20,
    routed_share_min: float = 0.90,
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    """Combine the binary PASS-rate gate + continuous sharpe + cost + validity.

    Primary gate = PASS-per-real-sim effect CI (treatment - control), via the
    shared bootstrap. Cost is a guardrail; sharpe is the higher-power fallback
    when the binary is sample-starved.

    ``effect_floor`` is a **RAW rate difference** (NOT ×100 percentage points):
    -0.002 means "treatment may be up to 0.2pp worse before NO-GO". Note the
    upstream ``bootstrap_diff_ci`` returns ``effect_pct_pts`` as a raw fraction
    despite the name — at a ~1.5% PASS base rate the shared module's -0.10
    default would mean -10pp and never fire, so we use a base-rate-appropriate
    raw floor here.
    ``routed_share_min``: the treatment node must have run the routed model on at
    least this fraction of its calls, else the arm is CONTAMINATED (a provider
    brown-out fell back to the default model on too many calls — see the runbook
    caveat on the global circuit breaker).
    """
    c_sims, t_sims = control["real_sims"], treatment["real_sims"]
    insufficient = (c_sims < _MIN_DENOM) or (t_sims < _MIN_DENOM)

    ci = bootstrap_diff_ci(
        c_sims, control["passes"], t_sims, treatment["passes"], seed=seed,
    )
    effect = ci["effect_pct_pts"]            # RAW rate diff (treat_rate - control_rate)
    lo, hi = ci["ci_lower"], ci["ci_upper"]

    # Binary gate — mirrors evaluate_go_gate EXACTLY (the reviewed shared gate):
    # GO requires the 80% CI lower bound strictly > 0 (significant improvement),
    # NOT merely lo > floor — else a CI straddling 0 would be declared GO on noise.
    if ci.get("insufficient_samples"):
        binary = "INSUFFICIENT"
    elif effect <= effect_floor or hi < effect_floor:
        binary = "NO-GO"
    elif effect > effect_floor and lo > 0:
        binary = "GO"
    else:
        binary = "PARTIAL"

    # Continuous sharpe (higher power) — fallback / corroboration.
    cc, tt = control, treatment
    sharpe = {"welch_t": None, "p_value": None, "cohens_d": None, "required_n_per_arm": None}
    if (cc.get("sharpe_var") is not None and tt.get("sharpe_var") is not None
            and cc["sharpe_n"] >= 2 and tt["sharpe_n"] >= 2):
        t, p, df = _welch_from_summary(tt["sharpe_mean"], tt["sharpe_var"], tt["sharpe_n"],
                                       cc["sharpe_mean"], cc["sharpe_var"], cc["sharpe_n"])
        d = _cohens_d(tt["sharpe_mean"], tt["sharpe_var"], tt["sharpe_n"],
                      cc["sharpe_mean"], cc["sharpe_var"], cc["sharpe_n"])
        sharpe = {"welch_t": round(t, 3), "p_value": round(p, 4), "welch_df": round(df, 1),
                  "cohens_d": round(d, 3), "required_n_per_arm": _required_n_per_arm(d)}

    # Cost guardrail — cost-per-PASS delta (the routing payoff is quality/$).
    # Only meaningful when BOTH arms have a real (priced) cost AND the binary is
    # not sample-starved (cost/PASS on a noisy PASS count is itself noise, and an
    # unpriced routed model collapses cost→None → never falsely "OK").
    cpp_c, cpp_t = control["cost_per_pass_usd"], treatment["cost_per_pass_usd"]
    cost_flag = "unknown"
    cost_delta_pct = None
    if cpp_c is not None and cpp_t is not None and cpp_c > 0 and not insufficient:
        cost_delta_pct = round((cpp_t - cpp_c) / cpp_c * 100.0, 1)
        cost_flag = "WORSE" if cost_delta_pct > cost_tolerance * 100 else "OK"

    # A/B validity — did the override actually take on the treatment arm?
    #   NO_NODE_CALLS     : the routed node never ran (nothing to compare)
    #   INVALID_SAME_MODEL: treatment ran ONLY control's model(s) → override dropped
    #   CONTAMINATED      : override took, but a provider brown-out fell back to the
    #                       default model on >(1-routed_share_min) of treatment's
    #                       calls (set equality alone misses this — t={routed,default}
    #                       ≠ c={default} reads as OK while half the calls were default)
    #   OK                : routed model dominates treatment's node calls
    c_models = set(m for m in control["node_models"] if m)
    t_models = set(m for m in treatment["node_models"] if m)
    t_counts = treatment.get("node_model_counts") or {}
    t_total = sum(t_counts.values())
    routed_models = t_models - c_models          # models only treatment ran = the override
    routed_calls = sum(n for m, n in t_counts.items() if m in routed_models)
    routed_share = (routed_calls / t_total) if t_total else 0.0
    if not t_models:
        validity = "NO_NODE_CALLS"
    elif not routed_models:
        validity = "INVALID_SAME_MODEL"
    elif t_total and routed_share < routed_share_min:
        validity = "CONTAMINATED"
    else:
        validity = "OK"

    # Headline decision: prefer the binary unless sample-starved, then defer to
    # sharpe; never declare GO if the A/B is invalid or cost is materially worse
    # without a clear quality win.
    if validity != "OK":
        decision = "INVALID"
    elif not insufficient and binary in ("GO", "NO-GO"):
        decision = binary
        if decision == "GO" and cost_flag == "WORSE":
            decision = "PARTIAL"  # quality up but pays a lot more $/PASS — operator call
    elif sharpe["cohens_d"] is not None and sharpe["p_value"] is not None and sharpe["p_value"] < 0.05:
        # Sample-starved binary → defer to the higher-power in-sample sharpe. But
        # in-sample sharpe of generated alphas is a weak, survivorship-laden proxy
        # (project lesson: ground-truth 不可信), and the fallback only fires at
        # small n. So a significant NEGATIVE effect is trusted as protective NO-GO,
        # but a positive one is capped at PARTIAL — never a hard GO off thin sharpe.
        decision = "PARTIAL" if sharpe["cohens_d"] > 0 else "NO-GO"
    else:
        decision = "PARTIAL"

    return {
        "node": node,
        "decision": decision,
        "binary_gate": binary,
        "insufficient_sample": insufficient,
        "effect_pct_pts": effect,
        "ci": ci,
        "sharpe": sharpe,
        "cost": {"control_per_pass": cpp_c, "treatment_per_pass": cpp_t,
                 "delta_pct": cost_delta_pct, "flag": cost_flag},
        "validity": validity,
        "routed_share": round(routed_share, 3),
        "thresholds": {"effect_floor": effect_floor,
                       "cost_tolerance_pct": cost_tolerance * 100,
                       "routed_share_min": routed_share_min,
                       "min_denom": _MIN_DENOM},
    }


# ---------------------------------------------------------------------------
# DB layer (thin)
# ---------------------------------------------------------------------------
async def _query_arm(db, task_id: int, node: str, win: str) -> Dict[str, Any]:
    passes = (await db.execute(text(f"""
        SELECT count(*) FROM alphas
        WHERE task_id = :tid
          AND quality_status IN ('PASS','PASS_PROVISIONAL') {win}
    """), {"tid": task_id})).scalar() or 0

    den_alphas = (await db.execute(text(f"""
        SELECT count(*) FROM alphas
        WHERE task_id = :tid
          AND COALESCE(metrics->>'_pre_brain_skip','') <> 'true' {win}
    """), {"tid": task_id})).scalar() or 0

    den_fail = (await db.execute(text(f"""
        SELECT count(*) FROM alpha_failures
        WHERE task_id = :tid
          AND COALESCE(error_type,'') NOT IN ('PRESIM_SKIP','DEDUP_SKIP') {win}
    """), {"tid": task_id})).scalar() or 0

    srow = (await db.execute(text(f"""
        SELECT count(*) n, avg(is_sharpe) m, var_samp(is_sharpe) v
        FROM alphas
        WHERE task_id = :tid
          AND COALESCE(metrics->>'_pre_brain_skip','') <> 'true'
          AND is_sharpe IS NOT NULL {win}
    """), {"tid": task_id})).first()

    nrow = (await db.execute(text(f"""
        SELECT count(*) calls, COALESCE(sum(tokens_total),0) toks,
               sum(cost_usd) cost, avg(latency_ms) lat
        FROM llm_call_log
        WHERE task_id = :tid AND node_key = :node {win}
    """), {"tid": task_id, "node": node})).first()

    # per-model call counts on the routed node — distinct models (validity:
    # did override take?) + counts (validity: routed_share contamination check).
    mrows = (await db.execute(text(f"""
        SELECT model, count(*) n FROM llm_call_log
        WHERE task_id = :tid AND node_key = :node {win}
        GROUP BY model
    """), {"tid": task_id, "node": node})).all()

    trow = (await db.execute(text(f"""
        SELECT sum(cost_usd) cost, COALESCE(sum(tokens_total),0) toks
        FROM llm_call_log WHERE task_id = :tid {win}
    """), {"tid": task_id})).first()

    return {
        "task_id": task_id,
        "passes": passes,
        "den_alphas": den_alphas,
        "den_fail": den_fail,
        "sharpe_n": (srow[0] if srow else 0),
        "sharpe_mean": (srow[1] if srow else None),
        "sharpe_var": (srow[2] if srow else None),
        "node_calls": (nrow[0] if nrow else 0),
        "node_tokens": (nrow[1] if nrow else 0),
        "node_cost_usd": (nrow[2] if nrow else None),
        "node_latency_ms": (nrow[3] if nrow else None),
        "node_models": [r[0] for r in mrows],
        "node_model_counts": {r[0]: int(r[1]) for r in mrows},
        "total_cost_usd": (trow[0] if trow else None),
        "total_tokens": (trow[1] if trow else 0),
    }


async def _report(control_task: int, treatment_task: int, node: str, *,
                  days: int, effect_floor: float, cost_tol: float,
                  seed: Optional[int]) -> Dict[str, Any]:
    # Optional created_at window (default: full task lifetimes). Bound both
    # alphas/alpha_failures/llm_call_log on created_at when --days > 0.
    win = f"AND created_at >= now() - interval '{int(days)} days'" if days and days > 0 else ""
    async with AsyncSessionLocal() as db:
        c_raw = await _query_arm(db, control_task, node, win)
        t_raw = await _query_arm(db, treatment_task, node, win)
    control = assemble_arm(c_raw)
    treatment = assemble_arm(t_raw)
    verdict = decide_ab(control, treatment, node=node,
                        effect_floor=effect_floor, cost_tolerance=cost_tol, seed=seed)
    return {
        "node": node,
        "control_task": control_task,
        "treatment_task": treatment_task,
        "window_days": days or "full",
        "control": control,
        "treatment": treatment,
        "verdict": verdict,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase C per-node LLM-routing A/B evaluator")
    ap.add_argument("--control-task", type=int, required=True, help="control FLAT task_id (no override)")
    ap.add_argument("--treatment-task", type=int, required=True, help="treatment FLAT task_id (routed node)")
    ap.add_argument("--node", type=str, default="code_gen", help="routed node_key (default code_gen)")
    ap.add_argument("--days", type=int, default=0, help="created_at lookback (0 = full task lifetime)")
    ap.add_argument("--effect-floor", type=float, default=-0.002,
                    help="PASS-rate effect floor as a RAW rate diff (default -0.002 = "
                         "treatment may be up to 0.2pp worse before NO-GO; NOT ×100)")
    ap.add_argument("--cost-tolerance", type=float, default=0.20,
                    help="cost-per-PASS increase tolerated before flagging WORSE (default 0.20 = +20%%)")
    ap.add_argument("--seed", type=int, default=42, help="bootstrap RNG seed (reproducible CI)")
    ap.add_argument("--out", type=str, default=None, help="write JSON report to this path")
    args = ap.parse_args()

    r = asyncio.run(_report(
        args.control_task, args.treatment_task, args.node,
        days=args.days, effect_floor=args.effect_floor,
        cost_tol=args.cost_tolerance, seed=args.seed,
    ))

    v = r["verdict"]
    print(f"=== Phase C A/B — node={r['node']} | control={r['control_task']} vs "
          f"treatment={r['treatment_task']} (window={r['window_days']}) ===")
    for label in ("control", "treatment"):
        print(f"\n[{label}]")
        for k, val in r[label].items():
            print(f"  {k}: {val}")
    print(f"\n--- verdict: {v['decision']} ---")
    # effect/CI are RAW rate diffs (fractions); ×100 for the pp display only.
    print(f"  binary_gate={v['binary_gate']} effect={round(v['effect_pct_pts'] * 100, 3)}pp "
          f"CI=({round(v['ci']['ci_lower'] * 100, 3)}, {round(v['ci']['ci_upper'] * 100, 3)})pp "
          f"insufficient_sample={v['insufficient_sample']}")
    print(f"  sharpe: Welch_t={v['sharpe']['welch_t']} p={v['sharpe']['p_value']} "
          f"d={v['sharpe']['cohens_d']} required_n/arm={v['sharpe']['required_n_per_arm']}")
    print(f"  cost: ctrl/pass={v['cost']['control_per_pass']} treat/pass={v['cost']['treatment_per_pass']} "
          f"delta={v['cost']['delta_pct']}% flag={v['cost']['flag']}")
    print(f"  validity: {v['validity']} (routed_share={v['routed_share']})  "
          f"(control node models={r['control']['node_models']} / treatment={r['treatment']['node_models']})")
    if v["validity"] == "CONTAMINATED":
        print(f"  ⚠️ A/B CONTAMINATED — treatment ran the routed model on only "
              f"{v['routed_share']:.0%} of {r['node']} calls (rest fell back to default, "
              "likely a provider brown-out via the global circuit). Re-run after the "
              "provider stabilizes, or route to a steadier endpoint.")
    elif v["validity"] != "OK":
        print("  ⚠️ A/B INVALID — treatment override did not take (or node never ran). "
              "Re-launch the treatment arm with task.config['llm_overrides'].")
    elif v["insufficient_sample"] and v["binary_gate"] == "INSUFFICIENT":
        print(f"  ⚠️ binary sample-starved (need ≥{_MIN_DENOM} real_sims/arm) — "
              "decision deferred to the higher-power in-sample-sharpe signal above.")

    if args.out:
        outp = Path(args.out)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(json.dumps(r, indent=2, default=str), encoding="utf-8")
        print(f"\nwrote {outp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
