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
# PR3 — Tier system: lineage tree + transition history
# =============================================================================

class LineageNode(BaseModel):
    id: int
    alpha_id: Optional[str] = None
    expression: str
    factor_tier: Optional[int] = None
    quality_status: str
    is_sharpe: Optional[float] = None


class LineageResponse(BaseModel):
    self: LineageNode
    ancestors: List[LineageNode] = []      # parent → grandparent → ... up to root
    descendants: List[LineageNode] = []    # direct children only (one level)
    note: Optional[str] = None             # e.g. "not in tier hierarchy"


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


@router.get("/{alpha_id}/lineage", response_model=LineageResponse)
async def get_alpha_lineage(
    alpha_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Tier-aware lineage tree for an alpha.

    - alpha.factor_tier in {1,2,3}: returns ancestors (parent_alpha_id chain)
      and direct descendants (rows where parent_alpha_id == this id).
    - alpha.factor_tier IS NULL: returns empty lists with a note. Frontend
      shows an info banner instead of the tree.
    """
    from sqlalchemy import select as _select
    from backend.models import Alpha as _Alpha

    target = await db.get(_Alpha, alpha_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Alpha not found")

    def _to_node(a: _Alpha) -> LineageNode:
        return LineageNode(
            id=a.id,
            alpha_id=a.alpha_id,
            expression=(a.expression or "")[:300],
            factor_tier=a.factor_tier,
            quality_status=a.quality_status or "PENDING",
            is_sharpe=a.is_sharpe,
        )

    self_node = _to_node(target)
    if target.factor_tier is None:
        return LineageResponse(
            self=self_node,
            ancestors=[],
            descendants=[],
            note="not in tier hierarchy",
        )

    # Ancestors — walk parent_alpha_id up to root, capping at 5 to prevent
    # accidental loops (shouldn't happen but defensive).
    ancestors: List[LineageNode] = []
    current = target
    for _ in range(5):
        if not current.parent_alpha_id:
            break
        parent = await db.get(_Alpha, current.parent_alpha_id)
        if parent is None or parent.id == current.id:
            break
        ancestors.append(_to_node(parent))
        current = parent

    # Direct descendants — one-level fanout for now (recursive could be added
    # but UI renders only one level cleanly).
    desc_q = (
        _select(_Alpha)
        .where(_Alpha.parent_alpha_id == alpha_id)
        .order_by(_Alpha.is_sharpe.desc().nullslast())
        .limit(50)
    )
    desc_rows = (await db.execute(desc_q)).scalars().all()
    descendants = [_to_node(d) for d in desc_rows]

    return LineageResponse(
        self=self_node,
        ancestors=ancestors,
        descendants=descendants,
    )


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
