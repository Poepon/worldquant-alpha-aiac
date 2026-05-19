"""Phase 4 Sprint 3 A5.1 G10 — distilled_logic_library model.

Plan: docs/phase4_a_b_plan_v5_2026-05-19.md §6.12 / v4 §6.12.

LLM-distilled summary of common logic across the last 7 days' PASS
alphas, grouped by (pillar, region). Sunday 03:00 SH weekly_logic_
distill cron writes new rows; PR2 refine_logic_library (Sprint 4)
updates retired_at when a fresh distillation supersedes a stale row.

Dedicated table per [[feedback_r1a_dedicated_log_table]] — operator
queries on logic library don't fight alpha persistence hot path.
"""
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from backend.database import SQLAlchemyBase


class DistilledLogic(SQLAlchemyBase):
    """One distilled-logic entry — LLM summary of what worked in a
    (pillar, region) bucket for one week."""

    __tablename__ = "distilled_logic_library"

    id = Column(BigInteger, primary_key=True)

    logic_text = Column(Text, nullable=False)
    # Tokenized text for Jaccard similarity (PR2 retrieval / dedup)
    tokens = Column(JSONB, nullable=False, default=list)
    # Soft references to source alphas (no FK so alpha purge doesn't cascade)
    source_alpha_ids = Column(JSONB, nullable=False, default=list)

    pillar = Column(String(50), nullable=True, index=True)
    region = Column(String(10), nullable=False, index=True)

    distilled_at_week = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    llm_cost_usd = Column(Float, nullable=True)
    similarity_jaccard_to_prev_week = Column(Float, nullable=True)

    # NULL = active, non-NULL = superseded by a later row (Sprint 4 PR2)
    retired_at = Column(DateTime(timezone=True), nullable=True)
    llm_model = Column(String(80), nullable=True)
