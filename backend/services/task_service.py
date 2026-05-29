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
        """
        Start a mining task.
        
        Args:
            task_id: Task ID
            
        Returns:
            Dict with run_id and celery_task_id
            
        Raises:
            ValueError if task not found or invalid status
        """
        task = await self.task_repo.get_by_id(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")
        
        valid_start_statuses = ["PENDING", "PAUSED", "STOPPED", "FAILED", "COMPLETED"]
        if task.status not in valid_start_statuses:
            raise ValueError(f"Cannot start task in {task.status} status")
        
        # Update status
        await self.task_repo.update_status(task_id, "RUNNING")
        
        # Create experiment run
        run = ExperimentRun(
            task_id=task_id,
            status="RUNNING",
            trigger_source="API",
            celery_task_id=None,
            config_snapshot={
                "task": {
                    "region": task.region,
                    "universe": task.universe,
                    "dataset_strategy": task.dataset_strategy,
                    "target_datasets": task.target_datasets,
                    "daily_goal": task.daily_goal,
                    "config": task.config,
                },
            },
            strategy_snapshot={},
        )
        created_run = await self.run_repo.create(run)
        await self.commit()
        
        # Trigger Celery task
        from backend.tasks import run_mining_task
        celery_task = run_mining_task.delay(task_id, created_run.id)
        
        # Update run with celery task ID
        created_run.celery_task_id = celery_task.id
        await self.commit()
        
        return {
            "task_id": task_id,
            "run_id": created_run.id,
            "celery_task_id": celery_task.id,
        }
    
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

    async def _dispatch_session_worker(
        self,
        task_id: int,
        *,
        inherit_runtime_state: bool = False,
    ) -> str:
        """Create a new ExperimentRun + dispatch celery worker for the session.

        Each pause/resume cycle gets its own ExperimentRun for traceability.

        flat-F1 v1.5 Q1 Variant V2: when ``inherit_runtime_state=True`` the new
        run is seeded with the previous (latest) run's ``runtime_state`` so
        FLAT_CONTINUOUS sessions preserve ``flat_cursor`` (and any future
        per-session keys) across pause-resume. Cascade callers keep the default
        ``False`` — each cascade ExperimentRun starts with empty runtime_state,
        unchanged from pre-v1.5 behavior.
        """
        task = await self.task_repo.get_by_id(task_id)

        prev_state: Dict[str, Any] = {}
        if inherit_runtime_state:
            prev_run = await self.run_repo.get_latest_by_task(task_id)
            if prev_run is not None and isinstance(prev_run.runtime_state, dict):
                prev_state = dict(prev_run.runtime_state)

        run = ExperimentRun(
            task_id=task_id,
            status="RUNNING",
            trigger_source="MINING_SESSION",
            celery_task_id=None,
            config_snapshot={
                "task": {
                    "region": task.region,
                    "universe": task.universe,
                    "schedule": task.schedule,
                },
            },
            strategy_snapshot={},
            runtime_state=prev_state,
        )
        created_run = await self.run_repo.create(run)
        await self.commit()

        from backend.tasks import run_mining_task
        celery_task = run_mining_task.delay(task_id, created_run.id)
        created_run.celery_task_id = celery_task.id
        await self.commit()
        return celery_task.id

    # =========================================================================
    # FLAT_CONTINUOUS session lifecycle (flat-F1 v1.5)
    # =========================================================================
    # Restored 2026-05-18 — PR3e accidentally deleted these along with the
    # cascade-only methods (start_session / stop_session / resume_session etc).
    # The ops router (POST /api/v1/ops/start-flat-session and
    # POST /api/v1/ops/flat-sessions/{id}/resume) still calls them. Without
    # these the only production mining mode (FLAT_CONTINUOUS) is dead.
    # NOTE: do NOT pass cascade_phase / cascade_round_idx to MiningTask(...) —
    # those columns were dropped in PR3b (migration c3f9a7d2e4b8).

    async def start_flat_session(
        self,
        region: str = "USA",
        universe: str = "TOP3000",
        datasets: Optional[List[str]] = None,
        delay: int = 1,
        launched_by: str = "manual",
    ) -> "MiningSessionInfo":
        """Create a new FLAT_CONTINUOUS task and dispatch its worker.

        Unlike the retired CONTINUOUS_CASCADE.start_session this is NOT
        singleton-per-region: multiple FLAT tasks may co-exist (different
        dataset / hypothesis sets). Caller (ops endpoint) is expected to
        gate on ``settings.ENABLE_FLAT_CONTINUOUS``.

        delay: BRAIN simulation delay (0 or 1). 1 = the established path.
        delay=0 stamps task.config['delay']=0 so the worker mines the
        delay-0 field roster and sims at delay-0 (orthogonal axis ②/B);
        the delay-0 datafield cells must already be synced for the chosen
        datasets/universe (sync_fields_from_brain delay=0).
        """
        if region not in self.SUPPORTED_REGIONS:
            raise ValueError(
                f"region={region!r} not supported; choose one of {self.SUPPORTED_REGIONS}"
            )
        if delay not in (0, 1):
            raise ValueError(f"delay={delay!r} not supported; choose 0 or 1")

        # Pin hypothesis_centric level into the task config at CREATION time
        # (2026-05-22 root-cause fix). _get_active_level falls back to
        # settings.HYPOTHESIS_CENTRIC_LEVEL only when this key is ABSENT — and
        # that setting lives in .env (not a refreshable flag), so a Celery
        # worker started before .env was bumped runs at level 0 forever and
        # every FLAT alpha persists with hypothesis_id=NULL (verified: explicit
        # variant=2 tasks link at 98%, absent-variant FLAT at ~5-13%). Stamping
        # it here in the FastAPI/caller process (which has the correct value)
        # makes the worker read the pinned level from config, immune to its own
        # stale .env. assign_variant (ONESHOT A/B) no longer fires once
        # LEVEL==CANDIDATE, so FLAT must pin explicitly.
        from backend.config import settings as _hge
        _level = int(getattr(_hge, "HYPOTHESIS_CENTRIC_LEVEL", 0) or 0)

        # delay-1 omits the key so existing-session config stays byte-identical
        # (_task_delay falls back to 1 when absent); only delay-0 stamps it.
        _config = {"flat_cursor": 0, "hypothesis_centric_variant": _level}
        if delay != 1:
            _config["delay"] = int(delay)
        # Orchestrator Sub-phase 1 (Q6 DECIDED 2026-05-29):标记 task 是 manual
        # 启的还是 orchestrator 启的。orchestrator 让位决策只动自己启的 task,
        # user 手动启的 task 不被自动化干预。default "manual" 保留向后兼容。
        _config["launched_by"] = launched_by

        task = MiningTask(
            task_name=f"flat-session-{region}-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}",
            region=region,
            universe=universe,
            dataset_strategy="MANUAL" if datasets else "AUTO",
            target_datasets=list(datasets or []),
            daily_goal=0,                # 0 = unlimited within FLAT_CONTINUOUS_MAX_ITERATIONS
            max_iterations=999999,
            config=_config,
            status="RUNNING",
            schedule="FLAT",
        )
        created = await self.task_repo.create(task)
        await self.commit()
        await self._dispatch_session_worker(created.id, inherit_runtime_state=False)
        logger.info(
            f"[start_flat_session] region={region} task_id={created.id} "
            f"datasets={len(datasets or [])} dispatched"
        )
        return self._to_session_info(await self.task_repo.get_by_id(created.id))

    async def resume_flat_session(self, task_id: int) -> "MiningSessionInfo":
        """Resume a paused FLAT session, preserving runtime_state['flat_cursor']."""
        task = await self.task_repo.get_by_id(task_id)
        if not task:
            raise ValueError(f"task_id={task_id} not found")
        if (task.schedule or "").upper() != "FLAT":
            raise ValueError(
                f"task_id={task_id} is not a FLAT session (schedule={task.schedule})"
            )
        if task.status == "RUNNING":
            return self._to_session_info(task)
        if task.status != "PAUSED":
            raise ValueError(
                f"task_id={task_id} cannot resume from status={task.status}"
            )
        await self.task_repo.update_status(task_id, "RUNNING")
        await self.commit()
        await self._dispatch_session_worker(task_id, inherit_runtime_state=True)
        logger.info(
            f"[resume_flat_session] task_id={task_id} region={task.region} "
            f"PAUSED→RUNNING (runtime_state inherited)"
        )
        return self._to_session_info(await self.task_repo.get_by_id(task_id))

    async def pause_flat_session(self, task_id: int) -> "MiningSessionInfo":
        """Pause a RUNNING FLAT session by setting status→PAUSED.

        No worker dispatch / kill needed: the running flat worker self-checks
        task.status at every round boundary (CASCADE_PAUSE_POLL_SEC) and exits
        cleanly on PAUSED — identical mechanism to quota_guard_pause_at_threshold
        (session_watchdog.py:439). flat_cursor is preserved in task.config so a
        later resume_flat_session continues where it left off.
        """
        task = await self.task_repo.get_by_id(task_id)
        if not task:
            raise ValueError(f"task_id={task_id} not found")
        if (task.schedule or "").upper() != "FLAT":
            raise ValueError(
                f"task_id={task_id} is not a FLAT session (schedule={task.schedule})"
            )
        if task.status == "PAUSED":
            return self._to_session_info(task)
        if task.status != "RUNNING":
            raise ValueError(
                f"task_id={task_id} cannot pause from status={task.status}"
            )
        await self.task_repo.update_status(task_id, "PAUSED")
        await self.commit()
        logger.info(
            f"[pause_flat_session] task_id={task_id} region={task.region} "
            f"RUNNING→PAUSED (worker exits at next round boundary)"
        )
        return self._to_session_info(await self.task_repo.get_by_id(task_id))

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
