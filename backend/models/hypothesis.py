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
    ForeignKey, Index, text,
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
        # P1-C part 2 (2026-05-15): partial index for the frontend
        # "active-trigger" list. Mirrors the same partial-where pattern as
        # ix_hypotheses_region_active above so PG can skip the bulk of
        # healthy rows.
        Index(
            "ix_hypotheses_triggered",
            "region", "triggered_at",
            postgresql_where=(
                "is_triggered IS TRUE AND status IN ('ACTIVE','PROMOTED')"
            ),
        ),
        # P2-B (2026-05-15): per-pillar active hypothesis count partial index.
        # Double declaration (Alembic + model layer) mirrors the existing four
        # partial indexes above — required so the sqlite test fixture's
        # ``metadata.create_all()`` builds the index. PG honours the partial
        # ``postgresql_where`` clause; sqlite ignores it and creates a regular
        # index, which is fine for tests.
        Index(
            "ix_hypotheses_pillar_active",
            "pillar",
            postgresql_where="pillar IS NOT NULL AND is_active IS TRUE",
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

    # P2-B (2026-05-15): Five Pillars factor classifier.
    # 取值: momentum|value|quality|volatility|sentiment|other (PILLAR_VALUES
    # 在 backend/pillar_classifier 校验)。NULL = legacy row;
    # pillar_classifier.infer_pillar 走 op/field 静态推断兜底。
    # 来源: docs/alphagbm_skills_research_2026-05-15.md skill `compare`.
    pillar = Column(String(20), nullable=True, default=None)

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
    # V-27.B (2026-05-14): parent_hypothesis_id was no longer written by the
    # G-refine loop (which was removed; never fired: 0/673 rows had a parent).
    # R1b.3-v2 (2026-05-18): re-activated as the CoSTEER mutation chain
    # backbone — node_hypothesis_mutate INSERTs a new row with this FK set
    # to the parent, and _maybe_record_failure_tree walks the chain to
    # build failure_tree skeletons past depth=1.
    parent_hypothesis_id = Column(
        Integer,
        ForeignKey("hypotheses.id", ondelete="SET NULL"),
        nullable=True,
    )

    # R1b.3-v2 (2026-05-18): 0 = original (LLM exploration root);
    # bumped per mutation event so a chain ROOT → M1 → M2 has depths
    # 0 → 1 → 2. Used by _maybe_record_failure_tree to cap the walk
    # at R1B_FAILURE_TREE_MAX_DEPTH and by R8 RAG L2 for skeleton ranking.
    r1b_mutation_depth = Column(
        Integer,
        nullable=True,
        default=0,
        server_default="0",
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

    # ---- P1-C part 2 (2026-05-15): structured triggers + LLM thesis scoring ----
    # 来源: docs/alphagbm_skills_research_2026-05-15.md skill `investment-thesis`.
    # `is_triggered` is a soft warning flag ORTHOGONAL to `status` — a triggered
    # hypothesis stays ACTIVE/PROMOTED and continues to be sampled (SFX-16
    # invariant). It only affects audit + LLM scoring output. To actually stop
    # sampling, set `is_active=False` (the existing regime-freeze mechanism).
    is_triggered = Column(
        Boolean, nullable=False, default=False,
        server_default=text("false"),
    )
    triggered_at = Column(DateTime(timezone=True), nullable=True)
    # Append-only list of {type, threshold, observed, window_rounds, severity,
    # reason, hit_at}. 24h dedup-by-(type, window); FIFO-capped at
    # TRIGGER_DETAIL_MAX_ENTRIES. MFX-7: server_default AND default=list — the
    # latter is required for sqlite create_all in tests (SQLite ignores
    # server_default on newly-inserted rows, leaving the column NULL otherwise).
    trigger_detail = Column(
        JSONB, nullable=False, default=list,
        server_default=text("'[]'::jsonb"),
    )
    # Frozen snapshot at first PROMOTED stamp time: {stamped_at, n_alphas,
    # alpha_pks_seed, sharpe_avg, fitness_avg, turnover_avg}. T1 trigger
    # `dropped_sharpe` compares current AVG vs this baseline AVG (MFX-1
    # symmetric semantics; n_alphas<3 auto-skips T1 to avoid small-sample
    # false-positives).
    baseline_metrics = Column(JSONB, nullable=True)
    # LLM-emitted 0-100 score; rewritten on each scoring run.
    thesis_score = Column(Float, nullable=True)
    last_thesis_score_at = Column(DateTime(timezone=True), nullable=True)
    # SFX-13: 'ok' | 'fallback_failed' | 'fallback_schema_invalid'. Read by
    # `_can_call_llm` to use a 4h backoff for failures vs 24h gate for ok runs.
    last_thesis_score_status = Column(String(30), nullable=True)
    # LLM-emitted free-text feedback (capped 600 chars).
    ai_feedback = Column(Text, nullable=True)
    # Append-only list of {scored_at, thesis_score, status,
    # recommended_action, ai_feedback}. FIFO-capped at 20 entries.
    thesis_score_history = Column(
        JSONB, nullable=False, default=list,
        server_default=text("'[]'::jsonb"),
    )

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


class HypothesisRoundStats(SQLAlchemyBase):
    """Per-hypothesis per-round outcome detail — append-only.

    V-27.92: the authoritative input for should_abandon_hypothesis. Pre-fix
    the abandon decision read state.hypothesis_round_history (in-memory),
    which is lost on worker restart / Celery task-boundary switch — so a
    hypothesis that should have been abandoned stayed ACTIVE forever. This
    table survives restarts and is shared across the V-20.1 prefetch round's
    isolated session, so should_abandon always sees the full N-round window.

    Counts are the REAL attribution: flip-retry products (V-27.71) and
    retryable transient-BRAIN-failure attempts (V-27.61) are tracked in their
    own columns and are NOT folded into alpha_count — the abandon decision
    reads a clean alpha_count.
    """

    __tablename__ = "hypothesis_round_stats"
    __table_args__ = (
        Index("ix_hrs_hid_round", "hypothesis_id", "round_index"),
        # Uniqueness key supports upsert on LangGraph checkpoint replay. task_id
        # is part of it: a hypothesis reused across tasks restarts round_index
        # from 0, so (hypothesis_id, round_index) alone would collide.
        Index(
            "uq_hrs_hid_round_task",
            "hypothesis_id", "round_index", "task_id",
            unique=True,
        ),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, index=True)

    hypothesis_id = Column(
        Integer,
        ForeignKey("hypotheses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # nullable=False — B5 (_process_hypothesis_feedback) always runs with a
    # task context, and task_id is part of the uniqueness key above.
    task_id = Column(
        Integer,
        ForeignKey("mining_tasks.id"),
        nullable=False,
        index=True,
    )
    round_index = Column(Integer, nullable=False)

    # ---- Real counts — flip products + retryable attempts excluded ----
    alpha_count = Column(Integer, default=0, nullable=False)
    pass_count = Column(Integer, default=0, nullable=False)
    syntax_fail_count = Column(Integer, default=0, nullable=False)
    simulate_fail_count = Column(Integer, default=0, nullable=False)
    quality_fail_count = Column(Integer, default=0, nullable=False)

    # ---- V-27.71: flip-retry products — separate track, never in alpha_count ----
    flip_alpha_count = Column(Integer, default=0, nullable=False)
    flip_pass_count = Column(Integer, default=0, nullable=False)

    # ---- V-27.61: retryable (transient BRAIN failure) attempts ----
    retryable_count = Column(Integer, default=0, nullable=False)

    # ---- Attribution (B5 v2 LLM / heuristic) ----
    attribution = Column(String(20), nullable=True)
    attribution_reason = Column(Text, nullable=True)
    best_sharpe = Column(Float, nullable=True)

    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<HypothesisRoundStats hid={self.hypothesis_id} "
            f"task={self.task_id} round={self.round_index} "
            f"alphas={self.alpha_count}/{self.pass_count}>"
        )
