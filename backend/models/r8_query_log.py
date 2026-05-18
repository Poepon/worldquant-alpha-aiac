"""Phase 3 R8 follow-up: r8_query_log table for hierarchical RAG telemetry.

Plan: per [[project_phase3_r8_kb_shape_endpoint_2026_05_18]] "future work"
listing — kb-shape gives *corpus*-level visibility, this table records
*per-query* layer hit distribution + cache effectiveness so operators
see runtime L0/L1/L2/L3 fall-through patterns + Redis cache hit rate.

One row per `query_hierarchical` call when ENABLE_R8_QUERY_LOG flag is
ON. Soft-fail INSERT — DB error logs warn + swallows so RAG retrieval
NEVER aborts on telemetry writes.
"""
from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Index, Integer, String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func, text

from backend.database import SQLAlchemyBase


class R8QueryLog(SQLAlchemyBase):
    """One row per query_hierarchical invocation (when flag ON)."""

    __tablename__ = "r8_query_log"
    __table_args__ = (
        Index("ix_r8q_created_at", "created_at"),
        Index("ix_r8q_task_id", "task_id"),
    )

    id = Column(BigInteger, primary_key=True)
    task_id = Column(Integer, nullable=True)              # nullable — background jobs can RAG
    region = Column(String(8), nullable=True)
    dataset_id = Column(String(64), nullable=True)
    current_expression_hash = Column(String(64), nullable=True)

    # Per-layer hit counts — {"L0_exact": N, "L1_pillar": N, "L2_family": N, "L3_field": N}
    layer_hits = Column(JSONB, nullable=True, server_default=text("'{}'::jsonb"))
    total_queries = Column(Integer, nullable=True, server_default="0")
    cache_hit = Column(Boolean, nullable=True, server_default=text("false"))
    had_failure_tree_elevation = Column(
        Boolean, nullable=True, server_default=text("false"),
    )

    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
