"""Factor Library Router (PR3) — tier-aware analytics endpoints.

Provides:
- GET /factor-library/stats        per-tier KPI dashboard
- GET /factor-library/alphas       paginated list filtered by tier
- GET /factor-library/promotion-count?days=30
                                   transition-event counts (T1→T2, T2→T3)
- GET /factor-library/seed-availability?tier=  TaskCreate prerequisite check

All read-only. Frontend is the primary consumer.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.graph.tier_thresholds import get_min_seed_count
from backend.database import get_db
from backend.models import Alpha, AlphaStatusTransition

router = APIRouter(
    prefix="/factor-library",
    tags=["factor-library"],
    responses={404: {"description": "Not found"}},
)


# =============================================================================
# Response models
# =============================================================================

class TierKpi(BaseModel):
    tier: int
    pass_count: int
    provisional_count: int
    fail_count: int
    total: int
    avg_sharpe: Optional[float] = None
    median_sharpe: Optional[float] = None
    max_sharpe: Optional[float] = None
    today_pass_increment: int = 0


class StatsResponse(BaseModel):
    tiers: List[TierKpi]
    last_refreshed_at: Optional[datetime] = None


class FactorAlpha(BaseModel):
    id: int
    alpha_id: Optional[str]
    expression: str
    region: Optional[str]
    dataset_id: Optional[str]
    factor_tier: Optional[int]
    parent_alpha_id: Optional[int]
    quality_status: str
    is_sharpe: Optional[float]
    is_fitness: Optional[float]
    is_turnover: Optional[float]
    metrics_snapshot_at: Optional[datetime]
    created_at: Optional[datetime]
    status: Optional[str] = None  # BRAIN status: ACTIVE / UNSUBMITTED / created
    date_submitted: Optional[datetime] = None
    can_submit: Optional[bool] = None  # NULL = 未检查；True = 可提交；False = 不可提交


class AlphaListResponse(BaseModel):
    items: List[FactorAlpha]
    total: int
    limit: int
    offset: int


class PromotionPoint(BaseModel):
    date: str
    t1_to_t2: int
    t2_to_t3: int


class PromotionCountResponse(BaseModel):
    days: int
    points: List[PromotionPoint]


class SeedAvailabilityResponse(BaseModel):
    tier: int
    region: str
    dataset_id: Optional[str]
    available_seeds: int
    min_required: int
    is_ready: bool


# =============================================================================
# Endpoints
# =============================================================================

@router.get("/stats", response_model=StatsResponse)
async def get_stats(db: AsyncSession = Depends(get_db)) -> StatsResponse:
    """Per-tier KPI summary for the FactorLibrary dashboard.

    Returns counts (PASS / PASS_PROVISIONAL / FAIL) and sharpe quartile-ish
    stats (avg / median / max) for each of T1/T2/T3. today_pass_increment
    counts how many alphas transitioned to PASS today (read from the
    transition audit table, NOT a created_at-based proxy).
    """
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    tiers: List[TierKpi] = []
    last_refreshed: Optional[datetime] = None

    for tier in (1, 2, 3):
        # Counts by quality_status for this tier
        counts_q = (
            select(Alpha.quality_status, func.count(Alpha.id))
            .where(Alpha.factor_tier == tier)
            .group_by(Alpha.quality_status)
        )
        counts_rows = (await db.execute(counts_q)).all()
        by_status = {row[0]: int(row[1]) for row in counts_rows}
        pass_n = by_status.get("PASS", 0)
        prov_n = by_status.get("PASS_PROVISIONAL", 0)
        fail_n = by_status.get("FAIL", 0)
        total = sum(by_status.values())

        # Sharpe stats (only over PASS / PROVISIONAL — avoid FAIL noise)
        sharpe_q = (
            select(
                func.avg(Alpha.is_sharpe),
                func.percentile_cont(0.5).within_group(Alpha.is_sharpe.asc()),
                func.max(Alpha.is_sharpe),
            )
            .where(Alpha.factor_tier == tier)
            .where(Alpha.quality_status.in_(["PASS", "PASS_PROVISIONAL"]))
            .where(Alpha.is_sharpe.isnot(None))
        )
        sharpe_row = (await db.execute(sharpe_q)).one_or_none()
        avg_s = float(sharpe_row[0]) if sharpe_row and sharpe_row[0] is not None else None
        med_s = float(sharpe_row[1]) if sharpe_row and sharpe_row[1] is not None else None
        max_s = float(sharpe_row[2]) if sharpe_row and sharpe_row[2] is not None else None

        # today_pass_increment: count transitions to PASS for this tier today
        today_q = (
            select(func.count(AlphaStatusTransition.id))
            .join(Alpha, Alpha.id == AlphaStatusTransition.alpha_id)
            .where(AlphaStatusTransition.new_status == "PASS")
            .where(AlphaStatusTransition.transitioned_at >= today_start)
            .where(Alpha.factor_tier == tier)
        )
        today_inc = int((await db.execute(today_q)).scalar() or 0)

        # Track latest snapshot timestamp for the page header
        snapshot_q = (
            select(func.max(Alpha.metrics_snapshot_at))
            .where(Alpha.factor_tier == tier)
        )
        snap = (await db.execute(snapshot_q)).scalar()
        if snap and (last_refreshed is None or snap > last_refreshed):
            last_refreshed = snap

        tiers.append(TierKpi(
            tier=tier,
            pass_count=pass_n,
            provisional_count=prov_n,
            fail_count=fail_n,
            total=total,
            avg_sharpe=avg_s,
            median_sharpe=med_s,
            max_sharpe=max_s,
            today_pass_increment=today_inc,
        ))

    return StatsResponse(tiers=tiers, last_refreshed_at=last_refreshed)


@router.get("/alphas", response_model=AlphaListResponse)
async def list_alphas_by_tier(
    tier: int = Query(..., ge=1, le=3),
    region: Optional[str] = None,
    dataset_id: Optional[str] = None,
    quality_status: Optional[str] = None,
    min_sharpe: Optional[float] = None,
    max_sharpe: Optional[float] = None,
    expression_search: Optional[str] = None,
    submitted: Optional[bool] = None,  # True = 已提交, False = 未提交, None = 不筛选
    can_submit: Optional[str] = None,  # 'true' | 'false' | 'null' | None (无筛选)
    sort_by: str = Query("is_sharpe", pattern="^(is_sharpe|is_fitness|is_turnover|created_at)$"),
    sort_order: str = Query("desc", pattern="^(asc|desc)$"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> AlphaListResponse:
    """Paginated alpha list filtered to a tier. Used by FactorLibrary tab tables."""
    base = select(Alpha).where(Alpha.factor_tier == tier)
    count_base = select(func.count(Alpha.id)).where(Alpha.factor_tier == tier)

    conds = []
    if region:
        conds.append(Alpha.region == region)
    if dataset_id:
        conds.append(Alpha.dataset_id == dataset_id)
    if quality_status:
        conds.append(Alpha.quality_status == quality_status)
    if min_sharpe is not None:
        conds.append(Alpha.is_sharpe >= min_sharpe)
    if max_sharpe is not None:
        conds.append(Alpha.is_sharpe <= max_sharpe)
    if expression_search:
        conds.append(Alpha.expression.ilike(f"%{expression_search}%"))
    if submitted is True:
        conds.append(Alpha.date_submitted.isnot(None))
    elif submitted is False:
        conds.append(Alpha.date_submitted.is_(None))
    if can_submit == "true":
        conds.append(Alpha.can_submit.is_(True))
    elif can_submit == "false":
        conds.append(Alpha.can_submit.is_(False))
    elif can_submit == "null":
        conds.append(Alpha.can_submit.is_(None))

    if conds:
        base = base.where(and_(*conds))
        count_base = count_base.where(and_(*conds))

    total = int((await db.execute(count_base)).scalar() or 0)

    sort_col = getattr(Alpha, sort_by, Alpha.is_sharpe)
    if sort_order == "desc":
        base = base.order_by(sort_col.desc().nullslast())
    else:
        base = base.order_by(sort_col.asc().nullsfirst())

    rows = (await db.execute(base.limit(limit).offset(offset))).scalars().all()

    items = [
        FactorAlpha(
            id=a.id,
            alpha_id=a.alpha_id,
            expression=(a.expression or "")[:300],
            region=a.region,
            dataset_id=a.dataset_id,
            factor_tier=a.factor_tier,
            parent_alpha_id=a.parent_alpha_id,
            quality_status=a.quality_status or "PENDING",
            is_sharpe=a.is_sharpe,
            is_fitness=a.is_fitness,
            is_turnover=a.is_turnover,
            metrics_snapshot_at=a.metrics_snapshot_at,
            created_at=a.created_at,
            status=a.status,
            date_submitted=a.date_submitted,
            can_submit=a.can_submit,
        )
        for a in rows
    ]
    return AlphaListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/promotion-count", response_model=PromotionCountResponse)
async def get_promotion_count(
    days: int = Query(30, ge=1, le=180),
    db: AsyncSession = Depends(get_db),
) -> PromotionCountResponse:
    """Daily promotion event counts for the FactorLibrary timeline chart.

    Counts transitions where new_status='PASS' AND alpha.factor_tier=N. The
    parent's tier is N-1 by construction so we don't need to join on parent.

    This uses event-stream counts rather than ratios on purpose — denominators
    based on current snapshot are noisy as alphas drift; counts are stable.
    """
    cutoff = datetime.utcnow() - timedelta(days=days)

    # One row per (date, target_tier) → count
    q = (
        select(
            func.date_trunc("day", AlphaStatusTransition.transitioned_at).label("d"),
            Alpha.factor_tier.label("t"),
            func.count(AlphaStatusTransition.id).label("n"),
        )
        .join(Alpha, Alpha.id == AlphaStatusTransition.alpha_id)
        .where(AlphaStatusTransition.transitioned_at >= cutoff)
        .where(AlphaStatusTransition.new_status == "PASS")
        .where(Alpha.factor_tier.in_([2, 3]))
        .group_by("d", "t")
        .order_by("d")
    )
    rows = (await db.execute(q)).all()

    bucket: dict = {}
    for d, t, n in rows:
        date_str = d.strftime("%Y-%m-%d") if d else "?"
        slot = bucket.setdefault(date_str, {"t1_to_t2": 0, "t2_to_t3": 0})
        if t == 2:
            slot["t1_to_t2"] = int(n)
        elif t == 3:
            slot["t2_to_t3"] = int(n)

    points = [
        PromotionPoint(date=d, t1_to_t2=v["t1_to_t2"], t2_to_t3=v["t2_to_t3"])
        for d, v in sorted(bucket.items())
    ]
    return PromotionCountResponse(days=days, points=points)


@router.get("/seed-availability", response_model=SeedAvailabilityResponse)
async def seed_availability(
    tier: int = Query(..., ge=2, le=3),
    region: str = Query(..., min_length=2, max_length=10),
    dataset_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
) -> SeedAvailabilityResponse:
    """How many predecessor-tier PASS alphas are available as seeds?

    Used by the TaskCreate form to enable/disable the start button for
    AUTONOMOUS_TIER2/3 modes. Mirrors the same query that
    TaskService._validate_tier_eligibility runs server-side.
    """
    prior = tier - 1
    q = (
        select(func.count(Alpha.id))
        .where(Alpha.factor_tier == prior)
        .where(Alpha.quality_status == "PASS")
        .where(Alpha.region == region)
    )
    if dataset_id:
        q = q.where(Alpha.dataset_id == dataset_id)
    count = int((await db.execute(q)).scalar() or 0)

    min_req = get_min_seed_count()
    return SeedAvailabilityResponse(
        tier=tier,
        region=region,
        dataset_id=dataset_id,
        available_seeds=count,
        min_required=min_req,
        is_ready=count >= min_req,
    )


class CanSubmitRefreshBatchResponse(BaseModel):
    scanned: int
    refreshed: int
    pass_count: int  # can_submit=True
    fail_count: int  # can_submit=False
    skipped: int     # BRAIN unreachable / missing alpha_id
    sampled_failures: List[dict] = []  # first ~5 alphas with FAIL details for UI


@router.post(
    "/refresh-can-submit",
    response_model=CanSubmitRefreshBatchResponse,
)
async def refresh_can_submit_batch(
    quality_status: str = Query(
        "PASS",
        description="Filter by quality_status. Default 'PASS' to spare BRAIN quota.",
    ),
    tier: Optional[int] = Query(None, ge=1, le=3),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Bulk re-check can_submit for a tranche of alphas. Sequential 1 req/sec
    pacing inside; do not call concurrently with another batch refresh.

    Caller selects scope via quality_status + tier; default selects all PASS
    alphas of any tier. Use this to backfill historical alphas after they were
    first ingested without BRAIN-checks resolution.
    """
    import asyncio
    from backend.adapters.brain_adapter import BrainAdapter
    from backend.services.alpha_service import AlphaService

    q = select(Alpha.id).where(Alpha.quality_status == quality_status).where(Alpha.alpha_id.isnot(None))
    if tier is not None:
        q = q.where(Alpha.factor_tier == tier)
    q = q.order_by(Alpha.id.desc()).limit(limit)
    ids = [r[0] for r in (await db.execute(q)).all()]

    svc = AlphaService(db)
    refreshed = pass_n = fail_n = skipped = 0
    sampled_failures = []
    async with BrainAdapter() as ba:
        for aid in ids:
            await asyncio.sleep(1.0)  # spare BRAIN quota
            res = await svc.refresh_can_submit(aid, brain_adapter=ba)
            if res is None:
                skipped += 1
                continue
            refreshed += 1
            if res["can_submit"]:
                pass_n += 1
            else:
                fail_n += 1
                if len(sampled_failures) < 5:
                    sampled_failures.append({
                        "alpha_pk": aid,
                        "failed": res["failed_checks"],
                        "pending": res["pending_checks"],
                    })
    return CanSubmitRefreshBatchResponse(
        scanned=len(ids),
        refreshed=refreshed,
        pass_count=pass_n,
        fail_count=fail_n,
        skipped=skipped,
        sampled_failures=sampled_failures,
    )
