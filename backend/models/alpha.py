"""
Alpha Models - Alpha entities and related models

Contains Alpha, AlphaFailure, and AlphaPnl models.
"""

from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, Float, Text, ForeignKey,
    UniqueConstraint, Index, text,
)
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
    # run_id dropped in Phase 1d (experiment_runs retired; pool has no per-run concept)

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
    # TIMEZONE CONVENTION (footgun — see [[reference_alpha_dual_timezone]]):
    # these three are naive-BEIJING, NOT UTC. date_created / date_submitted are the
    # alpha's BRAIN backtest creation / submission times, converted BRAIN-UTC → +8h
    # Beijing and tz-stripped by sync_tasks._parse_to_beijing; date_modified is a
    # local datetime.now(). They are kept Beijing on purpose (the frontend displays
    # them as local time) and are only ever used as presence flags (IS NULL),
    # self-consistent ORDER BY, or display. For any UTC date math / "today" / daily-
    # quota boundary use `created_at` (naive-UTC, server_default=func.now()) — NEVER
    # cross-compare these Beijing columns against a naive-UTC value (8h skew).
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
    # TODO #1 (2026-05-14): rolling IS-metric snapshots for decay/half-life
    # analysis. Daily Celery beat appends weekly. Each entry: snapshot_date,
    # days_since_submit, sharpe, fitness, turnover, returns, drawdown, margin.
    # Read pattern: per-alpha only — never queried by content.
    # ⚠️ 口径=IS(快照源 alpha.is_*),非 OS——BRAIN 隐藏 realized OS;快照自带 "basis":"IS"。
    # (旧注释误写 "OS-metric",2026-06-07 更正:实为 IS 指标滚动快照。)
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
    __table_args__ = (
        # Pool dedup backstop: a PARTIAL UNIQUE index (WHERE NOT NULL) makes the
        # E-pool failure write idempotent per candidate_queue PK — the DB backstop
        # behind the best-effort Redis persist-marker. NULLs are distinct, so FLAT/
        # legacy rows (candidate_queue_id=NULL) are unconstrained. On SQLite the
        # postgresql_where is dropped → a plain UNIQUE index (NULLs still distinct).
        Index(
            "uq_alpha_failures_candidate_queue_id",
            "candidate_queue_id",
            unique=True,
            postgresql_where=text("candidate_queue_id IS NOT NULL"),
        ),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("mining_tasks.id"), nullable=True)
    trace_step_id = Column(Integer, ForeignKey("trace_steps.id"), nullable=True)
    # run_id dropped in Phase 1d (experiment_runs retired)

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

    # Pool pipeline (four-pool decoupling, 2026-06-05): the E pool persists FAIL
    # rows here with their full signal payload — verdict_signals + BRAIN sim
    # metrics (sharpe/fitness/turnover/...) — symmetric with Alpha.metrics on the
    # PASS path, so forensic / B5-attribution analytics span PASS + FAIL via one
    # shape. none_as_null avoids the JSON-null footgun (None → JSONB scalar
    # 'null' breaks jsonb_* reads). NULL for legacy rows and any non-pool path.
    metrics = Column(JSONB(none_as_null=True), nullable=True)

    # Pool pipeline dedup link (2026-06-06): the candidate_queue PK the E pool
    # persisted this FAIL row from — backs the uq_alpha_failures_candidate_queue_id
    # partial-unique index above (closes the B2 crash-window double-write on the
    # load-bearing alpha_failures denominator). NO ForeignKey: candidate_queue rows
    # are purged but failure rows are a permanent audit log — an ON DELETE SET NULL
    # would silently un-dedup on purge, a NOT NULL FK would block purges. So a weak
    # foreign-key-less link, integrity by the partial unique index. NULL for FLAT /
    # legacy / any non-pool path.
    candidate_queue_id = Column(Integer, nullable=True)

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
