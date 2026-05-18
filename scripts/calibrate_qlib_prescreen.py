"""Phase 3 Q10 PR2c: calibrate Q10 prescreen floor from production shadow data.

Plan: ~/.claude/plans/phase3-q10-pyqlib-prescreen-2026-05-18.md v1.3 §4.5 + §13.2.

Replays last N days of (qlib_prescreen_log, alphas) JOIN to find the
optimal QLIB_PRESCREEN_SHARPE_FLOOR. Strategy:

  1. Pull pairs (local_sharpe, brain_passed) from production shadow data
     (qlib_prescreen_log left-joined to alphas via brain_expression).
  2. Sweep candidate floors in [0.1, 0.5] step 0.05.
  3. Per floor, build the 2x2 confusion matrix:
       brain_pass + local_sharpe >= floor  → TP (kept good alpha)
       brain_pass + local_sharpe <  floor  → FN (Q10 wrongly rejected!)
       brain_fail + local_sharpe >= floor  → FP (Q10 missed an obvious loser)
       brain_fail + local_sharpe <  floor  → TN (saved BRAIN cost)
     cost_saved% = TN / (TN+FN+FP+TP)
     fn_rate    = FN / (FN+TP)
  4. Print Pareto frontier table.
  5. Recommend the highest floor where fn_rate ≤ recommended_max_fn (default 0.15).
  6. Operator manually flips QLIB_PRESCREEN_SHARPE_FLOOR via /ops/feature-flags
     — script NEVER auto-flips (plan §13.3 floor-update protocol).

Insufficient data (< --min-samples pairs) → exit 0 with "insufficient data"
message + no recommendation (plan [V1.2-A2-12]).

Usage::

    python scripts/calibrate_qlib_prescreen.py --lookback-days 14 --min-samples 50

CI/test invocation::

    python scripts/calibrate_qlib_prescreen.py --pairs-json /tmp/pairs.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from typing import List, Optional, Tuple

logger = logging.getLogger("calibrate_qlib_prescreen")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)


@dataclass
class CalibrationRow:
    """One (local_sharpe, brain_passed) pair from shadow data."""
    local_sharpe: float
    brain_passed: bool


@dataclass
class FloorEval:
    """Outcome of evaluating a single floor candidate."""
    floor: float
    tp: int
    fn: int
    fp: int
    tn: int

    @property
    def total(self) -> int:
        return self.tp + self.fn + self.fp + self.tn

    @property
    def cost_saved_pct(self) -> float:
        return (self.tn / self.total * 100.0) if self.total else 0.0

    @property
    def fn_rate(self) -> float:
        denom = self.fn + self.tp
        return (self.fn / denom) if denom else 0.0


# ---------------------------------------------------------------------------
# DB query: build CalibrationRow list from production shadow data
# ---------------------------------------------------------------------------

async def fetch_pairs_from_db(*, lookback_days: int) -> List[CalibrationRow]:
    """JOIN qlib_prescreen_log + alphas on normalized brain_expression.

    NOTE on join key:
      qlib_prescreen_log.expression_hash uses sha256+raw (R1a audit
      convention). Alpha.expression_hash uses MD5+normalized via
      compute_expression_hash (different algorithm + input). The two
      hash columns are NOT directly comparable. We instead join on
      whitespace-normalized expression text — both sides come from the
      same alpha.expression at evaluation time, so byte-equality holds
      modulo trailing/leading whitespace and newline drift from LLM
      output. Normalizing via regexp_replace(\\s+ → ' ') + trim()
      catches the realistic drift cases.

    Soft-fail: returns [] on any DB / import error (operator can retry).
    """
    try:
        from sqlalchemy import text
        from backend.database import AsyncSessionLocal
    except Exception as ex:
        logger.warning(f"DB imports unavailable ({ex}); returning empty set")
        return []
    sql = text(
        r"""
        SELECT q.local_sharpe AS local_sharpe,
               (a.quality_status IN ('PASS', 'PROVISIONAL')) AS brain_passed
        FROM qlib_prescreen_log q
        JOIN alphas a
          ON trim(regexp_replace(a.expression,    '\s+', ' ', 'g'))
           = trim(regexp_replace(q.brain_expression, '\s+', ' ', 'g'))
        WHERE q.local_sharpe IS NOT NULL
          AND a.quality_status IS NOT NULL
          AND q.created_at > NOW() - (:days || ' days')::interval
        """
    )
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(sql, {"days": str(int(lookback_days))})).all()
    except Exception as ex:
        logger.error(f"DB query failed: {ex}")
        return []
    out: List[CalibrationRow] = []
    for r in rows:
        try:
            ls = float(r[0])
            bp = bool(r[1])
            out.append(CalibrationRow(local_sharpe=ls, brain_passed=bp))
        except Exception:
            continue
    return out


def load_pairs_from_json(path: str) -> List[CalibrationRow]:
    """For tests + manual replays — load [(local_sharpe, brain_passed), ...] JSON."""
    with open(path, "r", encoding="utf-8") as fp:
        raw = json.load(fp)
    out: List[CalibrationRow] = []
    for item in raw:
        try:
            out.append(CalibrationRow(
                local_sharpe=float(item["local_sharpe"]),
                brain_passed=bool(item["brain_passed"]),
            ))
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# Floor sweep + Pareto
# ---------------------------------------------------------------------------

def evaluate_floor(rows: List[CalibrationRow], floor: float) -> FloorEval:
    tp = fn = fp = tn = 0
    for r in rows:
        kept = r.local_sharpe >= floor
        if r.brain_passed and kept:
            tp += 1
        elif r.brain_passed and not kept:
            fn += 1
        elif (not r.brain_passed) and kept:
            fp += 1
        else:
            tn += 1
    return FloorEval(floor=floor, tp=tp, fn=fn, fp=fp, tn=tn)


def sweep_floors(
    rows: List[CalibrationRow], *,
    floor_min: float = 0.1, floor_max: float = 0.5, floor_step: float = 0.05,
) -> List[FloorEval]:
    out: List[FloorEval] = []
    # Avoid float drift — use multiplied ints in the loop
    steps = int(round((floor_max - floor_min) / floor_step)) + 1
    for i in range(steps):
        f = round(floor_min + i * floor_step, 4)
        out.append(evaluate_floor(rows, f))
    return out


def recommend_floor(
    sweep: List[FloorEval], *, max_fn_rate: float = 0.15,
) -> Optional[FloorEval]:
    """Pick the floor with the highest cost-saved among ones meeting fn_rate gate.

    Returns None if no candidate satisfies the gate (operator should defer
    or lower the gate manually).
    """
    candidates = [e for e in sweep if e.fn_rate <= max_fn_rate and e.total > 0]
    if not candidates:
        return None
    return max(candidates, key=lambda e: e.cost_saved_pct)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def format_report(sweep: List[FloorEval], recommended: Optional[FloorEval]) -> str:
    lines = []
    lines.append("=" * 70)
    lines.append("Q10 prescreen floor calibration — Pareto frontier")
    lines.append("=" * 70)
    lines.append(f"{'floor':>7} | {'TP':>5} {'FN':>5} {'FP':>5} {'TN':>5} | "
                 f"{'cost_saved%':>12} {'fn_rate':>9}")
    lines.append("-" * 70)
    for e in sweep:
        marker = " ← RECOMMEND" if recommended and e.floor == recommended.floor else ""
        lines.append(
            f"{e.floor:>7.3f} | {e.tp:>5d} {e.fn:>5d} {e.fp:>5d} {e.tn:>5d} | "
            f"{e.cost_saved_pct:>11.2f}% {e.fn_rate:>9.4f}{marker}"
        )
    lines.append("-" * 70)
    if recommended:
        lines.append(
            f"Recommended floor: {recommended.floor:.3f} "
            f"(saves {recommended.cost_saved_pct:.1f}% with fn_rate {recommended.fn_rate:.3f})"
        )
        lines.append(
            "Operator action: PATCH /ops/feature-flags ENABLE_QLIB_PRESCREEN_SHARPE_FLOOR "
            f"to {recommended.floor:.3f} only if drift > 0.05 vs current."
        )
    else:
        lines.append("No floor satisfies the fn_rate gate — defer or relax gate manually.")
    lines.append("=" * 70)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Calibrate Q10 prescreen floor")
    parser.add_argument("--lookback-days", type=int, default=14)
    parser.add_argument("--min-samples", type=int, default=50,
                        help="Below this row count, exit without recommendation")
    parser.add_argument("--max-fn-rate", type=float, default=0.15,
                        help="Recommendation gate: fn_rate must be <= this")
    parser.add_argument("--floor-min", type=float, default=0.1)
    parser.add_argument("--floor-max", type=float, default=0.5)
    parser.add_argument("--floor-step", type=float, default=0.05)
    parser.add_argument("--pairs-json", default=None,
                        help="Read pairs from JSON instead of DB (test/manual)")
    args = parser.parse_args(argv)

    if args.pairs_json:
        rows = load_pairs_from_json(args.pairs_json)
    else:
        rows = asyncio.run(fetch_pairs_from_db(lookback_days=args.lookback_days))

    if len(rows) < args.min_samples:
        logger.warning(
            f"insufficient data: {len(rows)} pairs < min_samples={args.min_samples} "
            "(no recommendation issued; rerun next week)"
        )
        return 0  # graceful exit per plan [V1.2-A2-12]

    sweep = sweep_floors(
        rows, floor_min=args.floor_min, floor_max=args.floor_max,
        floor_step=args.floor_step,
    )
    recommended = recommend_floor(sweep, max_fn_rate=args.max_fn_rate)
    print(format_report(sweep, recommended))
    return 0


if __name__ == "__main__":
    sys.exit(main())
