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
from sqlalchemy import select, update, func

from backend.services.base import BaseService
from backend.repositories.task_repository import TaskRepository, ExperimentRunRepository
from backend.repositories.alpha_repository import AlphaRepository
from backend.models import MiningTask, TraceStep, Alpha, ExperimentRun

logger = logging.getLogger("services.task")


@dataclass
class TaskCreateData:
    """Data for creating a new task."""
    name: str
    region: str = "USA"
    universe: str = "TOP3000"
    dataset_strategy: str = "AUTO"
    target_datasets: List[str] = None
    daily_goal: int = 4
    config: Dict[str, Any] = None
    schedule: Optional[str] = None       # ONESHOT | FLAT

    def __post_init__(self):
        if self.target_datasets is None:
            self.target_datasets = []
        if self.config is None:
            self.config = {}


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


@dataclass
class MiningSessionInfo:
    """Mining session DTO returned by start_flat_session / resume_flat_session."""
    task_id: int
    task_name: str
    region: str
    universe: str
    status: str           # RUNNING / PAUSED
    progress_current: int
    last_alpha_persisted_at: Optional[datetime]
    started_at: Optional[datetime]   # task.created_at
    paused_at: Optional[datetime]    # task.updated_at when status moved to PAUSED
    schedule: Optional[str] = None   # ONESHOT / FLAT


@dataclass
class ExperimentRunInfo:
    """Experiment run information."""
    id: int
    task_id: int
    status: str
    trigger_source: Optional[str]
    celery_task_id: Optional[str]
    started_at: datetime
    finished_at: Optional[datetime]
    error_message: Optional[str]


class TaskService(BaseService):
    """
    Service for task-related operations.
    
    Provides a clean interface for task management,
    abstracting database operations from routers.
    """
    
    def __init__(self, db: AsyncSession):
        super().__init__(db)
        self.task_repo = TaskRepository(db)
        self.run_repo = ExperimentRunRepository(db)
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
            progress_current=task.progress_current,
            current_iteration=task.current_iteration,
            max_iterations=task.max_iterations,
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
            progress_current=task.progress_current,
            current_iteration=task.current_iteration,
            max_iterations=task.max_iterations,
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

    async def create_task(self, data: TaskCreateData) -> TaskSummary:
        """Create a new mining task (post tier-system removal, 2026-05-18).

        Args:
            data: Task creation data

        Returns:
            Created TaskSummary
        """
        # Plan v5+ §F-5 50/50 A/B variant assignment.
        config = dict(data.config or {})
        if "hypothesis_centric_variant" not in config:
            from backend.config import settings as _hge
            level = int(_hge.HYPOTHESIS_CENTRIC_LEVEL or 0)
            candidate = int(_hge.HYPOTHESIS_CENTRIC_CANDIDATE or 0)
            if candidate > level:
                import random
                assigned = random.choice([level, candidate])
                config["hypothesis_centric_variant"] = assigned
                logger.info(
                    f"[task_service] F-5 A/B variant assigned: {assigned} "
                    f"(level={level} candidate={candidate})"
                )

        schedule = (data.schedule or "ONESHOT").upper()

        task = MiningTask(
            task_name=data.name,
            region=data.region,
            universe=data.universe,
            dataset_strategy=data.dataset_strategy,
            target_datasets=data.target_datasets,
            daily_goal=data.daily_goal,
            config=config,
            status="PENDING",
            schedule=schedule,
        )

        created = await self.task_repo.create(task)
        await self.commit()

        return self._to_summary(created)
    
    # =========================================================================
    # Lifecycle Operations
    # =========================================================================
    
    async def start_task(self, task_id: int) -> Dict[str, Any]:
        """ONESHOT task dispatch was retired in Phase 1c-delete.

        The FLAT/ONESHOT ``run_mining_task`` Celery task no longer exists — all
        mining now runs through the resident HG/S/E pool (fed by the scheduler
        beat). Manual one-off task starts will be re-homed onto the pool in
        Phase 1d (runs.py rework); until then this raises a clear error rather
        than dispatching a deleted Celery task.
        """
        raise ValueError(
            "ONESHOT task start was retired in Phase 1c-delete; mining now runs "
            "through the HG/S/E pool (run_mining_task no longer exists)."
        )

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
    
    async def list_task_runs(self, task_id: int) -> List[ExperimentRunInfo]:
        """
        Get all experiment runs for a task.
        
        Args:
            task_id: Task ID
            
        Returns:
            List of ExperimentRunInfo
            
        Raises:
            ValueError if task not found
        """
        task = await self.task_repo.get_by_id(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")
        
        result = await self.run_repo.get_by_task_id(task_id)
        
        return [
            ExperimentRunInfo(
                id=run.id,
                task_id=run.task_id,
                status=run.status,
                trigger_source=run.trigger_source,
                celery_task_id=run.celery_task_id,
                started_at=run.started_at,
                finished_at=run.finished_at,
                error_message=run.error_message,
            )
            for run in result.items
        ]

    # =========================================================================
    # Persistent Mining Service (flat sessions, post tier-system removal)
    # =========================================================================

    SUPPORTED_REGIONS = ("USA", "CHN", "EUR", "ASI", "GLB")

    # _dispatch_session_worker / start_flat_session / resume_flat_session /
    # pause_flat_session were retired in Phase 1c-delete (FLAT path removed;
    # mining runs through the HG/S/E pool). _to_session_info is kept as the
    # MiningSessionInfo projector (still referenced by surviving serializers).

    def _to_session_info(self, task: MiningTask) -> MiningSessionInfo:
        return MiningSessionInfo(
            task_id=task.id,
            task_name=task.task_name,
            region=task.region,
            universe=task.universe,
            status=task.status,
            progress_current=task.progress_current,
            last_alpha_persisted_at=task.last_alpha_persisted_at,
            started_at=task.created_at,
            paused_at=task.updated_at if task.status == "PAUSED" else None,
            schedule=getattr(task, "schedule", None),
        )
