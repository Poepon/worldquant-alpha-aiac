"""Phase 4 Sprint 1 A2 — R14 task_stop_loss_events model.

Plan: ~/.claude/docs/phase4_a_b_plan_v5_2026-05-19.md §6.2.

One row per R14 stop_loss trigger event — when the configurable EMA-floor
OR consecutive_zero policy fires, this records the snapshot that justified
the pause. Powers /ops/task-stop-loss/recent + the operator audit trail.

Mirrors the BRAIN_AUTH_CIRCUIT trip-audit pattern: dedicated table (per
[[feedback_r1a_dedicated_log_table]]) instead of stuffing into MiningTask
metadata, so the event history survives task purges / re-runs and operator
queries don't fight task hot-path writes.

Race fix (Round S0-A finding):
  Rounds that returned skipped=True with skipped_reason='brain_auth_circuit_open'
  must NOT count toward the consecutive_zero counter. The flat loop already
  `continue`s before calling stop_loss_service, satisfying this naturally,
  but the service ALSO defensively reads round_state["skipped_due_to_circuit_breaker"]
  and skips counter updates if set (defense in depth).
"""
from sqlalchemy import (
    Column, DateTime, Float, ForeignKey, Index, Integer, String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from backend.database import SQLAlchemyBase


class TaskStopLossEvent(SQLAlchemyBase):
    """One row per R14 stop_loss trigger event."""

    __tablename__ = "task_stop_loss_events"
    __table_args__ = (
        Index("ix_task_stop_loss_task_id", "task_id"),
        Index("ix_task_stop_loss_triggered_at", "triggered_at"),
        # Note: no UNIQUE here — a task can be re-resumed after pause and
        # trigger again on the next degenerate window. Each trigger row is
        # an independent forensic record.
    )

    # Integer (not BigInteger) — stop_loss events are low-volume (a handful
    # per task lifetime, NOT per round); Integer keeps SQLite auto-increment
    # working in test fixtures (BigInteger requires explicit autoincrement=True
    # on SQLite which the test setup doesn't apply).
    id = Column(Integer, primary_key=True)
    task_id = Column(
        Integer,
        ForeignKey("mining_tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    triggered_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    # 'pass_rate_floor' = EMA dipped below TASK_STOP_LOSS_PASS_RATE_FLOOR
    # 'consecutive_zero' = N consecutive rounds with 0 PASS alpha
    # 'manual_override' = ops console clear / debug
    trigger_reason = Column(String(40), nullable=False)

    # Snapshot at trigger time
    ema_pass_rate = Column(Float, nullable=True)
    consecutive_zero_rounds = Column(Integer, nullable=True)
    rounds_completed = Column(Integer, nullable=True)
    ema_window_pass_count = Column(Integer, nullable=True)

    # Forensic extras (LLM cost at trigger, last-N-round detail, etc).
    # JSONB so the shape can evolve without schema migration.
    meta_data = Column(JSONB, default=dict, nullable=False)

    def __repr__(self) -> str:
        return (
            f"<TaskStopLossEvent id={self.id} task_id={self.task_id} "
            f"reason={self.trigger_reason!r} "
            f"ema={self.ema_pass_rate} cz={self.consecutive_zero_rounds}>"
        )
