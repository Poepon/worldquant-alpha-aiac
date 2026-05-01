"""
Alpha Status Transition Model — audit log for quality_status changes.

Every change to alphas.quality_status writes a row here via
alpha_service.apply_quality_status_change(). Used for:
- 「晋级数」metric (T1→T2 / T2→T3 promotion counts based on transition events)
- Lineage tree status badges ("was PASS, now PROVISIONAL")
- Debug trail when a previously-PASS alpha drifts to FAIL
"""

from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Index
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
    #   "sharpe drifted to 1.4 (below T3 threshold)"
    #   "user manual review"
    #   "tier reclassified"
    reason = Column(String(200))
    # Machine-readable source (controlled enum). Values:
    #   "node_evaluate" / "tier_seed_refresh" / "daily_beat_kb" /
    #   "daily_beat_os" / "backfill" / "manual_api"
    source = Column(String(50))
    transitioned_at = Column(DateTime(timezone=True), server_default=func.now())
