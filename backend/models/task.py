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

    status = Column(String(50), default="PENDING")
    daily_goal = Column(Integer, default=4)
    progress_current = Column(Integer, default=0)

    # Evolution tracking
    current_iteration = Column(Integer, default=0)
    max_iterations = Column(Integer, default=10)

    config = Column(JSONB, default={})

    # Watchdog liveness signal — updated each time _incremental_save_alphas commits.
    last_alpha_persisted_at = Column(DateTime(timezone=True), nullable=True)

    # Phase 1.5-A scheduling field. Post tier-removal, ``schedule`` is the sole
    # authoritative driver for cascade vs flat (legacy ``agent_mode`` /
    # ``starting_tier`` / ``mining_mode`` columns dropped). Dual-default per
    # V1.2-B4 (Python default fires for ORM constructor INSERTs; server_default
    # fires for raw SQL INSERT + historical-row SELECT).
    schedule = Column(
        String(20),
        default="ONESHOT",
        server_default="ONESHOT",
        nullable=False,
    )
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
