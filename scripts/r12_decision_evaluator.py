"""R12 decision evaluator — operator runs at the 2026-07-04 ± 5d decision point.

Phase 4 Sprint 5 PR1 (decision-independent tooling, per plan v5 §7
Sprint 5 + the §300-303 R12 decision point). This script does NOT
execute any retire/restore — it produces the GO / NO-GO / PARTIAL
recommendation + per-sentinel counterfactual margins that the operator
acts on. The actual cleanup (B4.2 G3 retire / 6 sentinel deprecate, OR
sentinel restore) is a separate operator-gated step.

What it computes
----------------
1. **Main R12 decision** — LLM_MODE=assistant vs author PASS-rate diff
   with bootstrap effect-size CI (reuses
   ``backend.services.llm_mode_comparison``). GO/NO-GO/PARTIAL on the
   30d obs window per plan v5 §6.1 decision matrix.

2. **Per-sentinel counterfactual** — for each of the 6 R12 sentinel
   flags, compares PASS rate among alphas carrying the sentinel's
   metrics stamp vs the overall baseline PASS rate. A positive margin
   means the mechanism was helping (→ RESTORE recommendation); a
   zero/negative margin means it wasn't pulling weight (→ DEPRECATE
   recommendation). This drives the PARTIAL route's per-flag decision.

The 6 sentinel stamp keys (confirmed in evaluation.py):
  ENABLE_R1B_HYPOTHESIS_MUTATE     → metrics["_r1b_mutation_triggered"]
  ENABLE_G5_CROSSOVER              → metrics["_g5_crossover"]
  ENABLE_HYPOTHESIS_FOREST_REUSE   → metrics["_hypothesis_forest_reference"]
  ENABLE_R8_L0                     → metrics["_r8_l0_on"]
  ENABLE_AST_ORIGINALITY_GATE      → metrics["_g3_ast_originality_blocked"]
  ENABLE_SIMULATION_CACHE          → metrics["_simulation_cache_hit"]

Output
------
JSON to ``--out`` (or stdout) with:
  - main_decision: {decision, rationale, stats}
  - per_sentinel: [{flag, stamp_key, stamped_n, stamped_pass_rate,
                    baseline_pass_rate, margin_pct_pts, recommendation}]
  - sprint5_route: "GO" | "NO-GO" | "PARTIAL" (= main_decision mapped)
  - retire_candidates / restore_candidates (per-sentinel split)

Usage
-----
::

    python scripts/r12_decision_evaluator.py --days 30 \\
        --out docs/r12_decision_2026-07-04.json

Insufficient data (assistant pool empty) → main_decision=INSUFFICIENT,
sprint5_route=PARTIAL, and per-sentinel margins still reported.

Soft-fail: DB error → exits 1 with error JSON; never partial-writes.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("r12_decision_evaluator")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)


# Sentinel flag → metrics stamp key. Order = plan v5 §config sentinel list.
_SENTINELS: List[tuple] = [
    ("ENABLE_R1B_HYPOTHESIS_MUTATE", "_r1b_mutation_triggered"),
    ("ENABLE_G5_CROSSOVER", "_g5_crossover"),
    ("ENABLE_HYPOTHESIS_FOREST_REUSE", "_hypothesis_forest_reference"),
    ("ENABLE_R8_L0", "_r8_l0_on"),
    ("ENABLE_AST_ORIGINALITY_GATE", "_g3_ast_originality_blocked"),
    ("ENABLE_SIMULATION_CACHE", "_simulation_cache_hit"),
]

# Margin below which a sentinel is recommended for DEPRECATE rather than
# RESTORE (it wasn't pulling its weight). Operator-tunable via --margin-floor.
_DEFAULT_MARGIN_FLOOR_PCT_PTS = 0.0


@dataclass
class SentinelCounterfactual:
    flag: str
    stamp_key: str
    stamped_n: int
    stamped_pass: int
    stamped_pass_rate: float
    baseline_n: int
    baseline_pass: int
    baseline_pass_rate: float
    margin_pct_pts: float  # (stamped_rate - baseline_rate) × 100
    recommendation: str    # "RESTORE" | "DEPRECATE" | "INSUFFICIENT"


def _pass_set(status: Any) -> bool:
    s = getattr(status, "value", status)
    return s in ("PASS", "PASS_PROVISIONAL")


def compute_sentinel_counterfactuals(
    rows: List[tuple],
    *,
    margin_floor_pct_pts: float = _DEFAULT_MARGIN_FLOOR_PCT_PTS,
    min_stamped: int = 5,
) -> List[SentinelCounterfactual]:
    """Compute per-sentinel PASS-rate margin from (status, metrics) rows.

    Args:
        rows: list of (quality_status, metrics_dict) tuples.
        margin_floor_pct_pts: margin ≤ floor → DEPRECATE recommendation.
        min_stamped: < this many stamped rows → INSUFFICIENT.

    Returns one SentinelCounterfactual per sentinel flag.
    """
    total_n = len(rows)
    total_pass = sum(1 for status, _m in rows if _pass_set(status))
    baseline_rate = (total_pass / total_n) if total_n > 0 else 0.0

    out: List[SentinelCounterfactual] = []
    for flag, key in _SENTINELS:
        stamped_n = 0
        stamped_pass = 0
        for status, m in rows:
            if isinstance(m, dict) and m.get(key) is True:
                stamped_n += 1
                if _pass_set(status):
                    stamped_pass += 1
        stamped_rate = (stamped_pass / stamped_n) if stamped_n > 0 else 0.0
        margin = (stamped_rate - baseline_rate) * 100.0

        if stamped_n < min_stamped:
            rec = "INSUFFICIENT"
        elif margin > margin_floor_pct_pts:
            rec = "RESTORE"
        else:
            rec = "DEPRECATE"

        out.append(SentinelCounterfactual(
            flag=flag,
            stamp_key=key,
            stamped_n=stamped_n,
            stamped_pass=stamped_pass,
            stamped_pass_rate=round(stamped_rate, 4),
            baseline_n=total_n,
            baseline_pass=total_pass,
            baseline_pass_rate=round(baseline_rate, 4),
            margin_pct_pts=round(margin, 4),
            recommendation=rec,
        ))
    return out


def map_sprint5_route(main_decision: str) -> str:
    """Map the main R12 decision to a Sprint-5 route label (plan v5 §7)."""
    if main_decision == "GO":
        return "GO"  # B4.2 retire G3 + 6 sentinel permanent cleanup
    if main_decision == "NO-GO":
        return "NO-GO"  # cancel B4.2 + restore verification
    # INSUFFICIENT / PARTIAL / ERROR → PARTIAL route (per-flag margin)
    return "PARTIAL"


# ---------------------------------------------------------------------------
# DB orchestration
# ---------------------------------------------------------------------------

async def _fetch_alpha_rows(days: int, region: Optional[str]) -> List[tuple]:
    """Fetch (quality_status, metrics) for alphas in the window."""
    from sqlalchemy import select
    from backend.database import AsyncSessionLocal
    from backend.models import Alpha

    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(0, days))).replace(tzinfo=None)
    async with AsyncSessionLocal() as db:
        stmt = select(Alpha.quality_status, Alpha.metrics).where(
            Alpha.created_at >= cutoff
        )
        if region:
            stmt = stmt.where(Alpha.region == region)
        rows = (await db.execute(stmt)).all()
    return [(r[0], r[1]) for r in rows]


async def main_async(args) -> Dict[str, Any]:
    from backend.database import AsyncSessionLocal
    from backend.services.llm_mode_comparison import (
        query_mode_pool, evaluate_go_gate,
    )

    # 1. Main R12 decision (LLM mode PASS-rate diff)
    try:
        async with AsyncSessionLocal() as db:
            comparison = await query_mode_pool(db, days=args.days, region=args.region)
    except Exception as ex:  # noqa: BLE001
        return {"error": f"query_mode_pool failed: {str(ex)[:200]}"}

    go_gate = evaluate_go_gate(
        comparison,
        effect_floor_pct_pts=args.effect_floor,
        seed=args.seed,
    )

    # 2. Per-sentinel counterfactual
    try:
        rows = await _fetch_alpha_rows(args.days, args.region)
    except Exception as ex:  # noqa: BLE001
        return {"error": f"alpha row fetch failed: {str(ex)[:200]}"}

    sentinels = compute_sentinel_counterfactuals(
        rows,
        margin_floor_pct_pts=args.margin_floor,
        min_stamped=args.min_stamped,
    )

    route = map_sprint5_route(go_gate.get("decision", "PARTIAL"))
    retire = [s.flag for s in sentinels if s.recommendation == "DEPRECATE"]
    restore = [s.flag for s in sentinels if s.recommendation == "RESTORE"]

    return {
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": args.days,
        "region_filter": args.region,
        "main_decision": {
            "decision": go_gate.get("decision"),
            "rationale": go_gate.get("rationale"),
            "stats": go_gate.get("stats"),
            "thresholds": go_gate.get("thresholds"),
        },
        "comparison_by_mode": comparison.get("by_mode", {}),
        "per_sentinel": [asdict(s) for s in sentinels],
        "sprint5_route": route,
        "retire_candidates": retire,
        "restore_candidates": restore,
        "insufficient_sentinels": [
            s.flag for s in sentinels if s.recommendation == "INSUFFICIENT"
        ],
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--days", type=int, default=30, help="obs window (default 30)")
    p.add_argument("--region", default=None, help="filter to one region")
    p.add_argument("--effect-floor", type=float, default=-0.10,
                   help="R12 GO gate PASS-rate diff floor in pct-pts (default -0.10)")
    p.add_argument("--margin-floor", type=float, default=0.0,
                   help="per-sentinel margin ≤ floor → DEPRECATE (default 0.0)")
    p.add_argument("--min-stamped", type=int, default=5,
                   help="min stamped rows for a sentinel verdict (default 5)")
    p.add_argument("--seed", type=int, default=None, help="bootstrap RNG seed")
    p.add_argument("--out", default=None, help="JSON output path; stdout if omitted")
    args = p.parse_args()

    result = asyncio.run(main_async(args))

    if result.get("error"):
        logger.error("R12 evaluation failed: %s", result["error"])
        print(json.dumps(result, indent=2))
        return 1

    logger.info("=" * 64)
    logger.info("R12 DECISION — main: %s", result["main_decision"]["decision"])
    logger.info("  %s", result["main_decision"]["rationale"])
    logger.info("Sprint 5 route: %s", result["sprint5_route"])
    logger.info("-" * 64)
    for s in result["per_sentinel"]:
        logger.info(
            "  %-32s stamped=%-5d margin=%+.3fpp → %s",
            s["flag"], s["stamped_n"], s["margin_pct_pts"], s["recommendation"],
        )
    logger.info("-" * 64)
    logger.info("retire_candidates:  %s", result["retire_candidates"])
    logger.info("restore_candidates: %s", result["restore_candidates"])
    if result["insufficient_sentinels"]:
        logger.info("insufficient (more obs): %s", result["insufficient_sentinels"])

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        logger.info("Wrote JSON: %s", args.out)
    else:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
