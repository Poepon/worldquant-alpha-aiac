"""
Alpha Status Transition Model — audit log for quality_status changes.

Every change to alphas.quality_status writes a row here via
alpha_service.apply_quality_status_change(). Used for:
- 「晋级数」metric (T1→T2 / T2→T3 promotion counts based on transition events)
- Lineage tree status badges ("was PASS, now PROVISIONAL")
- Debug trail when a previously-PASS alpha drifts to FAIL
"""

from sqlalchemy import (
    Boolean, Column, Integer, String, Float, DateTime, ForeignKey, Index, Text,
    text,
)
from sqlalchemy.sql import func

from backend.database import SQLAlchemyBase


class AlphaStatusTransition(SQLAlchemyBase):
    """One row per quality_status change. Append-only — never updated, never deleted."""
    __tablename__ = "alpha_status_transitions"
    __table_args__ = (
        Index("ix_status_trans_alpha", "alpha_id", "transitioned_at"),
        Index("ix_status_trans_time", "transitioned_at"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True)
    alpha_id = Column(Integer, ForeignKey("alphas.id", ondelete="CASCADE"), nullable=False)
    old_status = Column(String(50))
    new_status = Column(String(50), nullable=False)
    # Snapshot of alpha.is_sharpe at the moment this transition was written.
    # Not old/new pair — to reconstruct historical sharpe values use SQL window
    # function over transitioned_at.
    sharpe_at_transition = Column(Float)
    # Human-readable reason (free text). Examples:
    #   "sharpe drifted to 1.4 (below threshold)"
    #   "user manual review"
    reason = Column(String(200))
    # Machine-readable source (controlled enum). Values:
    #   "node_evaluate" / "daily_beat_kb" / "daily_beat_os" / "backfill" /
    #   "manual_api"
    source = Column(String(50))
    transitioned_at = Column(DateTime(timezone=True), server_default=func.now())


class HypothesisStatusTransition(SQLAlchemyBase):
    """Audit log for ``hypotheses.is_triggered`` edge transitions.

    P1-C part 2 (2026-05-15) — append-only row written by the daily
    ``hypothesis-health-check`` Celery beat whenever a hypothesis crosses
    the False → True ``is_triggered`` boundary (SFX-10: edge-only — same-day
    re-trigger / steady-state True does NOT add a row, so the table stays
    grep-able).

    Status-column intentionally omitted (no ``old_status`` / ``new_status``
    columns) — adding them would force ``mark_abandoned`` / ``mark_promoted``
    to start writing rows or the audit becomes asymmetric. Status coverage
    is deferred to a follow-up; this table stays narrowly scoped to the
    trigger-flag edge.
    """
    __tablename__ = "hypothesis_status_transitions"
    __table_args__ = (
        Index("ix_hyp_status_trans_hid", "hypothesis_id", "transitioned_at"),
        Index("ix_hyp_status_trans_time", "transitioned_at"),
        Index(
            "ix_hyp_status_trans_triggered",
            "transitioned_at",
            postgresql_where=text("new_is_triggered IS TRUE"),
        ),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True)
    hypothesis_id = Column(
        Integer,
        ForeignKey("hypotheses.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Edge endpoints. ``old_is_triggered`` can be NULL on the very first
    # transition for a hypothesis (no prior state row); subsequent edges
    # always have it populated.
    old_is_triggered = Column(Boolean, nullable=True)
    new_is_triggered = Column(Boolean, nullable=False)
    # Snapshot of ``hypothesis.sharpe_avg`` at the moment this transition
    # was written (mirrors AlphaStatusTransition.sharpe_at_transition). Note
    # this is the denormalized cache, not a fresh JOIN — used only as a
    # rough historical breadcrumb.
    sharpe_at_transition = Column(Float, nullable=True)
    # Human-readable reason — typically a "; "-joined list of TriggerHit
    # reason strings (e.g. "sharpe_down_50pct_vs_baseline; pass_rate_dropped_60pct").
    reason = Column(Text, nullable=True)
    # Machine-readable source. Values: "trigger_eval_beat" | "manual".
    source = Column(String(50), nullable=True)
    transitioned_at = Column(
        DateTime(timezone=True), server_default=func.now(),
    )
