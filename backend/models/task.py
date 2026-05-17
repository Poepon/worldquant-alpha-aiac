"""
Task Models - Mining tasks and experiment tracking

Contains MiningTask, ExperimentRun, and TraceStep models.
"""

from sqlalchemy import Column, Integer, String, DateTime, Text, ForeignKey, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from backend.database import SQLAlchemyBase


class MiningTask(SQLAlchemyBase):
    """
    Mining Task - Represents a mining job configuration and state.
    
    A task can have multiple experiment runs and produces alphas.
    """
    __tablename__ = "mining_tasks"
    __table_args__ = {'extend_existing': True}
    
    id = Column(Integer, primary_key=True, index=True)
    task_name = Column(String(255), nullable=False)
    region = Column(String(50), nullable=False)
    universe = Column(String(100), nullable=False)
    
    dataset_strategy = Column(String(50), default="AUTO")
    target_datasets = Column(JSONB, default=[])
    agent_mode = Column(String(50), default="AUTONOMOUS")
    
    status = Column(String(50), default="PENDING")
    daily_goal = Column(Integer, default=4)
    progress_current = Column(Integer, default=0)
    
    # Evolution tracking
    current_iteration = Column(Integer, default=0)
    max_iterations = Column(Integer, default=10)
    
    config = Column(JSONB, default={})

    # V-19 Persistent Mining Service mode (2026-05-10)
    # mining_mode: 'DISCRETE' (legacy task — finishes when daily_goal met)
    #              'CONTINUOUS_CASCADE' (service singleton — runs T1→T2→T3 loop until paused)
    mining_mode = Column(String(30), default="DISCRETE", nullable=False)
    # Active cascade phase for CONTINUOUS_CASCADE tasks. T1/T2/T3/IDLE.
    cascade_phase = Column(String(10), nullable=True)
    # Cascade round counter (each round = T1→T2→T3 sequence). Persisted across PAUSE/RESUME.
    cascade_round_idx = Column(Integer, default=0, nullable=False)
    # Watchdog liveness signal — updated each time _incremental_save_alphas commits.
    last_alpha_persisted_at = Column(DateTime(timezone=True), nullable=True)

    # === Phase 1.5-A (Revision A 7a3f9e1c2b8d, plan v1.3 §1) ===
    # Dual-default per V1.2-B4:
    #   default=        → Python-side, fires for any ORM-INSERT (MiningTask(...)
    #                     constructor) — covers 21 test fixture files without
    #                     editing each one.
    #   server_default= → DB-side, fires for raw SQL INSERT + historical row
    #                     SELECT (column was added with backfill default).
    # MUST have BOTH — SQLAlchemy ORM-INSERT does NOT consult server_default
    # at INSERT time, only at refresh post-INSERT.
    schedule = Column(
        String(20),
        default="ONESHOT",
        server_default="ONESHOT",
        nullable=False,
    )
    starting_tier = Column(
        Integer,
        default=1,
        server_default="1",
        nullable=False,
    )
    # JSONB server_default uses sa.text("'X'::jsonb") form per MF-V1.4-1/2 —
    # asyncpg requires explicit ::jsonb cast or the column inherits text type.
    generation_strategy = Column(
        JSONB,
        default=lambda: ["llm"],
        server_default=text("'[\"llm\"]'::jsonb"),
        nullable=False,
    )

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), server_default=func.now())

    # Relationships
    trace_steps = relationship("TraceStep", back_populates="task", order_by="TraceStep.step_order")
    alphas = relationship("Alpha", back_populates="task")


class ExperimentRun(SQLAlchemyBase):
    """
    Experiment Run - A single execution of a mining task.
    
    Tracks configuration snapshots for reproducibility.
    """
    __tablename__ = "experiment_runs"
    __table_args__ = {'extend_existing': True}

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("mining_tasks.id"), nullable=False)

    status = Column(String(50), default="RUNNING")
    trigger_source = Column(String(50), default="API")
    celery_task_id = Column(String(100))

    config_snapshot = Column(JSONB, default={})
    prompt_version = Column(String(100))
    thresholds_version = Column(String(100))
    strategy_snapshot = Column(JSONB, default={})

    # === Phase 1.5-A (Revision A 7a3f9e1c2b8d, plan v1.3 §1) ===
    # Per-run mutable runtime state (current_tier / round_idx / progress /
    # iteration / last_persisted_at / dag). Phase 1.5-B dual-writes from
    # legacy MiningTask cols; Phase 2 R6 owns the `dag` sub-key. Dual-default
    # per V1.2-B4 (Python default=dict + DB server_default='{}'::jsonb).
    runtime_state = Column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
        nullable=False,
    )

    started_at = Column(DateTime, server_default=func.now())
    finished_at = Column(DateTime)
    error_message = Column(Text)

    # Relationships
    task = relationship("MiningTask")


class TraceStep(SQLAlchemyBase):
    """
    Trace Step - Records each step in the mining workflow.
    
    Provides observability and debugging capabilities.
    """
    __tablename__ = "trace_steps"
    __table_args__ = {'extend_existing': True}
    
    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("mining_tasks.id"), nullable=False)
    run_id = Column(Integer, ForeignKey("experiment_runs.id"), nullable=True)
    
    step_type = Column(String(50), nullable=False)
    step_order = Column(Integer, nullable=False)
    iteration = Column(Integer, default=1)
    input_data = Column(JSONB, default={})
    output_data = Column(JSONB, default={})
    
    duration_ms = Column(Integer, nullable=True)
    status = Column(String(50), default="RUNNING")
    error_message = Column(Text, nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    # Relationships
    task = relationship("MiningTask", back_populates="trace_steps")
    alpha = relationship("Alpha", back_populates="trace_step", uselist=False)


# Legacy model for backward compatibility
class MiningJob(SQLAlchemyBase):
    """Mining Job - Legacy model for job tracking."""
    __tablename__ = "mining_jobs"
    __table_args__ = {'extend_existing': True}
    
    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("mining_tasks.id"))
    iteration_idx = Column(Integer, default=0)
    status = Column(String(50), default="PENDING")
    start_time = Column(DateTime)
    end_time = Column(DateTime)
    logs = Column(Text)
    created_at = Column(DateTime, server_default=func.now())
