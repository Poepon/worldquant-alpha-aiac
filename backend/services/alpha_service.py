"""
Alpha Service - Business logic for alpha management

Provides methods for:
- Listing and filtering alphas
- Alpha details and trace retrieval
- Human feedback submission
- Statistics and aggregations
"""

import logging
from typing import List, Optional, Dict, Any
from datetime import datetime
from dataclasses import dataclass
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func

from backend.services.base import BaseService
from backend.repositories import AlphaRepository
from backend.protocols.repository_protocol import PaginationParams, PaginatedResult
from backend.models import Alpha, AlphaStatusTransition, TraceStep

logger = logging.getLogger("services.alpha")


@dataclass
class AlphaListFilters:
    """Filters for alpha listing."""
    region: Optional[str] = None
    quality_status: Optional[str] = None
    human_feedback: Optional[str] = None
    dataset_id: Optional[str] = None
    task_id: Optional[int] = None
    # Expression substring search (case-insensitive ILIKE)
    expression_search: Optional[str] = None
    # IS metric range filters (None = no bound)
    min_sharpe: Optional[float] = None
    max_sharpe: Optional[float] = None
    min_fitness: Optional[float] = None
    max_fitness: Optional[float] = None
    min_turnover: Optional[float] = None
    max_turnover: Optional[float] = None
    min_returns: Optional[float] = None
    max_returns: Optional[float] = None


# Sort key mapping: external name -> SQLAlchemy column attribute. Keeps the
# API stable while DB column names ("is_*") remain implementation detail.
_SORT_COLUMN_MAP = {
    "sharpe": "is_sharpe",
    "fitness": "is_fitness",
    "turnover": "is_turnover",
    "returns": "is_returns",
    "drawdown": "is_drawdown",
    "created_at": "date_created",
    "date_created": "date_created",
    "id": "id",
    "region": "region",
    "quality_status": "quality_status",
}


@dataclass
class AlphaListItem:
    """Simplified alpha for list views."""
    id: int
    alpha_id: Optional[str]
    type: str
    name: Optional[str]
    expression: str
    region: Optional[str]
    dataset_id: Optional[str]
    quality_status: str
    human_feedback: str
    sharpe: Optional[float]
    returns: Optional[float]
    turnover: Optional[float]
    drawdown: Optional[float]
    margin: Optional[float]
    fitness: Optional[float]
    created_at: Optional[datetime]


@dataclass
class AlphaDetail:
    """Full alpha details."""
    id: int
    alpha_id: Optional[str]
    task_id: Optional[int]
    expression: str
    hypothesis: Optional[str]
    logic_explanation: Optional[str]
    region: Optional[str]
    universe: Optional[str]
    dataset_id: Optional[str]
    fields_used: List[str]
    operators_used: List[str]
    status: str
    quality_status: str
    human_feedback: str
    feedback_comment: Optional[str]
    metrics: Dict[str, Any]
    is_metrics: Dict[str, Any]
    os_metrics: Dict[str, Any]
    created_at: Optional[datetime]
    date_submitted: Optional[datetime]
    can_submit: Optional[bool]


class AlphaService(BaseService):
    """
    Service for alpha-related operations.
    
    Provides a clean interface for alpha management,
    abstracting database operations from routers.
    """
    
    def __init__(self, db: AsyncSession):
        super().__init__(db)
        self.alpha_repo = AlphaRepository(db)
    
    # =========================================================================
    # List Operations
    # =========================================================================
    
    async def list_alphas(
        self,
        filters: AlphaListFilters,
        sort_by: str = "date_created",
        sort_order: str = "desc",
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[List[AlphaListItem], int]:
        """
        List alphas with filtering and sorting.
        
        Args:
            filters: Filter criteria
            sort_by: Column to sort by
            sort_order: 'asc' or 'desc'
            limit: Maximum results
            offset: Pagination offset
            
        Returns:
            Tuple of (items, total_count)
        """
        # Build query
        query = select(Alpha)
        count_query = select(func.count()).select_from(Alpha)
        
        # Apply filters
        if filters.region:
            query = query.where(Alpha.region == filters.region)
            count_query = count_query.where(Alpha.region == filters.region)
        
        if filters.quality_status:
            query = query.where(Alpha.quality_status == filters.quality_status)
            count_query = count_query.where(Alpha.quality_status == filters.quality_status)
        
        if filters.human_feedback:
            query = query.where(Alpha.human_feedback == filters.human_feedback)
            count_query = count_query.where(Alpha.human_feedback == filters.human_feedback)
        
        if filters.dataset_id:
            query = query.where(Alpha.dataset_id == filters.dataset_id)
            count_query = count_query.where(Alpha.dataset_id == filters.dataset_id)
        
        if filters.task_id:
            query = query.where(Alpha.task_id == filters.task_id)
            count_query = count_query.where(Alpha.task_id == filters.task_id)

        if filters.expression_search:
            pattern = f"%{filters.expression_search}%"
            query = query.where(Alpha.expression.ilike(pattern))
            count_query = count_query.where(Alpha.expression.ilike(pattern))

        # Numeric range filters on IS metrics
        for column, lo, hi in (
            (Alpha.is_sharpe,   filters.min_sharpe,   filters.max_sharpe),
            (Alpha.is_fitness,  filters.min_fitness,  filters.max_fitness),
            (Alpha.is_turnover, filters.min_turnover, filters.max_turnover),
            (Alpha.is_returns,  filters.min_returns,  filters.max_returns),
        ):
            if lo is not None:
                query = query.where(column >= lo)
                count_query = count_query.where(column >= lo)
            if hi is not None:
                query = query.where(column <= hi)
                count_query = count_query.where(column <= hi)

        # Get total count
        total = (await self.db.execute(count_query)).scalar() or 0

        # Apply sorting via the public sort-key map. Unknown keys fall back to
        # date_created to avoid leaking arbitrary column access.
        sort_attr = _SORT_COLUMN_MAP.get(sort_by, "date_created")
        sort_column = getattr(Alpha, sort_attr, Alpha.date_created)
        if sort_order.lower() == "desc":
            query = query.order_by(sort_column.desc().nullslast())
        else:
            query = query.order_by(sort_column.asc().nullsfirst())
        
        query = query.limit(limit).offset(offset)
        
        result = await self.db.execute(query)
        alphas = result.scalars().all()
        
        # Convert to list items
        items = [self._to_list_item(a) for a in alphas]
        
        return items, total
    
    def _to_list_item(self, alpha: Alpha) -> AlphaListItem:
        """Convert Alpha model to AlphaListItem."""
        expression = alpha.expression or "N/A"
        if len(expression) > 100:
            expression = expression[:100] + "..."
        
        margin = None
        if alpha.is_metrics and isinstance(alpha.is_metrics, dict):
            margin = alpha.is_metrics.get("margin")
        
        return AlphaListItem(
            id=alpha.id,
            alpha_id=alpha.alpha_id,
            type=alpha.type or "REGULAR",
            name=alpha.name,
            expression=expression,
            region=alpha.region,
            dataset_id=alpha.dataset_id,
            quality_status=alpha.quality_status or "PENDING",
            human_feedback=alpha.human_feedback or "NONE",
            sharpe=alpha.is_sharpe,
            returns=alpha.is_returns,
            turnover=alpha.is_turnover,
            drawdown=alpha.is_drawdown,
            margin=margin,
            fitness=alpha.is_fitness,
            created_at=alpha.date_created or alpha.created_at,
        )
    
    # =========================================================================
    # Get Operations
    # =========================================================================
    
    async def get_alpha(self, alpha_id: int) -> Optional[AlphaDetail]:
        """
        Get detailed alpha information.
        
        Args:
            alpha_id: Database ID of the alpha
            
        Returns:
            AlphaDetail or None if not found
        """
        alpha = await self.alpha_repo.get_by_id(alpha_id)
        if not alpha:
            return None
        
        return self._to_detail(alpha)
    
    async def refresh_can_submit(
        self,
        alpha_pk: int,
        brain_adapter=None,
    ) -> Optional[Dict[str, Any]]:
        """Re-fetch BRAIN GET /alphas/{id}, recompute can_submit, persist.

        Writes:
          - alphas.can_submit (top-level column for fast filter)
          - metrics._brain_failed_checks (list of compact FAIL items)
          - metrics._brain_pending_checks (list of compact PENDING items)
          - metrics._brain_can_submit (mirror of can_submit, kept for legacy
            consumers that already read this field — see evaluation.py line 592)

        BRAIN unreachable / empty response → return None (no overwrite).
        Caller is responsible for owning the BrainAdapter lifecycle (passes it
        in to keep this method test-friendly).

        Returns dict {can_submit, failed_checks, pending_checks} on success,
        None if the alpha is missing alpha_id or BRAIN call failed.
        """
        from backend.can_submit import compute_can_submit

        alpha = await self.alpha_repo.get_by_id(alpha_pk)
        if not alpha or not alpha.alpha_id:
            return None

        if brain_adapter is None:
            from backend.adapters.brain_adapter import BrainAdapter
            async with BrainAdapter() as ba:
                detail = await ba.get_alpha(alpha.alpha_id)
        else:
            detail = await brain_adapter.get_alpha(alpha.alpha_id)

        if not detail:
            return None

        ok, failed, pending = compute_can_submit(detail)
        if ok is None:
            return None

        alpha.can_submit = ok
        new_metrics = dict(alpha.metrics or {})
        new_metrics["_brain_can_submit"] = ok
        new_metrics["_brain_failed_checks"] = failed
        new_metrics["_brain_pending_checks"] = pending
        alpha.metrics = new_metrics
        # Force JSONB column re-write — SQLAlchemy doesn't auto-detect mutation
        # on a plain dict assignment when the JSONB type is the same identity;
        # reassignment + flag_modified is the safe pattern.
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(alpha, "metrics")

        await self.db.commit()

        return {
            "can_submit": ok,
            "failed_checks": failed,
            "pending_checks": pending,
        }

    async def get_alpha_by_brain_id(self, brain_alpha_id: str) -> Optional[AlphaDetail]:
        """
        Get alpha by BRAIN platform ID.
        
        Args:
            brain_alpha_id: BRAIN alpha ID string
            
        Returns:
            AlphaDetail or None if not found
        """
        alpha = await self.alpha_repo.get_by_alpha_id(brain_alpha_id)
        if not alpha:
            return None
        
        return self._to_detail(alpha)
    
    def _to_detail(self, alpha: Alpha) -> AlphaDetail:
        """Convert Alpha model to AlphaDetail."""
        return AlphaDetail(
            id=alpha.id,
            alpha_id=alpha.alpha_id,
            task_id=alpha.task_id,
            expression=alpha.expression,
            hypothesis=alpha.hypothesis,
            logic_explanation=alpha.logic_explanation,
            region=alpha.region,
            universe=alpha.universe,
            dataset_id=alpha.dataset_id,
            fields_used=alpha.fields_used or [],
            operators_used=alpha.operators_used or [],
            status=alpha.status or "created",
            quality_status=alpha.quality_status or "PENDING",
            human_feedback=alpha.human_feedback or "NONE",
            feedback_comment=alpha.feedback_comment,
            metrics=alpha.metrics or {},
            is_metrics=alpha.is_metrics or {},
            os_metrics=alpha.os_metrics or {},
            created_at=alpha.created_at,
            date_submitted=alpha.date_submitted,
            can_submit=alpha.can_submit,
        )
    
    # =========================================================================
    # Trace Operations
    # =========================================================================
    
    async def get_alpha_trace(self, alpha_id: int) -> Optional[Dict[str, Any]]:
        """
        Get the trace steps that generated an alpha.
        
        Args:
            alpha_id: Database ID of the alpha
            
        Returns:
            Dict with trace context or None if not found
        """
        alpha = await self.alpha_repo.get_by_id(alpha_id)
        if not alpha:
            return None
        
        if not alpha.trace_step_id:
            return {"message": "No trace step linked to this alpha"}
        
        # Get the trace step
        step_query = select(TraceStep).where(TraceStep.id == alpha.trace_step_id)
        step_result = await self.db.execute(step_query)
        step = step_result.scalar_one_or_none()
        
        if not step:
            return {"message": "Trace step not found"}
        
        # Get all related trace steps for context
        context_query = (
            select(TraceStep)
            .where(TraceStep.task_id == step.task_id)
            .where(TraceStep.step_order <= step.step_order)
            .order_by(TraceStep.step_order)
        )
        
        context_result = await self.db.execute(context_query)
        context_steps = context_result.scalars().all()
        
        return {
            "alpha_id": alpha_id,
            "trace_step_id": step.id,
            "task_id": step.task_id,
            "context": [
                {
                    "step_type": s.step_type,
                    "step_order": s.step_order,
                    "status": s.status,
                    "input": s.input_data,
                    "output": s.output_data,
                    "duration_ms": s.duration_ms,
                }
                for s in context_steps
            ],
        }
    
    # =========================================================================
    # Feedback Operations
    # =========================================================================
    
    async def submit_feedback(
        self,
        alpha_id: int,
        rating: str,
        comment: Optional[str] = None,
    ) -> bool:
        """
        Submit human feedback for an alpha.
        
        Args:
            alpha_id: Database ID of the alpha
            rating: 'LIKED' or 'DISLIKED'
            comment: Optional feedback comment
            
        Returns:
            True if feedback was submitted, False if alpha not found
        """
        if rating not in ["LIKED", "DISLIKED"]:
            raise ValueError("Rating must be LIKED or DISLIKED")
        
        # Check if alpha exists
        alpha = await self.alpha_repo.get_by_id(alpha_id)
        if not alpha:
            return False
        
        # Update feedback
        await self.db.execute(
            update(Alpha)
            .where(Alpha.id == alpha_id)
            .values(human_feedback=rating, feedback_comment=comment)
        )
        await self.commit()

        # W3: dispatch Voyager-style skill promotion to Celery worker.
        # Branch logic per plan R3 #1 modification:
        #   LIKED + PASS              → promote SUCCESS_PATTERN, +0.2 confidence
        #   LIKED + PASS_PROVISIONAL  → promote (lower confidence)
        #   LIKED + OPTIMIZE          → "user prefers this direction" hint
        #   LIKED + FAIL              → record only as direction signal
        #   DISLIKED                  → -0.15 confidence on existing pattern
        try:
            from backend.tasks import learn_from_alpha
            user_feedback_payload = {
                "rating": rating,
                "comment": comment,
                "quality_status": alpha.quality_status,
            }
            learn_from_alpha.delay(alpha_id, user_feedback=user_feedback_payload)
            logger.info(f"[AlphaService] HITL feedback dispatched: alpha={alpha_id} rating={rating}")
        except Exception as e:
            # Non-fatal: feedback row is already saved, learning is best-effort
            logger.warning(f"[AlphaService] failed to dispatch learn_from_alpha for {alpha_id}: {e}")

        return True
    
    # =========================================================================
    # Statistics
    # =========================================================================
    
    async def get_task_stats(self, task_id: int) -> Dict[str, Any]:
        """
        Get statistics for alphas in a task.
        
        Args:
            task_id: The task ID
            
        Returns:
            Statistics dict
        """
        return await self.alpha_repo.get_task_stats(task_id)
    
    async def get_region_distribution(self, task_id: Optional[int] = None) -> Dict[str, int]:
        """
        Get distribution of alphas by region.

        Args:
            task_id: Optional task filter

        Returns:
            Dict of region -> count
        """
        return await self.alpha_repo.get_region_distribution(task_id)

    # =========================================================================
    # Tier System — quality_status transition audit (PR2)
    # =========================================================================

    async def apply_quality_status_change(
        self,
        alpha_id: int,
        new_status: str,
        reason: str,
        source: str,
    ) -> bool:
        """Single-point writer for alpha.quality_status changes.

        Atomically updates alphas.quality_status and inserts an
        alpha_status_transitions row capturing the change. Wrapping both in
        one transaction guarantees the audit log can never miss a transition.

        Args:
            alpha_id: Database ID (NOT BRAIN alpha_id) of the alpha.
            new_status: Target QualityStatus value (PASS / PASS_PROVISIONAL /
                FAIL / OPTIMIZE / REJECT / PENDING).
            reason: Free-text human-readable explanation. Examples:
                "tier_seed_refresh — sharpe drifted below T3 threshold",
                "user manual review", "tier reclassified".
            source: Controlled enum identifying the code path that triggered
                the change. One of: "node_evaluate" / "tier_seed_refresh" /
                "daily_beat_kb" / "daily_beat_os" / "backfill" / "manual_api".

        Returns:
            True if a transition row was written, False if no-op (status
            unchanged or alpha not found).

        The session commit is the caller's responsibility — this method only
        flushes so the transition is visible within the same transaction.
        """
        alpha = await self.alpha_repo.get_by_id(alpha_id)
        if alpha is None:
            logger.warning(
                f"[AlphaService] apply_quality_status_change: alpha_id={alpha_id} not found"
            )
            return False
        if alpha.quality_status == new_status:
            return False  # no-op, don't pollute audit log

        transition = AlphaStatusTransition(
            alpha_id=alpha_id,
            old_status=alpha.quality_status,
            new_status=new_status,
            sharpe_at_transition=alpha.is_sharpe,
            reason=reason,
            source=source,
        )
        self.db.add(transition)
        alpha.quality_status = new_status
        await self.db.flush()
        logger.info(
            f"[AlphaService] alpha_id={alpha_id} {transition.old_status} -> {new_status} "
            f"(source={source}, reason={reason!r})"
        )
        return True
