"""
Task Service - Business logic for mining task management

Provides methods for:
- Task CRUD operations
- Task lifecycle (start, pause, stop)
- Trace step retrieval
- Experiment run management
"""

import logging
from typing import List, Optional, Dict, Any
from datetime import datetime
from dataclasses import dataclass
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from backend.services.base import BaseService
from backend.repositories.task_repository import TaskRepository
from backend.repositories.alpha_repository import AlphaRepository
from backend.models import MiningTask, TraceStep

logger = logging.getLogger("services.task")


# TaskCreateData retired in Phase 1d-2 (create_task endpoint removed; ONESHOT
# task creation is gone — the pool is fed by the scheduler beat).


@dataclass
class TaskSummary:
    """Task summary for list views."""
    id: int
    task_name: str
    region: str
    universe: str
    dataset_strategy: str
    status: str
    daily_goal: int
    progress_current: int
    current_iteration: int
    max_iterations: int
    created_at: datetime
    updated_at: Optional[datetime]
    schedule: Optional[str] = None


@dataclass
class TraceStepInfo:
    """Trace step information."""
    id: int
    step_type: str
    step_order: int
    iteration: int
    input_data: Dict[str, Any]
    output_data: Dict[str, Any]
    duration_ms: Optional[int]
    status: str
    error_message: Optional[str]
    created_at: datetime


@dataclass
class TaskDetail:
    """Full task details with trace steps."""
    id: int
    task_name: str
    region: str
    universe: str
    dataset_strategy: str
    target_datasets: List[str]
    status: str
    daily_goal: int
    progress_current: int
    current_iteration: int
    max_iterations: int
    config: Dict[str, Any]
    created_at: datetime
    updated_at: Optional[datetime]
    trace_steps: List[TraceStepInfo]
    alphas_count: int
    schedule: Optional[str] = None   # ONESHOT | FLAT — needed by frontend to
                                     # route PAUSE/RESUME to the correct
                                     # endpoint (FLAT uses /ops/flat-sessions/*)


# MiningSessionInfo DTO retired in Phase 1d-2 (flat sessions removed; projector
# _to_session_info gone, no caller).


# ExperimentRunInfo retired in Phase 1d-2 (list_task_runs + experiment_runs removed).


class TaskService(BaseService):
    """
    Service for task-related operations.
    
    Provides a clean interface for task management,
    abstracting database operations from routers.
    """
    
    def __init__(self, db: AsyncSession):
        super().__init__(db)
        self.task_repo = TaskRepository(db)
        self.alpha_repo = AlphaRepository(db)
    
    # =========================================================================
    # List Operations
    # =========================================================================
    
    async def list_tasks(
        self,
        status: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[TaskSummary]:
        """
        List tasks with optional status filter.
        
        Args:
            status: Optional status filter
            limit: Maximum results
            offset: Pagination offset
            
        Returns:
            List of TaskSummary
        """
        query = select(MiningTask).order_by(MiningTask.created_at.desc())
        
        if status:
            query = query.where(MiningTask.status == status)
        
        query = query.limit(limit).offset(offset)
        
        result = await self.db.execute(query)
        tasks = result.scalars().all()
        
        return [self._to_summary(t) for t in tasks]
    
    def _to_summary(self, task: MiningTask) -> TaskSummary:
        """Convert MiningTask to TaskSummary."""
        return TaskSummary(
            id=task.id,
            task_name=task.task_name,
            region=task.region,
            universe=task.universe,
            dataset_strategy=task.dataset_strategy,
            status=task.status,
            daily_goal=task.daily_goal,
            # progress_current/current_iteration/max_iterations columns dropped in
            # Phase 1d-2 (never written post-pool) — fields kept at 0 for API shape.
            progress_current=0,
            current_iteration=0,
            max_iterations=0,
            created_at=task.created_at,
            updated_at=task.updated_at,
            schedule=getattr(task, "schedule", None),
        )

    # =========================================================================
    # Get Operations
    # =========================================================================
    
    async def get_task(self, task_id: int) -> Optional[TaskSummary]:
        """
        Get task summary by ID.
        
        Args:
            task_id: Task ID
            
        Returns:
            TaskSummary or None
        """
        task = await self.task_repo.get_by_id(task_id)
        if not task:
            return None
        return self._to_summary(task)
    
    async def get_task_detail(self, task_id: int) -> Optional[TaskDetail]:
        """
        Get full task details including trace steps.
        
        Args:
            task_id: Task ID
            
        Returns:
            TaskDetail or None
        """
        task = await self.task_repo.get_by_id(task_id)
        if not task:
            return None
        
        # Get trace steps
        steps_query = (
            select(TraceStep)
            .where(TraceStep.task_id == task_id)
            .order_by(TraceStep.step_order)
        )
        steps_result = await self.db.execute(steps_query)
        steps = steps_result.scalars().all()
        
        # Count alphas
        alphas_count = await self.alpha_repo.count_by({"task_id": task_id})
        
        return TaskDetail(
            id=task.id,
            task_name=task.task_name,
            region=task.region,
            universe=task.universe,
            dataset_strategy=task.dataset_strategy,
            target_datasets=task.target_datasets or [],
            status=task.status,
            daily_goal=task.daily_goal,
            # columns dropped in Phase 1d-2 — fields kept at 0 for API shape.
            progress_current=0,
            current_iteration=0,
            max_iterations=0,
            config=task.config or {},
            created_at=task.created_at,
            updated_at=task.updated_at,
            trace_steps=[self._to_trace_info(s) for s in steps],
            alphas_count=alphas_count,
            schedule=getattr(task, "schedule", None),
        )
    
    def _to_trace_info(self, step: TraceStep) -> TraceStepInfo:
        """Convert TraceStep to TraceStepInfo."""
        return TraceStepInfo(
            id=step.id,
            step_type=step.step_type,
            step_order=step.step_order,
            iteration=step.iteration,
            input_data=step.input_data or {},
            output_data=step.output_data or {},
            duration_ms=step.duration_ms,
            status=step.status,
            error_message=step.error_message,
            created_at=step.created_at,
        )
    
    # =========================================================================
    # Create Operations
    # =========================================================================

    
    # =========================================================================
    # Lifecycle Operations
    # =========================================================================
    

    async def intervene_task(
        self,
        task_id: int,
        action: str,
        parameters: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        """
        Intervene in a running task (pause, resume, stop, adjust).
        
        Args:
            task_id: Task ID
            action: Intervention action
            parameters: Optional parameters for ADJUST action
            
        Returns:
            Dict with result
            
        Raises:
            ValueError if task not found or invalid action
        """
        task = await self.task_repo.get_by_id(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")

        action = action.upper()

        # Post tier-system removal (2026-05-18): all running tasks are flat
        # sessions managed via /ops/flat-sessions/... The intervene path is
        # status-only and does NOT dispatch a worker, so PAUSE/RESUME is
        # routed there instead.
        if (task.schedule or "ONESHOT").upper() == "FLAT" and action in ("PAUSE", "RESUME"):
            raise ValueError(
                f"FLAT sessions use POST /ops/flat-sessions/{task_id}/resume "
                f"instead of /tasks/{task_id}/intervene which does not dispatch a worker."
            )

        if action == "PAUSE":
            if task.status != "RUNNING":
                raise ValueError("Can only pause running tasks")
            await self.task_repo.update_status(task_id, "PAUSED")
            await self.commit()
            return {"action": "paused", "new_status": "PAUSED"}
        
        elif action == "RESUME":
            if task.status != "PAUSED":
                raise ValueError("Can only resume paused tasks")
            await self.task_repo.update_status(task_id, "RUNNING")
            await self.commit()
            return {"action": "resumed", "new_status": "RUNNING"}
        
        elif action == "STOP":
            await self.task_repo.update_status(task_id, "STOPPED")
            await self.commit()
            return {"action": "stopped", "new_status": "STOPPED"}
        
        elif action == "SKIP":
            # Skip signal - logging only for now
            return {"action": "skip_signal_sent"}
        
        elif action == "ADJUST":
            if not parameters:
                raise ValueError("ADJUST action requires parameters")
            new_config = {**(task.config or {}), **parameters}
            await self.task_repo.update_by_id(task_id, {"config": new_config})
            await self.commit()
            return {"action": "adjusted", "new_config": new_config}
        
        else:
            raise ValueError(f"Unknown action: {action}")
    
    # =========================================================================
    # Trace Operations
    # =========================================================================
    
    async def get_task_trace(self, task_id: int) -> List[TraceStepInfo]:
        """
        Get all trace steps for a task.
        
        Args:
            task_id: Task ID
            
        Returns:
            List of TraceStepInfo
            
        Raises:
            ValueError if task not found
        """
        task = await self.task_repo.get_by_id(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")
        
        steps_query = (
            select(TraceStep)
            .where(TraceStep.task_id == task_id)
            .order_by(TraceStep.step_order)
        )
        result = await self.db.execute(steps_query)
        steps = result.scalars().all()
        
        return [self._to_trace_info(s) for s in steps]
    
    # =========================================================================
    # Run Operations
    # =========================================================================
    

    # Persistent Mining Service (flat sessions) fully retired:
    # _dispatch_session_worker / start_flat_session / resume_flat_session /
    # pause_flat_session (Phase 1c-delete) + _to_session_info / MiningSessionInfo /
    # SUPPORTED_REGIONS (Phase 1d-2, orphaned after flat-session removal).
