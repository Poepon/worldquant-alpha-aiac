"""DirectionBandit off-policy log table (Phase 1 R2/Q7, 2026-05-17).

WHY THIS TABLE EXISTS — same pattern as R1aAttributionLog (Phase 0 v1.6):

  R2/Q7 ContextualDirectionBandit state lives in
  ``mining_tasks.config["contextual_bandit_v1"]`` JSONB — that captures the
  CURRENT posterior. But for off-policy evaluation (IPS / SNIPS) and Phase 1.5
  Q&A on per-segment reward distribution, we need the **full event stream**
  (one row per arm-select / arm-update tuple, with observed reward).

  Writing this stream into the task.config JSONB would balloon it to MB-scale
  after a few weeks. So independent INSERT into a flat append-only table —
  same lesson as ``[[feedback_r1a_dedicated_log_table]]``.

DESIGN:
  - One row per (task_id, round_idx) — each round contributes 1 select + 1
    update (or just 1 select for the first round, no prior reward).
  - ``segment_id`` is the string-concat key ``f"{region}|{cat}|{pattern}"``
    so it's stable across deploys (MF-V1.2-4 — NOT Python hash()).
  - ``sampled_arm_probs`` JSONB optional — captures the Thompson posterior
    sample per arm at select time, useful for IPS reweighting in Phase 2+.
  - No Alembic migration in Phase 1 — table created via
    ``metadata.create_all()`` dev startup fallback. Phase 1.5 phase15-A will
    promote to typed columns.
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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from backend.database import SQLAlchemyBase


class DirectionBanditLog(SQLAlchemyBase):
    """One row per round's bandit select+update cycle."""

    __tablename__ = "direction_bandit_log"
    __table_args__ = (
        Index("ix_dbl_task_id", "task_id"),
        Index("ix_dbl_segment_id", "segment_id"),
        Index("ix_dbl_created_at", "created_at"),
    )

    id = Column(BigInteger, primary_key=True)
    task_id = Column(Integer, nullable=True, index=True)
    round_idx = Column(Integer, nullable=True)

    # Context dimensions (denormalized for query convenience — segment_id is
    # the canonical key, the three component cols are for analytics)
    segment_id = Column(String(128), nullable=False)
    region = Column(String(50), nullable=True)
    dataset_category = Column(String(100), nullable=True)
    failure_pattern = Column(String(32), nullable=True)

    # Bandit decision + outcome
    selected_arm = Column(String(64), nullable=False)
    observed_reward = Column(Float, nullable=True)  # NULL for first round (no prior reward)
    cold_start = Column(String(8), nullable=True)   # 'true'/'false' string for asyncpg simplicity

    # Thompson posterior debug (NULL when not captured to keep rows lean)
    sampled_arm_probs = Column(JSONB, nullable=True)

    # Bookkeeping
    bandit_version = Column(String(8), default="v1")
    write_error = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
