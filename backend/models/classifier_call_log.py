"""Pitfall-classifier call log table.

One row per `feedback_agent._classify_pitfall_error_type` decision: every
new_pitfall the LLM emitted, with whether the helper kept it (stamped a
category) or dropped it as noise. Powers /ops/classifier/stats so
operators can see drop rate, top noise strings, per-region breakdown,
and timeline — none of which the in-process logger.info gave.

DESIGN:
  - One row per pitfall classification call. Batched INSERT at the end
    of FeedbackAgent.learn_from_round (analogous to llm_call_log).
  - `resolved_category` is NULL when the helper dropped the row as
    noise; non-NULL value is the stamped meta_data['category'] of the
    written KB row ('threshold' / 'robustness' / 'static_finding').
  - `error_type` is the raw LLM string, truncated to 200 chars.
"""
from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Integer,
    String,
)
from sqlalchemy.sql import func

from backend.database import SQLAlchemyBase


class ClassifierCallLog(SQLAlchemyBase):
    """One row per pitfall-classifier decision."""

    __tablename__ = "classifier_call_log"
    __table_args__ = (
        Index("ix_classifier_call_log_task_id", "task_id"),
        Index("ix_classifier_call_log_created_at", "created_at"),
        Index("ix_classifier_call_log_resolved_category", "resolved_category"),
    )

    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, nullable=True)
    iteration = Column(Integer, nullable=True)
    region = Column(String(16), nullable=True)
    dataset_id = Column(String(64), nullable=True)

    error_type = Column(String(200), nullable=True)
    resolved_category = Column(String(32), nullable=True)

    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
