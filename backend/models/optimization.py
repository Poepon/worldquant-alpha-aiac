"""OptimizationRun model — one row per optimization cycle.

The cycle row is the single source of truth for GO/STOP-gate telemetry:
``n_winners / n_variants`` over a 14d window is the Stage A → Stage B
trigger. Writing this state to ``alphas.metrics`` JSONB would make the
aggregate query untestable + unindexable; the dedicated table costs
~20 rows/day and gives us O(1) range scans.

Source: ``docs/optimization_closure_plan_v1_2026-05-28.md`` §5.
"""
from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from backend.database import SQLAlchemyBase


class OptimizationRun(SQLAlchemyBase):
    """One optimization cycle (open → variants simulated → winners persisted
    → submit decisions → finish). Stage A is settings_sweep only."""

    __tablename__ = "optimization_runs"
    __table_args__ = (
        Index("ix_opt_runs_parent", "parent_alpha_id"),
        Index("ix_opt_runs_started", "cycle_started_at"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True)

    # The candidate alpha being optimized — points into ``alphas.id``.
    parent_alpha_id = Column(
        Integer, ForeignKey("alphas.id"), nullable=False, index=True
    )

    # ``"settings_sweep"`` (Stage A) / ``"composite"`` (Stage B) /
    # ``"ga"`` (Stage C). Used to slice the conversion-rate query per
    # generator family.
    generator_name = Column(String(64), nullable=False)

    # ``"beat"`` (Stage A only) / ``"pipeline_hook"`` (Stage C) /
    # ``"manual"`` (ops console).
    trigger_source = Column(String(32), nullable=False)

    # Cycle outcomes (updated by Persister/SubmitPolicy mid-cycle).
    n_variants = Column(Integer, nullable=False, default=0, server_default="0")
    n_winners = Column(Integer, nullable=False, default=0, server_default="0")
    n_submitted = Column(Integer, nullable=False, default=0, server_default="0")
    sim_budget_used = Column(Integer, nullable=False, default=0, server_default="0")
    sim_budget_granted = Column(Integer, nullable=False)

    # Lifecycle timestamps.
    cycle_started_at = Column(
        DateTime, nullable=False, server_default=func.now()
    )
    cycle_finished_at = Column(DateTime, nullable=True)

    # Non-NULL = cycle aborted; Persister/Simulator/SubmitPolicy soft-fail
    # message ends up here.
    error = Column(Text, nullable=True)

    # Generator-private blob (e.g. settings_sweep stores the cell grid
    # snapshot it actually picked from; Stage B's composite could store
    # the rewrite count). Never indexed; read-mostly for forensic debug.
    cycle_metadata = Column(JSONB, nullable=True, default=dict, server_default="{}")
