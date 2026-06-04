"""
Alpha Models - Alpha entities and related models

Contains Alpha, AlphaFailure, and AlphaPnl models.
"""

from sqlalchemy import Column, Integer, String, Boolean, DateTime, Float, Text, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, ARRAY
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from backend.database import SQLAlchemyBase


class Alpha(SQLAlchemyBase):
    """
    Alpha - Represents a generated alpha expression with its metrics.
    
    This is the core entity for storing alpha expressions, their
    simulation results, and quality status.
    """
    __tablename__ = "alphas"
    __table_args__ = (
        UniqueConstraint('alpha_id', name='uq_alpha_id'),
        {'extend_existing': True}
    )
    
    id = Column(Integer, primary_key=True, index=True)
    alpha_id = Column(String(20), unique=True, index=True)
    type = Column(String(20), default="REGULAR")  # REGULAR, SUPER
    
    # Associations
    task_id = Column(Integer, ForeignKey("mining_tasks.id"), nullable=True)
    trace_step_id = Column(Integer, ForeignKey("trace_steps.id"), nullable=True)
    template_id = Column(Integer, ForeignKey("templates.id"))
    run_id = Column(Integer, ForeignKey("experiment_runs.id"), nullable=True)
    
    # Core Info
    expression = Column(Text, nullable=False)
    expression_hash = Column(String(64))
    author = Column(String(50))
    name = Column(String(200))
    region = Column(String(10), nullable=False)
    universe = Column(String(50), nullable=False)
    dataset_id = Column(String(50), nullable=True)
    
    # Settings
    delay = Column(Integer, default=1)
    decay = Column(Integer, default=0)
    neutralization = Column(String(50), default="NONE")
    truncation = Column(Float, default=0.08)
    instrument_type = Column(String(20), default="EQUITY")
    
    # Status
    status = Column(String(20), default="created")  # created, simulated, submitted
    stage = Column(String(10), default="IS")  # IS, OS
    quality_status = Column(String(50), default="PENDING")
    
    # Metrics (Flattened)
    is_sharpe = Column(Float)
    is_turnover = Column(Float)
    is_fitness = Column(Float)
    is_returns = Column(Float)
    is_drawdown = Column(Float)
    is_margin = Column(Float)
    is_long_count = Column(Integer)
    is_short_count = Column(Integer)
    
    # Rich Metadata
    settings = Column(JSONB)
    tags = Column(ARRAY(String))
    checks = Column(JSONB)
    
    # Full Metrics Objects
    is_metrics = Column(JSONB)
    os_metrics = Column(JSONB)
    metrics = Column(JSONB, default={})
    
    # Dates
    date_created = Column(DateTime)
    date_modified = Column(DateTime)
    date_submitted = Column(DateTime)
    
    # Human Feedback
    human_feedback = Column(String(50), default="NONE")
    feedback_comment = Column(Text)
    
    # Context
    hypothesis = Column(Text)
    logic_explanation = Column(Text)
    fields_used = Column(JSONB, default=[])
    operators_used = Column(JSONB, default=[])

    # parent_alpha_id keeps flat hypothesis lineage (post tier-system removal,
    # 2026-05-18). The old ``factor_tier`` column was dropped — all alpha
    # quality classification now flows through the flat EVAL_* threshold band.
    parent_alpha_id = Column(Integer, ForeignKey("alphas.id"), nullable=True, index=True)

    # Optimization closure (Stage A, 2026-05-28). NULL for mining-origin
    # alphas; set for rows produced by OptimizationService.Persister.
    # Alembic: {hex}_phase16_a_optimization_runs.
    optimization_run_id = Column(
        Integer, ForeignKey("optimization_runs.id"), nullable=True
    )
    # Family root id: points at the topmost ancestor in the parent_alpha_id
    # chain (self.id for root rows, parent.parent_alpha_family_id otherwise).
    # Stage A dedup key for "have we already produced a variant from this
    # lineage" queries. Backfilled once in the migration via WITH RECURSIVE.
    parent_alpha_family_id = Column(
        Integer, ForeignKey("alphas.id"), nullable=True
    )

    metrics_snapshot_at = Column(DateTime(timezone=True), nullable=True)  # Last refresh from BRAIN
    # TODO #1 (2026-05-14): rolling OS-metric snapshots for decay/half-life
    # analysis. Daily Celery beat appends weekly. Each entry: snapshot_date,
    # days_since_submit, sharpe, fitness, turnover, returns, drawdown, margin.
    # Read pattern: per-alpha only — never queried by content.
    decay_curve = Column(JSONB, nullable=False, default=list, server_default="[]")
    # NULL = not yet refreshed from BRAIN GET /alphas/{id};
    # True = is.checks 全无 FAIL；False = 至少 1 个 FAIL
    can_submit = Column(Boolean, nullable=True)

    # B1 R11 (Sprint 2, 2026-05-20): USD capacity estimate stamped at PASS
    # alpha persist time. NULL when ENABLE_CAPACITY_SCORE was OFF at sim
    # time OR when (region, universe) lookup missed and estimator returned
    # 0. Per-alpha partial index (only non-NULL rows indexed) keeps
    # /ops/r11/capacity-stats range scans cheap without inflating the
    # alphas table footprint.
    # Alembic: k6e8b2a9f3d1_alpha_capacity_metadata
    capacity_usd_estimate = Column(Float, nullable=True)

    # Plan v5+ §Phase 2 B1 — hypothesis link. NULL for legacy / Phase 1 alphas
    # generated before HYPOTHESIS_CENTRIC_LEVEL=2 wired up. ON DELETE SET NULL
    # so hypothesis cleanup never cascades into alpha rows.
    hypothesis_id = Column(
        Integer,
        ForeignKey("hypotheses.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationships
    task = relationship("MiningTask", back_populates="alphas")
    trace_step = relationship("TraceStep", back_populates="alpha")
    # ``parent`` follows the hypothesis-lineage FK (parent_alpha_id), NOT the
    # optimization-family FK (parent_alpha_family_id) which also targets
    # alphas.id. Disambiguated via foreign_keys=[parent_alpha_id] — without
    # it SQLAlchemy can't pick between the two self-referential FKs and
    # raises AmbiguousForeignKeysError at mapper-configure time.
    parent = relationship(
        "Alpha",
        remote_side=[id],
        backref="children",
        foreign_keys="Alpha.parent_alpha_id",
    )
    # Phase 2: typed hypothesis link. The Text `hypothesis` column above is
    # kept for legacy compat (LLM-emitted summary text); hypothesis_obj is
    # the structured row.
    hypothesis_obj = relationship(
        "Hypothesis",
        back_populates="alphas",
        foreign_keys=[hypothesis_id],
    )


class AlphaFailure(SQLAlchemyBase):
    """
    Alpha Failure - Records failed alpha attempts for the feedback loop.
    
    Used by Feedback Agent to learn and improve.
    """
    __tablename__ = "alpha_failures"
    __table_args__ = {'extend_existing': True}
    
    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("mining_tasks.id"), nullable=True)
    trace_step_id = Column(Integer, ForeignKey("trace_steps.id"), nullable=True)
    run_id = Column(Integer, ForeignKey("experiment_runs.id"), nullable=True)

    expression = Column(Text, nullable=True)
    error_type = Column(String(100), nullable=True)  # SYNTAX_ERROR, FIELD_NOT_FOUND, TIMEOUT
    error_message = Column(Text, nullable=True)
    raw_response = Column(Text, nullable=True)

    # V-25.B (2026-05-13): typed Hypothesis link for FAIL alphas. Mirrors
    # Alpha.hypothesis_id so B5 attribution can span PASS + FAIL rows
    # via the same key. NULL for legacy rows (pre-migration) and for
    # non-Phase-2 paths. ON DELETE SET NULL so hypothesis cleanup never
    # cascades into failure rows we may need for forensic audit.
    hypothesis_id = Column(
        Integer,
        ForeignKey("hypotheses.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # For feedback analysis
    is_analyzed = Column(Boolean, default=False)

    # G1 follow-up (2026-05-19): bandit-arm provenance stamp. Symmetric with
    # Alpha.metrics["_direction_bandit_recommended_arm"] on the PASS path so
    # /ops/direction-bandit/telemetry per-arm denominator includes
    # PASS + FAIL (true Bayesian arm posterior, not PASS-only sample).
    # NULL for legacy rows (pre-migration) and for rounds where the bandit
    # was OFF / cold-start (round 1).
    bandit_arm_recommended = Column(String(40), nullable=True, index=True)

    # RAG category-overlap A/B (2026-05-21): per-round experiment arm
    # ("control"/"category") for the FAIL path — symmetric with
    # Alpha.metrics["_rag_ab_arm"] on the PASS path. Failures dominate the
    # "real BRAIN sim" denominator (~40:1 vs alphas), so per-arm failure
    # attribution is essential for scripts/rag_ab_report.py's PASS-per-sim.
    # NULL when ENABLE_RAG_CATEGORY_AB is OFF / legacy rows.
    rag_ab_arm = Column(String(40), nullable=True, index=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AlphaPnl(SQLAlchemyBase):
    """
    Alpha PnL - Daily PnL records for an alpha.
    """
    __tablename__ = "alpha_pnl"
    __table_args__ = {'extend_existing': True}
    
    id = Column(Integer, primary_key=True)
    alpha_id = Column(Integer, index=True)
    trade_date = Column(DateTime, nullable=False)
    pnl = Column(Float)
    cumulative_pnl = Column(Float)
    created_at = Column(DateTime, server_default=func.now())


class AutoSubmitAudit(SQLAlchemyBase):
    """Audit trail for the auto-submit beat (2026-06-04).

    One row per candidate the beat evaluated — in BOTH shadow and live mode —
    recording which guard gates it passed, the raw signal values at decision
    time, and the final outcome. Shadow mode writes ``would_submit`` rows WITHOUT
    submitting (the human-review surface before flipping to live); live mode
    writes ``submitted`` / ``rejected`` / ``error`` / ``skipped``.

    ``alpha_pk`` is a plain Integer (not FK) so audit rows survive alpha purges.
    """
    __tablename__ = "auto_submit_audit"
    __table_args__ = {'extend_existing': True}

    id = Column(Integer, primary_key=True)
    alpha_pk = Column(Integer, index=True, nullable=False)
    alpha_brain_id = Column(String(20), nullable=True)
    region = Column(String(20), nullable=True, index=True)
    mode = Column(String(10), nullable=False)        # shadow | live
    outcome = Column(String(20), nullable=False, index=True)  # would_submit|submitted|rejected|skipped|error
    skip_reason = Column(Text, nullable=True)        # which gate failed / why
    gate_results = Column(JSONB, default={})         # all signal values + per-gate pass/fail
    brain_response = Column(JSONB, nullable=True)     # submit_alpha return (live)
    beat_run_id = Column(String(40), index=True, nullable=True)  # groups one beat firing
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
