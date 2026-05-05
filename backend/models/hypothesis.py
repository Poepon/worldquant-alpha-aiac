"""Hypothesis Model — typed first-class hypothesis for HGE Phase 2.

Plan v5+ §Phase 2 B1: Hypothesis becomes a DB row, not just a transient
LLM dict. Each alpha references its parent hypothesis_id (Alpha.hypothesis_id
FK), enabling:

- Cross-round accumulation of alphas under the same hypothesis
- Lifecycle (PROPOSED → ACTIVE → PROMOTED / ABANDONED) tracked over multiple
  rounds, not reset per round like the legacy dict path
- KB learning unit upgrade (Phase 2 B8): SUCCESS_PATTERN entries reference
  hypothesis_id so RAG retrieval can pull "examples from this hypothesis
  family" rather than "examples from this dataset"
- Plan v5+ §决策 6 修正 5 simplified freeze: is_active boolean toggled by
  monthly regime review (the full FROZEN/DEPRECATED state machine was cut
  by Plan v4 §三轮精简 backlog)

Mirrors backend/agents/core/experiment.Hypothesis (typed dataclass) but adds
operational fields (region/dataset_pool/lineage/stats/lifecycle) that the
dataclass doesn't track.
"""

from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, Float, Text,
    ForeignKey, Index,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from backend.database import SQLAlchemyBase


class Hypothesis(SQLAlchemyBase):
    """One row per generated hypothesis. Append-mostly: alphas accumulate
    under it across rounds; lifecycle status updates over time."""

    __tablename__ = "hypotheses"
    __table_args__ = (
        # Active hypotheses per region — sampling path for node_hypothesis
        Index(
            "ix_hypotheses_region_active",
            "region", "is_active",
            postgresql_where="status IN ('PROPOSED', 'ACTIVE')",
        ),
        # Variant-isolation: phase gate / KB queries filter by variant
        Index(
            "ix_hypotheses_variant",
            "experiment_variant",
            postgresql_where="experiment_variant IS NOT NULL",
        ),
        # Parent lineage for ImprovementRule chaining (T2/T3)
        Index(
            "ix_hypotheses_parent_alpha",
            "parent_alpha_id",
            postgresql_where="parent_alpha_id IS NOT NULL",
        ),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, index=True)

    # ---- Core (mirrors typed Hypothesis dataclass) ----
    statement = Column(Text, nullable=False)
    rationale = Column(Text, nullable=True)

    # InvestmentThesis vs ImprovementRule (Plan v5+ §决策 3)
    # Stored as String not Postgres ENUM so future kinds don't need migration.
    kind = Column(String(30), default="INVESTMENT_THESIS", index=True)

    # Which tier this hypothesis targets (1=T1, 2=T2, 3=T3). Aligned with
    # task.agent_mode → factor_tier mapping.
    target_tier = Column(Integer, default=1, index=True)

    # Classification (LLM-emitted)
    expected_signal = Column(String(50), default="unknown")
    confidence = Column(String(20), default="medium")     # high|medium|low
    novelty = Column(String(30), default="established")   # established|emerging|experimental

    # Hints for downstream code_gen / strategy_select
    key_fields = Column(JSONB, default=list)
    suggested_operators = Column(JSONB, default=list)

    # ---- Region / dataset binding ----
    region = Column(String(10), nullable=False, index=True)
    universe = Column(String(50), nullable=True)

    # Cross-dataset support (Plan v5+ §Phase 1 + Phase 2): a hypothesis may
    # combine fields from multiple datasets. dataset_pool is the LLM-selected
    # set; downstream code_gen must use only fields that union.
    dataset_pool = Column(JSONB, default=list)

    # ---- Lineage ----
    # ImprovementRule path (Plan §决策 3): when kind=IMPROVEMENT_RULE the
    # hypothesis improves on a specific T1/T2 PASS alpha (parent_alpha_id)
    # or chains from another hypothesis (parent_hypothesis_id).
    parent_alpha_id = Column(
        Integer,
        ForeignKey("alphas.id", ondelete="SET NULL"),
        nullable=True,
    )
    parent_hypothesis_id = Column(
        Integer,
        ForeignKey("hypotheses.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ---- Variant isolation (Plan v5+ F-5) ----
    # Phase gate灰度期间 RAG retrieval / dedup confined to same variant so
    # legacy and Phase 2 don't pollute each other's KB.
    experiment_variant = Column(String(20), nullable=True)

    # ---- Aggregated stats (updated by hypothesis_service.refresh_stats) ----
    # Denormalized for frontend grouping + abandon-criterion checks. Source
    # of truth is alphas.hypothesis_id JOIN; these columns are the rollup.
    alpha_count = Column(Integer, default=0, nullable=False)
    pass_count = Column(Integer, default=0, nullable=False)  # PASS + PASS_PROVISIONAL
    sharpe_avg = Column(Float, nullable=True)
    sharpe_max = Column(Float, nullable=True)

    # ---- Lifecycle ----
    # PROPOSED  — just inserted, no alphas yet
    # ACTIVE    — has ≥1 alpha generated (regardless of PASS/FAIL)
    # PROMOTED  — has ≥1 PASS alpha (kept long-term for KB)
    # ABANDONED — should_abandon_hypothesis triggered (Plan §B6)
    # SUPERSEDED — replaced by a child hypothesis (parent_hypothesis_id ref)
    status = Column(String(20), default="PROPOSED", nullable=False, index=True)
    abandon_reason = Column(Text, nullable=True)

    # Plan v5+ Final §简化冷冻: single boolean toggled by monthly regime
    # review instead of full FROZEN/DEPRECATED state machine. is_active=False
    # means sampling skips this hypothesis without changing status.
    is_active = Column(Boolean, default=True, nullable=False, index=True)

    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # ---- Relationships ----
    # Alphas referencing this hypothesis. foreign_keys disambiguates from
    # the legacy alpha.hypothesis Text column.
    alphas = relationship(
        "Alpha",
        back_populates="hypothesis_obj",
        foreign_keys="Alpha.hypothesis_id",
        cascade="save-update",
    )
    parent_hypothesis = relationship(
        "Hypothesis",
        remote_side=[id],
        backref="child_hypotheses",
    )
    parent_alpha = relationship(
        "Alpha",
        foreign_keys=[parent_alpha_id],
        post_update=True,
    )

    def __repr__(self) -> str:
        return (
            f"<Hypothesis id={self.id} kind={self.kind} tier=T{self.target_tier} "
            f"status={self.status} alphas={self.alpha_count}/{self.pass_count}>"
        )
