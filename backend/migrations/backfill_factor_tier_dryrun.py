"""Backfill dry-run: classify existing alphas + KB entries into tiers, preview impact.

This script is **read-only**. It writes nothing to the database. Output goes to:
- stdout (summary report)
- backfill_plan.json (detailed change list, consumed by the apply step in PR2)

Usage:
    python backend/migrations/backfill_factor_tier_dryrun.py
    python backend/migrations/backfill_factor_tier_dryrun.py --output /tmp/backfill_plan.json

Run this after PR1's alembic migration adds the factor_tier columns. The plan
file produced here is hand-reviewed before the apply step (PR2) executes any
UPDATE statements.

What gets analyzed:
1. Tier classification — for every alpha row, compute classify_tier(expression).
   Compare to current factor_tier (NULL on first run).
2. Quality_status recomputation — for each alpha with a non-NULL new_tier,
   recompute PASS / PASS_PROVISIONAL / FAIL using tier-specific thresholds.
   Surface promotes / demotes / no-change counts.
3. Parent linkage — for T2/T3 alphas, run extract_tier1_seed once or twice and
   look up the resulting kernel in alphas.expression_hash. Report orphans.
4. KB alpha_id_ref backfill — for SUCCESS_PATTERN entries lacking
   meta_data.alpha_id_ref, reverse-lookup by pattern_hash and report match rate.

This script connects with the regular asyncpg DSN. Make sure POSTGRES_* env
vars are set (or .env loaded) before running.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.alpha_semantic_validator import compute_expression_hash
from backend.database import AsyncSessionLocal
from backend.factor_tier_classifier import (
    classify_tier,
    extract_tier1_seed,
    populate_known_fields,
)
from backend.models import Alpha, DataField, KnowledgeEntry


# =============================================================================
# Helpers
# =============================================================================

def _recompute_quality_status(
    factor_tier: Optional[int],
    is_sharpe: Optional[float],
    is_fitness: Optional[float],
    is_turnover: Optional[float],
    sub_universe_sharpe: Optional[float] = None,
    metrics: Optional[Dict] = None,
) -> str:
    """Reproduction of evaluation.node_evaluate's PASS/PROVISIONAL/FAIL gate
    for backfill dry-run impact preview.

    PR4 fix — also inspects metrics.checks for hard-gate FAILs that earlier
    evaluation runs may have missed. Without this check, alphas where
    CONCENTRATED_WEIGHT or LOW_SUB_UNIVERSE_SHARPE failed at sim time would
    silently re-pass through backfill (caused 4 false PASS to slip through
    today before the manual audit caught them).

    Tier-aware skip: T1 ignores BRAIN's submission-level checks entirely
    (project thresholds rule). T2 keeps concentrated, skips self_corr.
    T3 / NULL apply every check.
    """
    from backend.agents.graph.tier_thresholds import get_tier_thresholds

    if is_sharpe is None or is_fitness is None or is_turnover is None:
        return "PENDING"

    t = get_tier_thresholds(factor_tier)

    # PR4: hard-gate failures from metrics.checks. T1 skips entirely (BRAIN
    # checks reflect submission criteria, not T1 seed criteria).
    if factor_tier != 1 and metrics:
        checks = metrics.get("checks") or [] if isinstance(metrics, dict) else []
        for c in checks:
            if not isinstance(c, dict) or c.get("result") != "FAIL":
                continue
            name = c.get("name", "")
            # CONCENTRATED_WEIGHT and LOW_SUB_UNIVERSE_SHARPE are non-negotiable
            # for T2/T3/NULL — same rule as evaluation.node_evaluate.
            if name in ("CONCENTRATED_WEIGHT", "LOW_SUB_UNIVERSE_SHARPE"):
                return "PASS_PROVISIONAL"
            # SELF_CORRELATION FAIL only blocks T3 (T2 explicitly skips per plan)
            if name == "SELF_CORRELATION" and factor_tier == 3:
                return "PASS_PROVISIONAL"

    # PASS check (subuniv_min only enforced if value provided in metrics)
    pass_sharpe = is_sharpe >= t["sharpe_min"]
    pass_fitness = is_fitness >= t["fitness_min"]
    pass_turnover = t["turnover_min"] <= is_turnover <= t["turnover_max"]
    pass_subuniv = (
        t["subuniv_min"] is None
        or sub_universe_sharpe is None  # missing → don't block at this layer
        or sub_universe_sharpe >= t["subuniv_min"]
    )
    if pass_sharpe and pass_fitness and pass_turnover and pass_subuniv:
        return "PASS"

    # PROVISIONAL fallback (tier-specific looser bar)
    prov = t.get("provisional")
    if prov:
        prov_sharpe = is_sharpe >= prov.get("sharpe_min", t["sharpe_min"])
        prov_fitness = is_fitness >= prov.get("fitness_min", t["fitness_min"])
        prov_turn = t["turnover_min"] <= is_turnover <= prov.get(
            "turnover_max", t["turnover_max"]
        )
        if prov_sharpe and prov_fitness and prov_turn:
            return "PASS_PROVISIONAL"

    return "FAIL"


# =============================================================================
# Analysis passes
# =============================================================================

async def analyze_tiers(db: AsyncSession) -> Dict:
    """Pass 1: classify every alpha, find tier diff vs current."""
    result = await db.execute(
        select(Alpha.id, Alpha.expression, Alpha.factor_tier).order_by(Alpha.id)
    )
    rows = result.all()
    tier_changes: List[Dict] = []
    distribution: Counter = Counter()
    for row in rows:
        new_tier = classify_tier(row.expression or "")
        distribution[new_tier] += 1
        if row.factor_tier != new_tier:
            tier_changes.append(
                {
                    "id": row.id,
                    "expr_preview": (row.expression or "")[:120],
                    "old_tier": row.factor_tier,
                    "new_tier": new_tier,
                }
            )
    return {
        "total_alphas": len(rows),
        "distribution": dict(distribution),  # {1: x, 2: y, 3: z, None: w}
        "changes": tier_changes,
    }


async def analyze_quality_recompute(db: AsyncSession) -> Dict:
    """Pass 2: for every alpha with a new tier, recompute quality_status."""
    result = await db.execute(
        select(
            Alpha.id,
            Alpha.expression,
            Alpha.quality_status,
            Alpha.is_sharpe,
            Alpha.is_fitness,
            Alpha.is_turnover,
            Alpha.metrics,
        ).order_by(Alpha.id)
    )
    rows = result.all()
    changes: List[Dict] = []
    transitions: Counter = Counter()
    for row in rows:
        new_tier = classify_tier(row.expression or "")
        if new_tier is None:
            continue  # not in tier hierarchy → leave quality_status alone
        # Try to extract sub-universe sharpe from BRAIN checks if present
        sub_uni = None
        try:
            checks = (row.metrics or {}).get("checks", []) if row.metrics else []
            for chk in checks:
                if chk.get("name") == "LOW_SUB_UNIVERSE_SHARPE":
                    sub_uni = chk.get("value")
                    break
        except Exception:
            pass
        new_status = _recompute_quality_status(
            new_tier, row.is_sharpe, row.is_fitness, row.is_turnover, sub_uni,
            metrics=row.metrics,
        )
        if new_status != row.quality_status:
            transitions[(row.quality_status or "PENDING", new_status)] += 1
            changes.append(
                {
                    "id": row.id,
                    "tier": new_tier,
                    "old_status": row.quality_status,
                    "new_status": new_status,
                    "is_sharpe": row.is_sharpe,
                    "is_fitness": row.is_fitness,
                    "is_turnover": row.is_turnover,
                }
            )
    return {
        "changes": changes,
        "transitions_summary": {f"{k[0]}->{k[1]}": v for k, v in transitions.items()},
    }


async def analyze_parent_links(db: AsyncSession) -> Dict:
    """Pass 3: resolve T2/T3 → parent T1/T2 alpha by expression_hash."""
    # Build expression_hash → alpha_id index
    result = await db.execute(select(Alpha.id, Alpha.expression, Alpha.expression_hash))
    rows = result.all()
    hash_to_id: Dict[str, int] = {}
    for row in rows:
        if row.expression_hash:
            hash_to_id[row.expression_hash] = row.id

    parent_links: List[Dict] = []
    orphans: List[Dict] = []

    for row in rows:
        new_tier = classify_tier(row.expression or "")
        if new_tier not in (2, 3):
            continue
        kernel = extract_tier1_seed(row.expression or "")
        if not kernel:
            orphans.append(
                {
                    "id": row.id,
                    "tier": new_tier,
                    "expr_preview": (row.expression or "")[:120],
                    "reason": "extract_tier1_seed returned None",
                }
            )
            continue
        kernel_hash = compute_expression_hash(kernel)
        parent_id = hash_to_id.get(kernel_hash)
        if parent_id and parent_id != row.id:
            parent_links.append(
                {
                    "child_id": row.id,
                    "child_tier": new_tier,
                    "parent_id": parent_id,
                    "parent_expr_kernel": kernel[:120],
                }
            )
        else:
            orphans.append(
                {
                    "id": row.id,
                    "tier": new_tier,
                    "expr_preview": (row.expression or "")[:120],
                    "synthesized_kernel": kernel[:120],
                    "reason": "kernel hash not found in alphas table",
                }
            )

    return {"parent_links": parent_links, "orphans": orphans}


async def analyze_kb_alpha_id_ref(db: AsyncSession) -> Dict:
    """Pass 4: for KB SUCCESS_PATTERN entries lacking alpha_id_ref, reverse-lookup."""
    result = await db.execute(
        select(KnowledgeEntry.id, KnowledgeEntry.pattern, KnowledgeEntry.meta_data).where(
            KnowledgeEntry.entry_type == "SUCCESS_PATTERN",
            KnowledgeEntry.is_active == True,  # noqa: E712
        )
    )
    rows = result.all()

    # Index expressions by (pattern_hash, region) for matching
    alphas = await db.execute(select(Alpha.id, Alpha.expression, Alpha.region))
    expr_to_alpha: Dict[str, int] = {}
    for row in alphas.all():
        if row.expression:
            key = compute_expression_hash(row.expression)
            # Last-write wins; fine for dry-run preview
            expr_to_alpha[key] = row.id

    matched = 0
    unmatched = 0
    changes: List[Dict] = []
    for kb_row in rows:
        meta = kb_row.meta_data or {}
        if meta.get("alpha_id_ref") is not None:
            continue
        pattern_hash = compute_expression_hash(kb_row.pattern or "")
        match_id = expr_to_alpha.get(pattern_hash)
        if match_id:
            matched += 1
            changes.append({"kb_id": kb_row.id, "matched_alpha_id": match_id})
        else:
            unmatched += 1
            changes.append({"kb_id": kb_row.id, "matched_alpha_id": None})
    return {"matched": matched, "unmatched": unmatched, "changes": changes}


# =============================================================================
# Field cache priming (so classify_tier recognizes DB-backed fields)
# =============================================================================

async def prime_field_cache(db: AsyncSession) -> int:
    result = await db.execute(select(DataField.field_id))
    field_ids = {row.field_id for row in result.all() if row.field_id}
    populate_known_fields(field_ids)
    return len(field_ids)


# =============================================================================
# Main
# =============================================================================

async def main(output_path: Path) -> None:
    async with AsyncSessionLocal() as db:
        n_fields = await prime_field_cache(db)
        logger.info(f"[backfill-dryrun] Primed field cache with {n_fields} DataField rows")

        tier_result = await analyze_tiers(db)
        quality_result = await analyze_quality_recompute(db)
        parent_result = await analyze_parent_links(db)
        kb_result = await analyze_kb_alpha_id_ref(db)

    # Console summary
    print("=" * 70)
    print("Backfill DRY-RUN — factor_tier impact preview")
    print("=" * 70)
    print(f"Generated at: {datetime.utcnow().isoformat()}Z")
    print()
    print(f"Total alphas: {tier_result['total_alphas']}")
    print("Tier distribution (after classify_tier):")
    dist = tier_result["distribution"]
    print(f"  T1:   {dist.get(1, 0)}")
    print(f"  T2:   {dist.get(2, 0)}")
    print(f"  T3:   {dist.get(3, 0)}")
    print(f"  NULL: {dist.get(None, 0)}  (multi-field, single-rank, malformed)")
    print(f"Tier changes vs current factor_tier: {len(tier_result['changes'])}")
    print()
    print("Quality status recompute:")
    for k, v in (quality_result.get("transitions_summary") or {}).items():
        print(f"  {k}: {v}")
    print(f"Total status changes: {len(quality_result['changes'])}")
    print()
    print(f"Parent links resolved: {len(parent_result['parent_links'])}")
    print(f"Orphan T2/T3 (no parent in alphas): {len(parent_result['orphans'])}")
    print()
    print(
        f"KB SUCCESS_PATTERN alpha_id_ref backfill: "
        f"{kb_result['matched']} matched / {kb_result['unmatched']} unmatched"
    )
    print()

    plan = {
        "generated_at": datetime.utcnow().isoformat(),
        "tier_classification": tier_result,
        "quality_recompute": quality_result,
        "parent_links": parent_result,
        "kb_alpha_id_ref": kb_result,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2, default=str)
    print(f"Plan written to: {output_path.resolve()}")


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("backfill_plan.json"),
        help="Path to write the backfill plan JSON (default: ./backfill_plan.json)",
    )
    args = parser.parse_args()
    try:
        asyncio.run(main(args.output))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    cli()
