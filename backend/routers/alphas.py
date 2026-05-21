"""
Alphas Router - Alpha Lab functionality with feedback support

Uses AlphaService for all business logic.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime

from backend.database import get_db
from backend.services import AlphaService, AlphaListFilters
from backend.tasks import sync_user_alphas

router = APIRouter(
    prefix="/alphas",
    tags=["alphas"],
    responses={404: {"description": "Not found"}},
)


# =============================================================================
# DEPENDENCY INJECTION
# =============================================================================

def get_alpha_service(db: AsyncSession = Depends(get_db)) -> AlphaService:
    """Get AlphaService instance with injected dependencies."""
    return AlphaService(db)


# =============================================================================
# REQUEST/RESPONSE MODELS
# =============================================================================

class AlphaListItem(BaseModel):
    id: int
    alpha_id: Optional[str] = None
    type: Optional[str] = "REGULAR"
    name: Optional[str] = None
    expression: str
    region: Optional[str] = None
    dataset_id: Optional[str] = None
    quality_status: str
    human_feedback: str
    sharpe: Optional[float] = None
    returns: Optional[float] = None
    turnover: Optional[float] = None
    drawdown: Optional[float] = None
    margin: Optional[float] = None
    fitness: Optional[float] = None
    created_at: Optional[datetime] = None
    date_created: Optional[datetime] = None
    self_corr: Optional[float] = None
    self_corr_source: Optional[str] = None
    date_submitted: Optional[datetime] = None
    can_submit: Optional[bool] = None

    class Config:
        from_attributes = True


class AlphaDetailResponse(BaseModel):
    id: int
    alpha_id: Optional[str] = None
    task_id: Optional[int] = None
    expression: str
    hypothesis: Optional[str] = None
    logic_explanation: Optional[str] = None
    
    # Metadata
    region: Optional[str] = None
    universe: Optional[str] = None
    dataset_id: Optional[str] = None
    fields_used: List[str] = []
    operators_used: List[str] = []
    
    # Status
    status: str = "created"
    quality_status: str = "PENDING"
    human_feedback: str = "NONE"
    feedback_comment: Optional[str] = None
    
    # Metrics
    metrics: dict = {}
    is_metrics: dict = {}
    os_metrics: dict = {}

    created_at: Optional[datetime] = None
    date_submitted: Optional[datetime] = None
    can_submit: Optional[bool] = None

    class Config:
        from_attributes = True


class FeedbackRequest(BaseModel):
    rating: str  # LIKED or DISLIKED
    comment: Optional[str] = None


class SyncResponse(BaseModel):
    message: str
    task_id: str


class AlphaListResponse(BaseModel):
    items: List[AlphaListItem]
    total: int


# =============================================================================
# ENDPOINTS
# =============================================================================

@router.post("/sync", response_model=SyncResponse)
async def sync_alphas(background_tasks: BackgroundTasks = None):
    """
    Trigger background sync of ALL user alphas from Brain.
    Includes IS and OS stages, with full metadata.
    """
    task = sync_user_alphas.delay()
    return SyncResponse(
        message="Alpha sync started",
        task_id=str(task.id)
    )


@router.get("", response_model=AlphaListResponse)
async def list_alphas(
    region: Optional[str] = Query(None),
    quality_status: Optional[str] = Query(None),
    human_feedback: Optional[str] = Query(None),
    dataset_id: Optional[str] = Query(None),
    task_id: Optional[int] = Query(None, description="Restrict to a single task"),
    expression: Optional[str] = Query(None, description="Case-insensitive substring on the alpha expression"),
    min_sharpe: Optional[float] = Query(None),
    max_sharpe: Optional[float] = Query(None),
    min_fitness: Optional[float] = Query(None),
    max_fitness: Optional[float] = Query(None),
    min_turnover: Optional[float] = Query(None),
    max_turnover: Optional[float] = Query(None),
    min_returns: Optional[float] = Query(None),
    max_returns: Optional[float] = Query(None),
    sort_by: str = Query("date_created", description="One of: sharpe, fitness, turnover, returns, drawdown, created_at, region, quality_status, id"),
    sort_order: str = Query("desc", description="asc or desc"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    service: AlphaService = Depends(get_alpha_service),
):
    """
    List alphas with filtering and sorting.
    """
    filters = AlphaListFilters(
        region=region,
        quality_status=quality_status,
        human_feedback=human_feedback,
        dataset_id=dataset_id,
        task_id=task_id,
        expression_search=expression,
        min_sharpe=min_sharpe,
        max_sharpe=max_sharpe,
        min_fitness=min_fitness,
        max_fitness=max_fitness,
        min_turnover=min_turnover,
        max_turnover=max_turnover,
        min_returns=min_returns,
        max_returns=max_returns,
    )
    
    items, total = await service.list_alphas(
        filters=filters,
        sort_by=sort_by,
        sort_order=sort_order,
        limit=limit,
        offset=offset,
    )
    
    # Convert to response model
    response_items = [
        AlphaListItem(
            id=item.id,
            alpha_id=item.alpha_id,
            type=item.type,
            name=item.name,
            expression=item.expression,
            region=item.region,
            dataset_id=item.dataset_id,
            quality_status=item.quality_status,
            human_feedback=item.human_feedback,
            sharpe=item.sharpe,
            returns=item.returns,
            turnover=item.turnover,
            drawdown=item.drawdown,
            margin=item.margin,
            fitness=item.fitness,
            created_at=item.created_at,
            self_corr=item.self_corr,
            self_corr_source=item.self_corr_source,
            date_submitted=item.date_submitted,
            can_submit=item.can_submit,
        )
        for item in items
    ]
    
    return AlphaListResponse(items=response_items, total=total)


@router.get("/{alpha_id}", response_model=AlphaDetailResponse)
async def get_alpha(
    alpha_id: int,
    service: AlphaService = Depends(get_alpha_service),
):
    """
    Get detailed information about an alpha.
    """
    alpha = await service.get_alpha(alpha_id)
    
    if not alpha:
        raise HTTPException(status_code=404, detail="Alpha not found")
    
    return AlphaDetailResponse(
        id=alpha.id,
        alpha_id=alpha.alpha_id,
        task_id=alpha.task_id,
        expression=alpha.expression,
        hypothesis=alpha.hypothesis,
        logic_explanation=alpha.logic_explanation,
        region=alpha.region,
        universe=alpha.universe,
        dataset_id=alpha.dataset_id,
        fields_used=alpha.fields_used,
        operators_used=alpha.operators_used,
        status=alpha.status,
        quality_status=alpha.quality_status,
        human_feedback=alpha.human_feedback,
        feedback_comment=alpha.feedback_comment,
        metrics=alpha.metrics,
        is_metrics=alpha.is_metrics,
        os_metrics=alpha.os_metrics,
        created_at=alpha.created_at,
        date_submitted=alpha.date_submitted,
        can_submit=alpha.can_submit,
    )


class CanSubmitRefreshResponse(BaseModel):
    can_submit: Optional[bool] = None
    failed_checks: list = []
    pending_checks: list = []
    message: Optional[str] = None


@router.post("/{alpha_id}/refresh-can-submit", response_model=CanSubmitRefreshResponse)
async def refresh_can_submit(
    alpha_id: int,
    service: AlphaService = Depends(get_alpha_service),
):
    """Re-fetch BRAIN GET /alphas/{id}, recompute can_submit based on
    is.checks (no FAIL → True). Persists to alphas.can_submit + metrics
    audit fields. Returns the new verdict + diagnostic check lists.

    BRAIN unreachable / no alpha_id → returns can_submit=None with a message.
    """
    result = await service.refresh_can_submit(alpha_id)
    if result is None:
        return CanSubmitRefreshResponse(
            message="BRAIN 调用失败或 alpha 缺少 alpha_id；can_submit 未更新"
        )
    return CanSubmitRefreshResponse(**result)


class SubmitResponse(BaseModel):
    submitted: bool
    reason: str
    self_corr: Optional[float] = None
    self_corr_source: Optional[str] = None


@router.post("/{alpha_id}/submit", response_model=SubmitResponse)
async def submit_alpha_to_brain(
    alpha_id: int,
    service: AlphaService = Depends(get_alpha_service),
):
    """Submit an alpha to BRAIN for evaluation.

    Pre-flight gates run server-side (AlphaService.submit_alpha): the alpha
    must have a BRAIN alpha_id, must not already be submitted, can_submit
    must be True, and the local self-correlation precheck must be < 0.7.
    Any gate failure returns submitted=false with a human-readable reason
    (HTTP 200, not an error) so the UI can show it inline. Submit is
    irreversible and consumes BRAIN quota.
    """
    result = await service.submit_alpha(alpha_id)
    return SubmitResponse(
        submitted=result.get("submitted", False),
        reason=result.get("reason", "unknown"),
        self_corr=result.get("self_corr"),
        self_corr_source=result.get("self_corr_source"),
    )


class MarginalContributionDelta(BaseModel):
    sharpe: Optional[float] = None
    fitness: Optional[float] = None
    turnover: Optional[float] = None
    returns: Optional[float] = None
    pnl: Optional[float] = None
    drawdown: Optional[float] = None
    score: Optional[float] = None


class MarginalContributionResponse(BaseModel):
    alpha_pk: int
    alpha_brain_id: str
    scope: str
    deltas: MarginalContributionDelta
    raw: dict
    message: Optional[str] = None


@router.get(
    "/{alpha_id}/marginal-contribution",
    response_model=MarginalContributionResponse,
)
async def get_alpha_marginal_contribution(
    alpha_id: int,
    competition: Optional[str] = Query(
        None,
        description="Competition ID (e.g. IQC2026S1). Mutually exclusive with team_id; "
                    "when both omitted, BRAIN returns the user's personal portfolio delta.",
    ),
    team_id: Optional[str] = Query(
        None, description="Team ID for team-scoped comparison."
    ),
    service: AlphaService = Depends(get_alpha_service),
):
    """Fetch BRAIN marginal performance (standalone vs merged) for an alpha.

    IQC submission workflow: competition leaderboard ranks teams by the
    MERGED score (after-merge), not by the standalone IS sharpe each alpha
    shows in the alphas table. This endpoint surfaces that delta so the user
    can pick which can_submit alphas actually help team score before sending
    them to the submission queue.

    Performance: BRAIN computes the comparison asynchronously and may serve
    Retry-After while computing; the adapter polls up to 30× internally.
    Typical first call ~5-20s, subsequent calls ~1-3s (BRAIN-side cache).
    """
    result = await service.get_marginal_contribution(
        alpha_id, competition=competition, team_id=team_id,
    )
    if result is None:
        raise HTTPException(
            status_code=404,
            detail="Alpha not found, missing BRAIN alpha_id, or BRAIN call failed.",
        )
    return MarginalContributionResponse(**result)


@router.post("/{alpha_id}/feedback")
async def submit_feedback(
    alpha_id: int,
    request: FeedbackRequest,
    service: AlphaService = Depends(get_alpha_service),
):
    """
    Submit human feedback for an alpha (Human-in-the-Loop).
    This feedback is used by the Feedback Agent to improve future mining.
    """
    if request.rating not in ["LIKED", "DISLIKED"]:
        raise HTTPException(status_code=400, detail="Rating must be LIKED or DISLIKED")
    
    success = await service.submit_feedback(
        alpha_id=alpha_id,
        rating=request.rating,
        comment=request.comment,
    )
    
    if not success:
        raise HTTPException(status_code=404, detail="Alpha not found")
    
    return {
        "message": "Feedback submitted",
        "alpha_id": alpha_id,
        "rating": request.rating,
    }


@router.get("/{alpha_id}/trace", response_model=dict)
async def get_alpha_trace(
    alpha_id: int,
    service: AlphaService = Depends(get_alpha_service),
):
    """
    Get the trace step that generated this alpha.
    Shows the full context: RAG query, hypothesis, code generation, etc.
    """
    trace = await service.get_alpha_trace(alpha_id)

    if trace is None:
        raise HTTPException(status_code=404, detail="Alpha not found")

    return trace


# =============================================================================
# Status transition history
# =============================================================================
# Post tier-system removal (2026-05-18) the tier-aware lineage endpoint
# (/alphas/{id}/lineage) was deleted along with its LineageNode + LineageResponse
# Pydantic models. parent_alpha_id stays on the Alpha row for flat hypothesis
# lineage tracking; if a flat lineage view is needed later, build a fresh
# tier-agnostic endpoint.

class TransitionEntry(BaseModel):
    id: int
    old_status: Optional[str] = None
    new_status: str
    sharpe_at_transition: Optional[float] = None
    reason: Optional[str] = None
    source: Optional[str] = None
    transitioned_at: datetime


class TransitionsResponse(BaseModel):
    alpha_id: int
    transitions: List[TransitionEntry] = []


@router.get("/{alpha_id}/transitions", response_model=TransitionsResponse)
async def get_alpha_transitions(
    alpha_id: int,
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """Status transition history for an alpha (newest-first)."""
    from sqlalchemy import select as _select
    from backend.models import AlphaStatusTransition

    q = (
        _select(AlphaStatusTransition)
        .where(AlphaStatusTransition.alpha_id == alpha_id)
        .order_by(AlphaStatusTransition.transitioned_at.desc())
        .limit(limit)
    )
    rows = (await db.execute(q)).scalars().all()
    return TransitionsResponse(
        alpha_id=alpha_id,
        transitions=[
            TransitionEntry(
                id=r.id,
                old_status=r.old_status,
                new_status=r.new_status,
                sharpe_at_transition=r.sharpe_at_transition,
                reason=r.reason,
                source=r.source,
                transitioned_at=r.transitioned_at,
            )
            for r in rows
        ],
    )


@router.get("/by-brain-id/{brain_alpha_id}", response_model=AlphaDetailResponse)
async def get_alpha_by_brain_id(
    brain_alpha_id: str,
    service: AlphaService = Depends(get_alpha_service),
):
    """
    Get an alpha by its BRAIN platform ID.
    """
    alpha = await service.get_alpha_by_brain_id(brain_alpha_id)
    
    if not alpha:
        raise HTTPException(status_code=404, detail="Alpha not found")
    
    return AlphaDetailResponse(
        id=alpha.id,
        alpha_id=alpha.alpha_id,
        task_id=alpha.task_id,
        expression=alpha.expression,
        hypothesis=alpha.hypothesis,
        logic_explanation=alpha.logic_explanation,
        region=alpha.region,
        universe=alpha.universe,
        dataset_id=alpha.dataset_id,
        fields_used=alpha.fields_used,
        operators_used=alpha.operators_used,
        status=alpha.status,
        quality_status=alpha.quality_status,
        human_feedback=alpha.human_feedback,
        feedback_comment=alpha.feedback_comment,
        metrics=alpha.metrics,
        is_metrics=alpha.is_metrics,
        os_metrics=alpha.os_metrics,
        created_at=alpha.created_at,
        date_submitted=alpha.date_submitted,
        can_submit=alpha.can_submit,
    )


# =============================================================================
# Bulk maintenance endpoints (absorbed from retired factor_library router)
# =============================================================================

class CanSubmitRefreshBatchResponse(BaseModel):
    scanned: int
    refreshed: int
    pass_count: int
    fail_count: int
    skipped: int
    sampled_failures: List[dict] = []


@router.post(
    "/refresh-can-submit",
    response_model=CanSubmitRefreshBatchResponse,
)
async def refresh_can_submit_batch(
    quality_status: str = Query(
        "PASS",
        description="Filter by quality_status. Default 'PASS' to spare BRAIN quota.",
    ),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Bulk re-check can_submit for a tranche of alphas. Sequential 1 req/sec
    pacing inside; do not call concurrently with another batch refresh.

    Use this to backfill historical alphas after they were first ingested
    without BRAIN-checks resolution.
    """
    import asyncio
    from sqlalchemy import select as _select
    from backend.adapters.brain_adapter import BrainAdapter
    from backend.models import Alpha

    q = (
        _select(Alpha.id)
        .where(Alpha.quality_status == quality_status)
        .where(Alpha.alpha_id.isnot(None))
        .order_by(Alpha.id.desc())
        .limit(limit)
    )
    ids = [r[0] for r in (await db.execute(q)).all()]

    svc = AlphaService(db)
    refreshed = pass_n = fail_n = skipped = 0
    sampled_failures = []
    async with BrainAdapter() as ba:
        for aid in ids:
            await asyncio.sleep(1.0)
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


class IqcRefreshResponse(BaseModel):
    enqueued: int
    competition: str
    message: str


@router.post("/refresh-iqc", response_model=IqcRefreshResponse)
async def refresh_iqc_batch(
    scope: str = Query("submittable", pattern="^(submittable|all_can_submit)$"),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Enqueue IQC marginal-contribution re-audits for the 可提交 tab.

    IQC Δscore is dynamic — it shifts every time the team submits another
    alpha — so the 可提交 tab surfaces it as a (red-when-negative) column
    rather than a hard filter. Each audit runs as a fire-and-forget Celery
    task that writes back to alpha.metrics._iqc_marginal; this call returns
    immediately with the enqueued count.

    scope:
      - submittable (default): can_submit=True + unsubmitted + self_corr<0.7|null
      - all_can_submit: every can_submit=True + unsubmitted alpha
    """
    from sqlalchemy import select as _select, or_, Float
    from loguru import logger
    from backend.config import settings
    from backend.models import Alpha
    from backend.tasks.refresh_tasks import audit_iqc_marginal_for_alpha

    competition = settings.IQC_AUTO_AUDIT_COMPETITION or "IQC2026S1"

    q = _select(Alpha.id).where(
        Alpha.can_submit.is_(True),
        Alpha.date_submitted.is_(None),
    )
    if scope == "submittable":
        _self_corr = Alpha.metrics["_self_corr"].astext.cast(Float)
        q = q.where(or_(_self_corr < 0.7, _self_corr.is_(None)))
    q = q.order_by(Alpha.is_sharpe.desc().nullslast()).limit(limit)
    ids = [r[0] for r in (await db.execute(q)).all()]

    enqueued = 0
    last_countdown = 0
    for i, aid in enumerate(ids):
        try:
            audit_iqc_marginal_for_alpha.apply_async(
                args=[aid, competition], countdown=i * 2,
            )
            enqueued += 1
            last_countdown = i * 2
        except Exception as e:
            logger.warning(f"[refresh-iqc] enqueue failed for alpha_pk={aid}: {e}")

    eta = last_countdown
    return IqcRefreshResponse(
        enqueued=enqueued,
        competition=competition,
        message=(
            f"已触发 {enqueued} 个 IQC 审计（错开排队，约 {eta}s 内完成）；"
            f"稍后刷新表格查看更新后的 Δscore"
        ),
    )
