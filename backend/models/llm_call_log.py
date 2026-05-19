"""LLM call log table (G2 Phase A, 2026-05-19).

Per [[feedback_r1a_dedicated_log_table]] + [[feedback_light_wiring_deferred_gate]]:
existing per-call telemetry is split across r1b_retry_log (R1b path only),
in-process metrics_tracker._node_metrics (no persistence, no task_id), and
ad-hoc usage logs in node-specific tables. None of them give a global
(task × dataset × pillar × node × hour) cost breakdown.

DESIGN:
  - One row per LLMService.call invocation (whether successful or failed),
    written via the cost_tracker contextvar batch flush at round boundary.
  - task_id / run_id / round_idx / node_key resolved from contextvars set
    by mining_agent.run_evolution_loop round entry; nullable for calls
    outside a mining round (sync tasks, ops scripts).
  - cost_usd is derived once at flush time from prompt+completion tokens
    × LLM_PRICING_USD_PER_1K_TOKENS[model_prefix]. NULL when model is not
    in the pricing dict (tokens still recorded so cost can be re-derived
    later via SQL).
  - INSERT batched per round (typical 6-20 calls/round) — no per-call
    sync DB hit on the LLM hot path.
"""
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.sql import func

from backend.database import SQLAlchemyBase


class LLMCallLog(SQLAlchemyBase):
    """One row per LLMService.call invocation (flag-gated)."""

    __tablename__ = "llm_call_log"
    __table_args__ = (
        Index("ix_llmcl_task_id", "task_id"),
        Index("ix_llmcl_run_id", "run_id"),
        Index("ix_llmcl_created_at", "created_at"),
        Index("ix_llmcl_node_key", "node_key"),
        Index("ix_llmcl_model", "model"),
    )

    id = Column(BigInteger, primary_key=True)
    task_id = Column(Integer, nullable=True)
    run_id = Column(Integer, nullable=True)
    round_idx = Column(Integer, nullable=True)
    dataset_id = Column(String(64), nullable=True)
    pillar = Column(String(20), nullable=True)

    node_key = Column(String(40), nullable=True)
    model = Column(String(60), nullable=False)
    provider = Column(String(20), nullable=True)
    effort = Column(String(20), nullable=True)

    prompt_tokens = Column(Integer, nullable=True)
    completion_tokens = Column(Integer, nullable=True)
    tokens_total = Column(Integer, nullable=False, default=0)
    cost_usd = Column(Float, nullable=True)

    latency_ms = Column(Integer, nullable=True)
    success = Column(Boolean, nullable=False, default=True)
    error_kind = Column(String(40), nullable=True)
    call_id = Column(String(20), nullable=True)
    write_error = Column(Text, nullable=True)

    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
