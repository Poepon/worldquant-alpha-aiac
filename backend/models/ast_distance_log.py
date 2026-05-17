"""AST distance log table (Phase 1 R3/Q8, 2026-05-17).

Per R1a v1.6 + R2/Q7 v1 lessons (see ``[[feedback_r1a_dedicated_log_table]]``)
the diversity-tracker computes ast_distance for every code-gen candidate but
~95% of those alphas never INSERT to the ``alphas`` table (FAIL / OPTIMIZE
buckets drop without persistence). Writing to ``alpha.metrics`` would lose
the signal. This dedicated table captures every measurement so Phase 2+
analytics (family-cap R10, diversity-saturation report, hypothesis-pillar
balance) can join on it.

DESIGN:
  - One row per (task_id, expression_hash) — duplicate generation within
    a task can be dedup-checked via expression_hash
  - min/mean/max distance vs the top-K most recent attempts summarizes
    the candidate's novelty without committing per-attempt comparison rows
  - nearest_neighbor_hash lets future analytics identify family clusters
  - No Alembic in Phase 1 — relies on dev startup ``metadata.create_all()``
"""
from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    Text,
    DateTime,
    Index,
    BigInteger,
)
from sqlalchemy.sql import func

from backend.database import SQLAlchemyBase


class AstDistanceLog(SQLAlchemyBase):
    """One row per ast_distance computation in diversity_tracker hot path."""

    __tablename__ = "ast_distance_log"
    __table_args__ = (
        Index("ix_adl_task_id", "task_id"),
        Index("ix_adl_created_at", "created_at"),
        Index("ix_adl_expression_hash", "expression_hash"),
    )

    id = Column(BigInteger, primary_key=True)
    task_id = Column(Integer, nullable=True, index=True)
    round_idx = Column(Integer, nullable=True)

    expression = Column(Text, nullable=False)
    expression_hash = Column(String(64), nullable=True)
    skeleton = Column(Text, nullable=True)

    ast_distance_min = Column(Float, nullable=True)
    ast_distance_mean = Column(Float, nullable=True)
    ast_distance_max = Column(Float, nullable=True)
    nearest_neighbor_hash = Column(String(64), nullable=True)
    history_window = Column(Integer, nullable=True)  # K (compared count)

    tracker_version = Column(String(8), default="v1")
    write_error = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
