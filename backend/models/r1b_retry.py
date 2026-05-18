"""Phase 3 R1b.1a: r1b_retry_log table for the CoSTEER loop.

Plan: ~/.claude/plans/phase3-r1b-costeer-loop-2026-05-18.md v1.3 §5.2.

One row per R1b loop firing (retry_impl OR mutate_hyp). Captures the
original-alpha context + LLM-produced new expression/hypothesis + outcome
(filled by post-BRAIN follow-up hook). Dedicated table per
[[feedback_r1a_dedicated_log_table]] — R1b fires per FAIL alpha that has
typed attribution and lives independently of the alphas table (which only
stores PASS/PROV rows).

Outcome reconciliation:
  When the next iteration evaluates the retry alpha, an updater task
  fills outcome / outcome_alpha_id_brain / outcome_sharpe / outcome_fitness.
  Until then outcome='pending'. In hard mode where retry budget exhausts,
  the closing row gets outcome='budget_exhausted'.
"""
from sqlalchemy import (
    BigInteger, Column, DateTime, Float, Index, Integer, String, Text,
    UniqueConstraint,
)
from sqlalchemy.sql import func

from backend.database import SQLAlchemyBase


class R1bRetryLog(SQLAlchemyBase):
    """One row per R1b retry / mutate event."""

    __tablename__ = "r1b_retry_log"
    __table_args__ = (
        Index("ix_r1b_task_id", "task_id"),
        Index("ix_r1b_created_at", "created_at"),
        Index("ix_r1b_attempt_type", "attempt_type"),
        Index("ix_r1b_outcome", "outcome"),
        # R1b.1 review LOW (2026-05-18): defensive UNIQUE against concurrent
        # dup rows when same alpha enters retry node twice (workflow restart
        # on stuck cycle, or future multi-worker LangGraph mode). Tuple uses
        # original_expression_hash (always populated via SHA256, unlike the
        # often-NULL pre-sim original_alpha_id_brain) and round_idx as the
        # retry attempt counter. attempt_type is included so retry_impl +
        # mutate_hyp can coexist on same alpha+round for BOTH attribution.
        UniqueConstraint(
            "task_id",
            "round_idx",
            "original_expression_hash",
            "attempt_type",
            name="uq_r1b_retry_log_task_alpha_attempt_type",
        ),
    )

    id = Column(BigInteger, primary_key=True)
    task_id = Column(Integer, nullable=True)
    round_idx = Column(Integer, nullable=True)
    attempt_type = Column(String(20), nullable=False)  # 'retry_impl' / 'mutate_hyp'
    triggering_attribution = Column(String(20))         # 'implementation' / 'hypothesis' / 'both'
    triggering_attribution_source = Column(String(20))  # 'r1a_heuristic' / 'r5_judge'

    # Original alpha context
    original_expression_hash = Column(String(64))
    original_alpha_id_brain = Column(String(64), nullable=True)
    original_hypothesis_id = Column(Integer, nullable=True)
    original_quality_status = Column(String(20))

    # Loop output
    new_expression = Column(Text, nullable=True)           # for retry_impl
    new_hypothesis_statement = Column(Text, nullable=True)  # for mutate_hyp
    new_hypothesis_id = Column(Integer, nullable=True)     # FK to fresh Hypothesis row
    llm_changes_made = Column(Text, nullable=True)         # LLM-reported diff sentence

    # Outcome (filled by post-BRAIN follow-up)
    outcome = Column(String(20), nullable=True)            # pending/pass/fail/budget_exhausted
    outcome_alpha_id_brain = Column(String(64), nullable=True)
    outcome_sharpe = Column(Float, nullable=True)
    outcome_fitness = Column(Float, nullable=True)

    # Cost + bookkeeping
    llm_cost_usd = Column(Float, nullable=True)
    llm_tokens_used = Column(Integer, nullable=True)
    llm_model = Column(String(50), nullable=True)
    loop_error = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
