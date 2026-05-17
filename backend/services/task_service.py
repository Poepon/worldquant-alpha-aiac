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
    agent_mode: str = "AUTONOMOUS"
    daily_goal: int = 4
    config: Dict[str, Any] = None
    
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
    agent_mode: str
    status: str
    daily_goal: int
    progress_current: int
    current_iteration: int
    max_iterations: int
    created_at: datetime
    updated_at: Optional[datetime]


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
    agent_mode: str
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


@dataclass
class MiningSessionInfo:
    """V-19 persistent mining service session info."""
    task_id: int
    task_name: str
    region: str
    universe: str
    status: str           # RUNNING / PAUSED
    mining_mode: str      # always CONTINUOUS_CASCADE
    cascade_phase: Optional[str]   # T1 / T2 / T3 / IDLE
    cascade_round_idx: int
    progress_current: int
    last_alpha_persisted_at: Optional[datetime]
    started_at: Optional[datetime]   # task.created_at
    paused_at: Optional[datetime]    # task.updated_at when status moved to PAUSED


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
            agent_mode=task.agent_mode,
            status=task.status,
            daily_goal=task.daily_goal,
            progress_current=task.progress_current,
            current_iteration=task.current_iteration,
            max_iterations=task.max_iterations,
            created_at=task.created_at,
            updated_at=task.updated_at,
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
            agent_mode=task.agent_mode,
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
    
    # PR2 — agent_mode → factor_tier mapping shared across router and beat task.
    AGENT_MODE_TO_TIER = {
        "AUTONOMOUS": 1,        # legacy mode behaves as T1 when tier system on
        "AUTONOMOUS_TIER1": 1,
        "AUTONOMOUS_TIER2": 2,
        "AUTONOMOUS_TIER3": 3,
        "INTERACTIVE": None,    # tier-agnostic
    }

    @classmethod
    def factor_tier_from_mode(cls, agent_mode: str) -> Optional[int]:
        """Resolve agent_mode → factor_tier. Returns None for INTERACTIVE / unknown."""
        return cls.AGENT_MODE_TO_TIER.get(agent_mode)

    async def _validate_tier_eligibility(self, data: "TaskCreateData") -> None:
        """PR2: gate tier-mode tasks on feature flag + prerequisites.

        - ENABLE_FACTOR_TIERING=False → reject all AUTONOMOUS_TIER* modes.
        - T2/T3 → require MIN_TIER_SEED_COUNT PASS alphas in the predecessor tier
          for the target region (dataset filter is too narrow this early —
          users often haven't picked dataset yet for AUTO strategy).
        - T1 → require at least one DataField row for the region (proxy for
          "dataset has been synced from BRAIN"); skip if dataset_strategy=AUTO
          and no specific datasets pinned.

        Raises ValueError with a user-facing message; router maps to HTTP 400.
        """
        from backend.config import settings

        tier = self.factor_tier_from_mode(data.agent_mode)
        if data.agent_mode and data.agent_mode.startswith("AUTONOMOUS_TIER"):
            if not getattr(settings, "ENABLE_FACTOR_TIERING", True):
                raise ValueError(
                    "tier system is disabled (ENABLE_FACTOR_TIERING=False); "
                    "use agent_mode='AUTONOMOUS' instead"
                )

        if tier in (2, 3):
            from backend.models import Alpha
            from backend.agents.graph.tier_thresholds import get_min_seed_count

            prior_tier = tier - 1
            min_required = get_min_seed_count()
            count_q = (
                select(func.count(Alpha.id))
                .where(Alpha.factor_tier == prior_tier)
                .where(Alpha.quality_status == "PASS")
                .where(Alpha.region == data.region)
            )
            seed_count = (await self.db.execute(count_q)).scalar() or 0
            if seed_count < min_required:
                raise ValueError(
                    f"T{tier} task needs at least {min_required} PASS alphas "
                    f"at T{prior_tier} for region={data.region}; found {seed_count}. "
                    f"Run a T{prior_tier} task first to accumulate seeds."
                )

        if tier == 1 and data.dataset_strategy != "AUTO":
            # V-22.6.4-followup (2026-05-12): DataField.dataset_id is an INTEGER
            # FK to datasets.id, but data.target_datasets is a list of string
            # dataset_id values (e.g. ["fundamental6"]). The old in_(strings)
            # filter raised an UndefinedColumnError (mapped to 500). Join
            # through DatasetMetadata.dataset_id (String) instead.
            from backend.models import DataField, DatasetMetadata

            if data.target_datasets:
                ds_count_q = (
                    select(func.count(DataField.id))
                    .join(DatasetMetadata, DataField.dataset_id == DatasetMetadata.id)
                    .where(DatasetMetadata.dataset_id.in_(data.target_datasets))
                )
                if (await self.db.execute(ds_count_q)).scalar() == 0:
                    raise ValueError(
                        f"none of {data.target_datasets} have synced DataField rows; "
                        f"run sync_datasets task first"
                    )

    async def create_task(self, data: TaskCreateData) -> TaskSummary:
        """
        Create a new mining task.

        Args:
            data: Task creation data

        Returns:
            Created TaskSummary

        Raises:
            ValueError: when tier-mode prerequisites aren't met (mapped to HTTP
                400 by the router). Examples: tier system disabled, T2/T3
                without enough prior-tier PASS seeds, T1 with unsynced dataset.
        """
        await self._validate_tier_eligibility(data)

        # Plan v5+ §F-5 50/50 A/B variant assignment. Pre-2026-05-06 the
        # config slot existed but no code consumed CANDIDATE — tasks always
        # used LEVEL. Now: if CANDIDATE > LEVEL, every new task gets a
        # random.choice([LEVEL, CANDIDATE]) injected into config[
        # "hypothesis_centric_variant"]. mining_tasks.run_mining_task reads
        # this per-task value at execution time. Caller-supplied
        # hypothesis_centric_variant in data.config takes precedence (lets
        # ad-hoc scripts pin a variant for targeted runs).
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

        # Phase 1.5-B (Revision B 3b1c4e5d6a78): dual-write schedule +
        # starting_tier derived from agent_mode (legacy still SoT until
        # Phase 1.5-C cut-over). Python `default=` on the model also
        # backfills schedule='ONESHOT'/starting_tier=1, but dual-write
        # makes intent explicit + supports Phase 1.5-C reading them.
        # Mapping rules mirror backfill SQL in alembic Revision B.
        schedule = "ONESHOT"  # create_task only creates DISCRETE; cascade
                              # tasks go via _start_cascade_session below
        if data.agent_mode == "AUTONOMOUS_TIER2":
            starting_tier = 2
        elif data.agent_mode == "AUTONOMOUS_TIER3":
            starting_tier = 3
        else:
            starting_tier = 1

        task = MiningTask(
            task_name=data.name,
            region=data.region,
            universe=data.universe,
            dataset_strategy=data.dataset_strategy,
            target_datasets=data.target_datasets,
            agent_mode=data.agent_mode,
            daily_goal=data.daily_goal,
            config=config,
            status="PENDING",
            # Phase 1.5-B dual-write
            schedule=schedule,
            starting_tier=starting_tier,
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
    # V-19 Persistent Mining Service
    # =========================================================================
    # singleton-per-region semantics enforced at schema level by partial index
    # ix_mining_tasks_active_cascade_per_region (mining_mode='CONTINUOUS_CASCADE'
    # AND status IN ('RUNNING','PAUSED')). The service-layer methods below
    # navigate that constraint, with idempotent start.

    SUPPORTED_REGIONS = ("USA", "CHN", "EUR", "ASI", "GLB")

    async def get_active_session(self, region: str) -> Optional[MiningSessionInfo]:
        """Return the active CONTINUOUS_CASCADE session for `region`, or None.

        "active" = status IN ('RUNNING', 'PAUSED'). The unique partial index
        guarantees at most 1 row matches.
        """
        q = (
            select(MiningTask)
            .where(MiningTask.region == region)
            .where(MiningTask.mining_mode == "CONTINUOUS_CASCADE")
            .where(MiningTask.status.in_(("RUNNING", "PAUSED")))
            .limit(1)
        )
        result = await self.db.execute(q)
        task = result.scalar_one_or_none()
        return self._to_session_info(task) if task else None

    async def list_active_sessions(self) -> List[MiningSessionInfo]:
        """Return active CONTINUOUS_CASCADE sessions across all regions."""
        q = (
            select(MiningTask)
            .where(MiningTask.mining_mode == "CONTINUOUS_CASCADE")
            .where(MiningTask.status.in_(("RUNNING", "PAUSED")))
            .order_by(MiningTask.region)
        )
        result = await self.db.execute(q)
        tasks = result.scalars().all()
        return [self._to_session_info(t) for t in tasks]

    async def start_session(
        self,
        region: str = "USA",
        universe: str = "TOP3000",
    ) -> MiningSessionInfo:
        """Start (or resume) the singleton CONTINUOUS_CASCADE session for region.

        Idempotent — if a session already exists for the region:
          - status=RUNNING: returns it as-is (no-op).
          - status=PAUSED: flips to RUNNING and dispatches a fresh celery
            worker. cascade_phase / cascade_round_idx are preserved so the
            worker resumes mid-cascade.

        If no session exists, creates a new CONTINUOUS_CASCADE task with
        defaults (daily_goal=0 = unlimited, max_iterations=999999) and
        dispatches the celery worker.

        Raises ValueError on unsupported region.
        """
        if region not in self.SUPPORTED_REGIONS:
            raise ValueError(
                f"region={region!r} not supported; choose one of {self.SUPPORTED_REGIONS}"
            )

        existing = await self.get_active_session(region)
        if existing:
            if existing.status == "RUNNING":
                logger.info(
                    f"[start_session] region={region} already RUNNING "
                    f"(task_id={existing.task_id}); idempotent no-op"
                )
                return existing
            # PAUSED → RUNNING + dispatch fresh worker
            await self.task_repo.update_status(existing.task_id, "RUNNING")
            await self.commit()
            await self._dispatch_session_worker(existing.task_id)
            refreshed = await self.get_active_session(region)
            assert refreshed is not None
            logger.info(
                f"[start_session] region={region} resumed PAUSED→RUNNING "
                f"(task_id={refreshed.task_id} phase={refreshed.cascade_phase} "
                f"round_idx={refreshed.cascade_round_idx})"
            )
            return refreshed

        # No existing session — create a new one. The unique partial index
        # races against concurrent start calls; on conflict we fall back to
        # reading the winner.
        from sqlalchemy.exc import IntegrityError

        task = MiningTask(
            task_name=f"mining-session-{region}",
            region=region,
            universe=universe,
            dataset_strategy="AUTO",
            target_datasets=[],
            agent_mode="AUTONOMOUS",  # cascade ignores this — service self-manages tier
            daily_goal=0,                # 0 = unlimited (CONTINUOUS_CASCADE only stops on pause)
            max_iterations=999999,
            config={},
            mining_mode="CONTINUOUS_CASCADE",
            cascade_phase="T1",
            cascade_round_idx=0,
            status="RUNNING",
            # Phase 1.5-B dual-write — cascade always starts at T1
            schedule="CASCADE",
            starting_tier=1,
        )
        try:
            created = await self.task_repo.create(task)
            await self.commit()
        except IntegrityError:
            await self.db.rollback()
            # A concurrent caller won the race — return whatever exists.
            winner = await self.get_active_session(region)
            if winner is None:
                raise RuntimeError(
                    "race against unique partial index but no winner found"
                )
            logger.info(
                f"[start_session] region={region} lost race; returning winner "
                f"task_id={winner.task_id}"
            )
            return winner

        await self._dispatch_session_worker(created.id)
        logger.info(
            f"[start_session] region={region} created new session task_id={created.id}"
        )
        info = await self.get_active_session(region)
        assert info is not None
        return info

    async def stop_session(self, task_id: int) -> MiningSessionInfo:
        """Pause the session. Worker detects PAUSED at the next round boundary
        and exits gracefully (the IX-5 alpha-level dedup on resume covers any
        in-flight alphas). Cascade phase / round idx are preserved.
        """
        task = await self.task_repo.get_by_id(task_id)
        if not task:
            raise ValueError(f"task_id={task_id} not found")
        if task.mining_mode != "CONTINUOUS_CASCADE":
            raise ValueError(
                f"task_id={task_id} is not CONTINUOUS_CASCADE "
                f"(mining_mode={task.mining_mode}); use intervene_task PAUSE instead"
            )
        if task.status not in ("RUNNING", "PAUSED"):
            raise ValueError(
                f"task_id={task_id} cannot be stopped from status={task.status}"
            )
        if task.status == "RUNNING":
            await self.task_repo.update_status(task_id, "PAUSED")
            await self.commit()
        info = self._to_session_info(
            await self.task_repo.get_by_id(task_id)
        )
        logger.info(
            f"[stop_session] task_id={task_id} region={task.region} → PAUSED "
            f"(phase={info.cascade_phase} round_idx={info.cascade_round_idx})"
        )
        return info

    async def resume_session(self, task_id: int) -> MiningSessionInfo:
        """Explicit RESUME from PAUSED. start_session(region) auto-resumes too;
        this method is exposed for API parity (POST /mining-session/resume)."""
        task = await self.task_repo.get_by_id(task_id)
        if not task:
            raise ValueError(f"task_id={task_id} not found")
        if task.mining_mode != "CONTINUOUS_CASCADE":
            raise ValueError(f"task_id={task_id} is not CONTINUOUS_CASCADE")
        if task.status == "RUNNING":
            return self._to_session_info(task)  # already running
        if task.status != "PAUSED":
            raise ValueError(
                f"task_id={task_id} cannot resume from status={task.status}"
            )
        await self.task_repo.update_status(task_id, "RUNNING")
        await self.commit()
        await self._dispatch_session_worker(task_id)
        info = self._to_session_info(await self.task_repo.get_by_id(task_id))
        logger.info(
            f"[resume_session] task_id={task_id} region={task.region} PAUSED→RUNNING"
        )
        return info

    async def _dispatch_session_worker(self, task_id: int) -> str:
        """Create a new ExperimentRun + dispatch celery worker for the session.

        Each pause/resume cycle gets its own ExperimentRun for traceability.
        """
        task = await self.task_repo.get_by_id(task_id)
        run = ExperimentRun(
            task_id=task_id,
            status="RUNNING",
            trigger_source="MINING_SESSION",
            celery_task_id=None,
            config_snapshot={
                "task": {
                    "region": task.region,
                    "universe": task.universe,
                    "mining_mode": task.mining_mode,
                    "cascade_phase": task.cascade_phase,
                    "cascade_round_idx": task.cascade_round_idx,
                },
            },
            strategy_snapshot={},
        )
        created_run = await self.run_repo.create(run)
        await self.commit()

        from backend.tasks import run_mining_task
        celery_task = run_mining_task.delay(task_id, created_run.id)
        created_run.celery_task_id = celery_task.id
        await self.commit()
        return celery_task.id

    def _to_session_info(self, task: MiningTask) -> MiningSessionInfo:
        return MiningSessionInfo(
            task_id=task.id,
            task_name=task.task_name,
            region=task.region,
            universe=task.universe,
            status=task.status,
            mining_mode=task.mining_mode,
            cascade_phase=task.cascade_phase,
            cascade_round_idx=task.cascade_round_idx,
            progress_current=task.progress_current,
            last_alpha_persisted_at=task.last_alpha_persisted_at,
            started_at=task.created_at,
            paused_at=task.updated_at if task.status == "PAUSED" else None,
        )
