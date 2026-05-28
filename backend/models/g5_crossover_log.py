"""G5 trajectory crossover log table (Phase A, 2026-05-19).

Per [[feedback_r1a_dedicated_log_table]] each cross-cutting telemetry signal
gets its own table — keeps cross-task analytics clean and survives the
``alphas`` filter (many G5 LLM calls happen at round boundary where no PASS
alpha is produced, so writing to alpha.metrics would be lossy).

One row per llm_crossover_alpha invocation. parent_a_id / parent_b_id FK
back to alphas.id; offspring_expressions is the JSON list of LLM-generated
combinations (each {expression, combination_strategy, rationale}).
outcome_alpha_ids gets back-filled by the next round's persistence path
when an offspring INSERTs as a fresh alpha — closing the parent→offspring
attribution loop.
"""
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from backend.database import SQLAlchemyBase


class G5CrossoverLog(SQLAlchemyBase):
    """One row per G5 LLM crossover call (flag-gated)."""

    __tablename__ = "g5_crossover_log"
    __table_args__ = (
        Index("ix_g5_task_id", "task_id"),
        Index("ix_g5_created_at", "created_at"),
        Index("ix_g5_parent_a", "parent_a_alpha_id"),
        Index("ix_g5_parent_b", "parent_b_alpha_id"),
    )

    id = Column(BigInteger, primary_key=True)
    task_id = Column(Integer, nullable=True)
    run_id = Column(Integer, nullable=True)
    round_idx = Column(Integer, nullable=True)
    region = Column(String(10), nullable=True)

    # Parents — FK to alphas.id. SET NULL on delete so log survives parent
    # rotation.
    parent_a_alpha_id = Column(
        Integer, ForeignKey("alphas.id", ondelete="SET NULL"), nullable=True,
    )
    parent_b_alpha_id = Column(
        Integer, ForeignKey("alphas.id", ondelete="SET NULL"), nullable=True,
    )
    parent_a_sharpe = Column(Float, nullable=True)
    parent_b_sharpe = Column(Float, nullable=True)
    parent_a_pillar = Column(String(20), nullable=True)
    parent_b_pillar = Column(String(20), nullable=True)

    # LLM call cost + offspring
    offspring_count = Column(Integer, nullable=False, default=0)
    # none_as_null: persist Python None as SQL NULL, not JSONB scalar 'null'
    # (the latter breaks jsonb_array_elements in /ops/g5/crossover-stats).
    offspring_expressions = Column(JSONB(none_as_null=True), nullable=True)  # [{expression, strategy, rationale}, ...]
    llm_model = Column(String(50), nullable=True)
    llm_cost_usd = Column(Float, nullable=True)
    llm_tokens_used = Column(Integer, nullable=True)

    # Outcome — back-filled by next round's persistence when offspring INSERT.
    # NULL when offspring never persisted (LLM produced 0 valid, or all 3
    # downstream stages dropped them).
    outcome_alpha_ids = Column(JSONB, nullable=True)  # [int, int, ...] alphas.id
    outcome_pass_count = Column(Integer, nullable=True)

    error_kind = Column(String(40), nullable=True)
    write_error = Column(Text, nullable=True)

    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
