"""R10 / R10-v2 family-ban pairwise-correlation calibration.

Phase 4 Sprint 2 PR3 (per plan v5 §6.7 — F2 fix: alphas table has NO
`daily_pnl` column → can't do pure-SQL `corr(a1.daily_pnl, a2.daily_pnl)`.
Pull PnL series Python-side via existing
``CorrelationService._fetch_pnl_series`` and compute the corr matrix
in numpy / pandas).

Strategy
--------
1. Pull Top-N PASS alpha by sharpe (region × pillar) from ``alphas``,
   filter to ``alpha_id IS NOT NULL`` (only BRAIN-submitted have a
   PnL endpoint) and ``family_signature IS NOT NULL``.
2. Batch-fetch daily PnL via existing CorrelationService — same retry +
   empty-fallback as production self-corr path.
3. Wide DataFrame (date × alpha) → pairwise corr matrix (pandas .corr).
4. For each family (same family_signature) split intra-family pairs
   vs cross-family pairs, report:
     - intra-family p50 / p95 / p99 correlation
     - cross-family p50 / p95 / p99 correlation
     - per-family pair count
5. Recommend ``FAMILY_BAN_MIN_PAIRWISE_CORR`` τ:
     - intra-family p95 lower bound (catch tight clusters)
     - intra-family p99 ceiling (do not over-ban)
     - chosen τ = median(intra_p95, intra_p99) per plan v5 §6.7
6. Output JSON to ``--out`` for operator inspection — script NEVER
   auto-flips settings (mirrors calibrate_g3_threshold).

Insufficient data (< --min-pairs) → exit 0 with "insufficient" message.

Usage
-----
::

    python scripts/calibrate_r10_pairwise_corr.py \\
        --region USA --top-n 100 --min-pairs 30 \\
        --out docs/r10_calib_output_2026-06-05.json

Background-friendly: 100 alpha × pairwise ≈ 5,000 fetches BUT each PnL
series is already cached by ``refresh_os_alpha_cache``; cache-hit path
takes ~10 min for cold start, <2 min when warm. The pairwise corr
computation itself is in-memory ``DataFrame.corr()`` (~1s for 100×100).

Per plan v5 §6.7: ~40 min BRAIN call budget for cold cache.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple

import pandas as pd
import numpy as np

logger = logging.getLogger("calibrate_r10_pairwise_corr")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class FamilyStats:
    family_signature: str
    n_alphas: int
    n_pairs: int
    p50_corr: float
    p95_corr: float
    p99_corr: float


@dataclass
class CalibOutput:
    region: str
    top_n_requested: int
    alphas_loaded: int
    alphas_with_pnl: int
    intra_family_pairs: int
    cross_family_pairs: int
    intra_p50: float
    intra_p95: float
    intra_p99: float
    cross_p50: float
    cross_p95: float
    cross_p99: float
    recommended_tau: float
    per_family: List[FamilyStats]
    sample_size_sufficient: bool


# ---------------------------------------------------------------------------
# DB pull
# ---------------------------------------------------------------------------

async def load_top_n_pass_alphas(region: str, top_n: int) -> List[Dict]:
    """SELECT top-N PASS alpha by sharpe, return [{id, alpha_id, sharpe,
    expression, family_signature}, ...]."""
    try:
        from sqlalchemy import text as _text
        from backend.database import AsyncSessionLocal
    except Exception as e:
        logger.error("DB import failed: %s", e)
        return []

    rows: List[Dict] = []
    async with AsyncSessionLocal() as s:
        try:
            r = await s.execute(_text("""
                SELECT id, alpha_id, is_sharpe, expression, family_signature
                FROM alphas
                WHERE region = :region
                  AND quality_status = 'PASS'
                  AND alpha_id IS NOT NULL
                  AND family_signature IS NOT NULL
                  AND is_sharpe IS NOT NULL
                ORDER BY is_sharpe DESC NULLS LAST
                LIMIT :top_n
            """), {"region": region, "top_n": top_n})
            for row in r.mappings():
                rows.append(dict(row))
        except Exception as e:
            logger.error("Top-N PASS query failed: %s", e)
    logger.info(f"Loaded {len(rows)} PASS alphas for region={region}")
    return rows


# ---------------------------------------------------------------------------
# PnL fetch
# ---------------------------------------------------------------------------

async def build_pnl_matrix(
    alpha_rows: List[Dict],
) -> Tuple[pd.DataFrame, List[Dict]]:
    """Fetch each alpha's daily PnL via CorrelationService._fetch_pnl_series.

    Returns (wide_df, surviving_rows). wide_df has dates as index, one
    column per alpha_id. surviving_rows is the subset of alpha_rows
    whose PnL fetch succeeded (non-empty Series).
    """
    try:
        from backend.adapters.brain_adapter import BrainAdapter
        from backend.services.correlation_service import CorrelationService
    except Exception as e:
        logger.error("Brain/CorrelationService import failed: %s", e)
        return pd.DataFrame(), []

    brain = BrainAdapter()
    try:
        await brain.authenticate()
    except Exception as e:
        logger.error("BRAIN authenticate failed: %s — abort", e)
        return pd.DataFrame(), []

    svc = CorrelationService(brain)
    series_dict: Dict[str, pd.Series] = {}
    surviving: List[Dict] = []
    for i, row in enumerate(alpha_rows):
        aid = row["alpha_id"]
        try:
            s = await svc._fetch_pnl_series(aid, max_attempts=2)
            if s.empty:
                logger.warning(f"[{i+1}/{len(alpha_rows)}] alpha_id={aid} empty PnL — skip")
                continue
            series_dict[aid] = s
            surviving.append(row)
            if (i + 1) % 10 == 0:
                logger.info(f"PnL fetch progress: {i+1}/{len(alpha_rows)} ({len(surviving)} good)")
        except Exception as e:
            logger.warning(f"alpha_id={aid} fetch error: {e}")

    if not series_dict:
        return pd.DataFrame(), []

    df = pd.DataFrame(series_dict)
    logger.info(f"PnL matrix: {df.shape[0]} dates × {df.shape[1]} alphas")
    return df, surviving


# ---------------------------------------------------------------------------
# Pairwise correlation analysis
# ---------------------------------------------------------------------------

def compute_pair_stats(
    pnl_matrix: pd.DataFrame,
    surviving_rows: List[Dict],
) -> Tuple[List[float], List[float], List[FamilyStats]]:
    """Compute corr matrix, partition pairs into intra-family / cross-family.

    Returns (intra_corrs, cross_corrs, per_family_stats).
    """
    if pnl_matrix.empty or pnl_matrix.shape[1] < 2:
        return [], [], []

    # Daily returns (per CorrelationService._series_to_returns convention)
    returns = pnl_matrix - pnl_matrix.ffill().shift(1)
    corr_mat = returns.corr(min_periods=60)  # ≥60 overlapping days

    alpha_to_family: Dict[str, str] = {r["alpha_id"]: r["family_signature"] for r in surviving_rows}
    alphas = list(corr_mat.columns)

    intra: List[float] = []
    cross: List[float] = []
    family_pairs: Dict[str, List[float]] = {}

    for i in range(len(alphas)):
        for j in range(i + 1, len(alphas)):
            a_i = alphas[i]
            a_j = alphas[j]
            c = corr_mat.iloc[i, j]
            if pd.isna(c):
                continue
            fam_i = alpha_to_family.get(a_i)
            fam_j = alpha_to_family.get(a_j)
            if fam_i is None or fam_j is None:
                continue
            if fam_i == fam_j:
                intra.append(float(c))
                family_pairs.setdefault(fam_i, []).append(float(c))
            else:
                cross.append(float(c))

    per_family: List[FamilyStats] = []
    for fam_sig, corrs in family_pairs.items():
        if not corrs:
            continue
        per_family.append(FamilyStats(
            family_signature=fam_sig,
            n_alphas=sum(1 for r in surviving_rows if r["family_signature"] == fam_sig),
            n_pairs=len(corrs),
            p50_corr=float(np.percentile(corrs, 50)),
            p95_corr=float(np.percentile(corrs, 95)),
            p99_corr=float(np.percentile(corrs, 99)),
        ))

    return intra, cross, per_family


def recommend_tau(intra: List[float]) -> float:
    """Recommend FAMILY_BAN_MIN_PAIRWISE_CORR per plan v5 §6.7.

    τ = median(intra_p95, intra_p99) — captures the upper tail of
    same-family correlation while leaving room for legitimately divergent
    alphas (different fields/windows in the same operator skeleton).
    """
    if len(intra) < 10:
        return float("nan")
    p95 = np.percentile(intra, 95)
    p99 = np.percentile(intra, 99)
    return float((p95 + p99) / 2.0)


# ---------------------------------------------------------------------------
# CLI / orchestration
# ---------------------------------------------------------------------------

async def main_async(args) -> CalibOutput:
    rows = await load_top_n_pass_alphas(args.region, args.top_n)
    if not rows:
        return CalibOutput(
            region=args.region,
            top_n_requested=args.top_n,
            alphas_loaded=0,
            alphas_with_pnl=0,
            intra_family_pairs=0,
            cross_family_pairs=0,
            intra_p50=float("nan"),
            intra_p95=float("nan"),
            intra_p99=float("nan"),
            cross_p50=float("nan"),
            cross_p95=float("nan"),
            cross_p99=float("nan"),
            recommended_tau=float("nan"),
            per_family=[],
            sample_size_sufficient=False,
        )

    pnl_matrix, surviving = await build_pnl_matrix(rows)
    intra, cross, per_family = compute_pair_stats(pnl_matrix, surviving)

    def _p(arr: List[float], q: float) -> float:
        return float(np.percentile(arr, q)) if arr else float("nan")

    sufficient = len(intra) >= args.min_pairs

    out = CalibOutput(
        region=args.region,
        top_n_requested=args.top_n,
        alphas_loaded=len(rows),
        alphas_with_pnl=len(surviving),
        intra_family_pairs=len(intra),
        cross_family_pairs=len(cross),
        intra_p50=_p(intra, 50),
        intra_p95=_p(intra, 95),
        intra_p99=_p(intra, 99),
        cross_p50=_p(cross, 50),
        cross_p95=_p(cross, 95),
        cross_p99=_p(cross, 99),
        recommended_tau=recommend_tau(intra) if sufficient else float("nan"),
        per_family=per_family,
        sample_size_sufficient=sufficient,
    )

    return out


def _output_to_dict(out: CalibOutput) -> Dict:
    d = asdict(out)
    d["per_family"] = [asdict(f) for f in out.per_family]
    return d


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--region", default="USA", help="region to calibrate (default USA)")
    p.add_argument("--top-n", type=int, default=100, help="top-N PASS alpha (default 100)")
    p.add_argument("--min-pairs", type=int, default=30,
                   help="min intra-family pair count to recommend τ (default 30)")
    p.add_argument("--out", default=None, help="JSON output path; stdout if omitted")
    args = p.parse_args()

    out = asyncio.run(main_async(args))
    payload = _output_to_dict(out)

    logger.info("=" * 60)
    logger.info(f"Region:                  {out.region}")
    logger.info(f"Alphas loaded:           {out.alphas_loaded} / {out.top_n_requested}")
    logger.info(f"Alphas with PnL:         {out.alphas_with_pnl}")
    logger.info(f"Intra-family pairs:      {out.intra_family_pairs}")
    logger.info(f"Cross-family pairs:      {out.cross_family_pairs}")
    if out.sample_size_sufficient:
        logger.info(f"Intra-family p50/p95/p99: {out.intra_p50:.3f} / {out.intra_p95:.3f} / {out.intra_p99:.3f}")
        logger.info(f"Cross-family p50/p95/p99: {out.cross_p50:.3f} / {out.cross_p95:.3f} / {out.cross_p99:.3f}")
        logger.info(f"Recommended τ FAMILY_BAN_MIN_PAIRWISE_CORR: {out.recommended_tau:.3f}")
    else:
        logger.warning(
            f"INSUFFICIENT data — intra-family pairs={out.intra_family_pairs} "
            f"< min={args.min_pairs}. No τ recommendation."
        )

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        logger.info(f"Wrote JSON output: {args.out}")
    else:
        print(json.dumps(payload, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
