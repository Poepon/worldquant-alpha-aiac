"""G3 Phase A → Phase B: recommend AST_ORIGINALITY_MIN_DISTANCE (τ).

Reads Phase 1 R3/Q8 ``ast_distance_log`` (already populated) + the
``alphas`` table (PASS / FAIL outcomes from BRAIN sim), JOINs on
``expression_hash``, then sweeps candidate τ values and reports the
false-positive (FP) rate at each — i.e. fraction of alphas that would
have been G3-blocked despite ultimately passing.

Strategy
--------
1. Pull (ast_distance_min, brain_passed) pairs from production data:
   ``ast_distance_log.expression_hash`` JOIN ``alphas.expression_hash``
   where the alpha eventually reached a terminal status.
2. Sweep candidate τ values on the empirical 1st..30th percentile of
   ast_distance_min (most useful range — τ=0.5 would reject everything).
3. Per τ, compute:
     FP = brain_passed AND ast_distance_min < τ        (would-be wrongful reject)
     TP = NOT brain_passed AND ast_distance_min < τ    (correctly caught)
     fp_rate = FP / total_passes
     tp_rate = TP / total_fails
4. Recommend the highest τ where fp_rate ≤ --max-fp (default 0.05).
5. Operator manually flips AST_ORIGINALITY_MIN_DISTANCE via
   /ops/feature-flags — script NEVER auto-flips (per the Phase A spec).

Insufficient data (< --min-samples pairs) → exit 0 with "insufficient
data" message + no recommendation (matches calibrate_qlib_prescreen).

Usage::

    python scripts/calibrate_g3_threshold.py --lookback-days 14 --min-samples 100

CI / unit-test invocation (no DB)::

    python scripts/calibrate_g3_threshold.py --pairs-json /tmp/pairs.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from typing import List, Optional, Tuple

logger = logging.getLogger("calibrate_g3_threshold")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class Pair:
    """One observation: (ast_distance_min, brain_passed) at a given time."""
    distance: float
    passed: bool


@dataclass
class Row:
    """One row of the calibration sweep."""
    tau: float
    fp: int          # would-be wrongful reject
    tp: int          # correctly caught (failed-anyway)
    fp_rate: float   # FP / total_passes
    tp_rate: float   # TP / total_fails
    blocked: int     # FP + TP (gate output)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

async def load_pairs_from_db(lookback_days: int) -> List[Pair]:
    """JOIN ast_distance_log + alphas on raw expression text, return pairs.

    Schema fix (2026-05-19 G3 follow-up #2): the original G3 ship SQL
    `ON a.expression_hash = adl.expression_hash` never matched any row
    because the two tables use INCOMPATIBLE hash algorithms:
      * ``alphas.expression_hash``         = MD5 with operator-case +
        whitespace normalisation (32-char hex,
        compute_expression_hash in alpha_semantic_validator.py)
      * ``ast_distance_log.expression_hash`` = SHA256[:16] of raw expression
        (16-char hex, _hash_expr in ast_distance_logger.py)

    Even my first attempt (LEFT(a.hash, 16) = adl.hash) didn't help — the
    underlying byte-streams hashed differently. Fix: JOIN on the
    ``expression`` text columns themselves (ast_distance_log stores the
    full original expression; alphas does too). The join is O(N*M) but
    ast_distance_log is bounded ~10^4 rows long-term and alphas ~10^5 in
    a 30-day window — Postgres handles this in well under a second with
    the existing ix_alphas_expression_hash + ix_adl_expression_hash on
    the prefix indexes. Future cleanup: add a compatible_hash column to
    ast_distance_log (Alembic) and back-fill; defer that until G3 Phase B
    actually validates a τ.

    Soft-fails to empty list on DB error.
    """
    pairs: List[Pair] = []
    try:
        from sqlalchemy import text as _text
        from backend.database import AsyncSessionLocal
    except Exception as e:
        logger.error("DB import failed: %s", e)
        return pairs

    try:
        async with AsyncSessionLocal() as s:
            rows = (await s.execute(_text(
                "SELECT adl.ast_distance_min, a.quality_status "
                "FROM ast_distance_log adl "
                "JOIN alphas a ON a.expression = adl.expression "
                "WHERE adl.created_at > now() - (:days || ' day')::interval "
                "  AND adl.ast_distance_min IS NOT NULL "
                "  AND a.quality_status IN ('PASS', 'PASS_PROVISIONAL', 'FAIL', 'REJECT', 'OPTIMIZE')"
            ), {"days": str(int(lookback_days))})).all()
        for distance, qs in rows:
            if distance is None:
                continue
            passed = str(qs) in ("PASS", "PASS_PROVISIONAL", "OPTIMIZE")
            pairs.append(Pair(distance=float(distance), passed=bool(passed)))
    except Exception as e:
        logger.warning("DB query failed (returning empty pairs): %s", e)

    return pairs


def load_pairs_from_json(path: str) -> List[Pair]:
    """Load pre-extracted pairs from JSON (offline / CI mode)."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [Pair(distance=float(p["distance"]), passed=bool(p["passed"])) for p in data]


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------

def candidate_taus(pairs: List[Pair], n_steps: int = 30) -> List[float]:
    """Pick candidate τ values on the empirical 1st..30th percentile of
    distances. Above the 30th percentile τ becomes too aggressive."""
    if not pairs:
        return []
    distances = sorted(p.distance for p in pairs)
    n = len(distances)
    out: List[float] = []
    # Step 1% increments up to 30%
    for pct_int in range(1, 31):
        idx = max(0, min(n - 1, int(n * pct_int / 100)))
        tau = distances[idx]
        if not out or tau != out[-1]:
            out.append(tau)
    return out[:n_steps]


def sweep(pairs: List[Pair], taus: List[float]) -> List[Row]:
    """Compute FP/TP confusion matrix for each candidate τ."""
    total_passes = sum(1 for p in pairs if p.passed)
    total_fails = sum(1 for p in pairs if not p.passed)
    out: List[Row] = []
    for tau in taus:
        fp = sum(1 for p in pairs if p.passed and p.distance < tau)
        tp = sum(1 for p in pairs if (not p.passed) and p.distance < tau)
        out.append(Row(
            tau=tau,
            fp=fp,
            tp=tp,
            fp_rate=(fp / total_passes) if total_passes > 0 else 0.0,
            tp_rate=(tp / total_fails) if total_fails > 0 else 0.0,
            blocked=fp + tp,
        ))
    return out


def recommend(rows: List[Row], max_fp_rate: float) -> Optional[Row]:
    """Pick the highest τ where fp_rate ≤ max_fp_rate."""
    eligible = [r for r in rows if r.fp_rate <= max_fp_rate]
    if not eligible:
        return None
    # Highest τ wins — catches the most also-fails
    return max(eligible, key=lambda r: r.tau)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_table(rows: List[Row]) -> None:
    print(f"\n{'τ':>8} | {'fp':>5} | {'tp':>5} | {'fp_rate':>8} | {'tp_rate':>8} | {'blocked':>8}")
    print("-" * 64)
    for r in rows:
        print(
            f"{r.tau:>8.4f} | {r.fp:>5d} | {r.tp:>5d} | "
            f"{r.fp_rate:>8.4f} | {r.tp_rate:>8.4f} | {r.blocked:>8d}"
        )


async def _amain(args) -> int:
    if args.pairs_json:
        pairs = load_pairs_from_json(args.pairs_json)
    else:
        pairs = await load_pairs_from_db(args.lookback_days)

    if len(pairs) < args.min_samples:
        print(
            f"INSUFFICIENT DATA: have {len(pairs)} pairs, need >= {args.min_samples}. "
            "Phase A flag may have just been turned on, or ENABLE_AST_DIVERSITY_DIM "
            "was OFF — leave τ at default (0.15) and re-run after more accumulation."
        )
        return 0

    print(f"Loaded {len(pairs)} (distance, passed) pairs (lookback={args.lookback_days}d)")
    n_passes = sum(1 for p in pairs if p.passed)
    n_fails = sum(1 for p in pairs if not p.passed)
    print(f"  passes={n_passes}  fails={n_fails}")

    taus = candidate_taus(pairs)
    rows = sweep(pairs, taus)
    _print_table(rows)

    rec = recommend(rows, args.max_fp_rate)
    if rec is None:
        print(
            f"\nNo τ candidate keeps fp_rate <= {args.max_fp_rate}. "
            "Either widen --max-fp or accept that G3 cannot ship without "
            "false-positive cost in this data window."
        )
        return 0

    print(
        f"\nRECOMMENDED τ = {rec.tau:.4f} "
        f"(fp_rate={rec.fp_rate:.4f}, tp_rate={rec.tp_rate:.4f}, "
        f"blocks {rec.blocked}/{len(pairs)} candidates)"
    )
    print(
        "To apply: flip AST_ORIGINALITY_MIN_DISTANCE in settings (env or "
        "feature-flag override). Script NEVER auto-flips per G3 spec."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lookback-days", type=int, default=14)
    parser.add_argument("--min-samples", type=int, default=100)
    parser.add_argument("--max-fp-rate", type=float, default=0.05)
    parser.add_argument(
        "--pairs-json", type=str, default=None,
        help="Skip DB; load pairs from JSON file (CI / unit-test invocation)",
    )
    args = parser.parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
