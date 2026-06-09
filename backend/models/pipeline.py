"""Mining pipeline queues — Phase 0 foundation (four-pool decoupling).

Two DB-persistent work queues that replace the in-memory ``asyncio.Queue``
hand-offs of the single-task FLAT pipeline. They are the claim sources for the
resident HG / Simulate / Evaluate worker pools — see
``docs/four_pool_decoupling_plan_2026-06-05.md``.

  hyp_intent      — one row per generation intent; the **HG** pool claim source.
                    Carries the frozen ``config_snapshot`` (eval-band thresholds
                    + llm_overrides + brain_role_snapshot, lifted off the legacy
                    ExperimentRun) so a claimed intent is independently
                    hydratable with no live-settings drift.

  candidate_queue — one row per ``is_valid`` candidate emitted by HG; flows
                    HG → **S** (PENDING_SIM) → **E** (PENDING_EVAL) →
                    DONE/FAILED. Persists ``pipeline/types.py`` Candidate +
                    SimResult as DB rows. The role-snapshot fields S/E read
                    (``effective_default_test_period`` /
                    ``effective_sharpe_submit_min`` / ``delay``) are
                    **first-class columns** (终审 #7) so a hydrated row can never
                    silently fall back to the wrong testPeriod / sharpe gate.

Claim/lease contract (BUILT in Phase 1b — these tables are INERT in Phase 0,
nothing reads or writes them yet): two-transaction claim (``SELECT FOR UPDATE
SKIP LOCKED`` + ``UPDATE CLAIMED`` + COMMIT *before* any long node await) +
heartbeat-renewed lease + ``attempts``-capped poison-pill. Lineage anchors on
``hypotheses.id``, NOT ``run_id`` — ``experiment_runs`` is left untouched
(Phase 1d). ``alphas.run_id`` / ``trace_steps.run_id`` are already nullable, so
pool rows simply omit them.

NOT carried across the pool boundary (intentional): the in-memory RAG/distill
products (patterns / pitfalls / focused_fields / distilled_concepts /
recent_dedup_skeletons). They live only inside one HG workflow run and are
consumed before a candidate is emitted — fused H+G is exactly why they never
need a queue column.

Status/stage are plain ``String`` (no PG ENUM — repo convention, keeps values
flexible). JSONB columns use ``none_as_null=True`` to avoid the JSON-null
footgun (Python ``None`` → JSONB scalar ``'null'`` breaks ``jsonb_*`` functions).
"""
from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from backend.database import SQLAlchemyBase


class HypothesisIntent(SQLAlchemyBase):
    """One generation intent — the HG pool claim source.

    Lifecycle stage: ``PENDING`` → ``CLAIMED`` → ``DONE`` | ``FAILED`` |
    ``PURGED``. The scheduler INSERTs ``PENDING`` rows (weighted_choice over
    ``DatasetCellStats.mining_weight`` under a per-region advisory lock); the HG
    pool claims them, runs rag→distill→hypothesis→codegen→validate, emits N
    ``candidate_queue`` rows, and marks the intent ``DONE``.
    """

    __tablename__ = "hyp_intent"
    __table_args__ = (
        # HG claim scan (stage='PENDING') + lease-recycle scan
        # (stage='CLAIMED' AND lease_expires_at<now). Partial — terminal rows
        # (DONE/FAILED/PURGED) dominate volume and are never scanned here, so
        # they stay out of the btree.
        Index(
            "ix_hyp_intent_claim",
            "stage",
            "lease_expires_at",
            postgresql_where="stage IN ('PENDING', 'CLAIMED')",
        ),
        Index("ix_hyp_intent_task_id", "task_id"),
        {"extend_existing": True},
    )

    # No index=True on the PK — the primary-key constraint already provides the
    # unique btree; a separate ix_*_id would be redundant (and drift from the
    # migration, which creates only the PK index).
    id = Column(Integer, primary_key=True)

    # Owning scope. MiningTask becomes the resident mining-intent/scope in the
    # pool world (its dispatch-era columns drop in Phase 1d). Nullable so the
    # scheduler can also insert ad-hoc scope-less intents. ondelete SET NULL
    # (mirrors the hypotheses FK + g5_crossover_log) so a task hard-delete
    # (scripts/cleanup_historical_tasks.py) doesn't FK-block on queue rows.
    task_id = Column(
        Integer, ForeignKey("mining_tasks.id", ondelete="SET NULL"), nullable=True,
    )

    # --- claim / lease machinery (consumed Phase 1b) ---
    # stage is NOT separately indexed — ix_hyp_intent_claim (composite partial)
    # serves the stage-prefixed claim/recycle scans.
    stage = Column(String(20), nullable=False, server_default="PENDING")
    claimed_by = Column(String(64), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    attempts = Column(Integer, nullable=False, server_default="0")

    # --- generation scope ---
    region = Column(String(10), nullable=False)
    universe = Column(String(50), nullable=True)
    dataset_id = Column(String(50), nullable=True, index=True)
    delay = Column(Integer, nullable=False, server_default="1")
    # N candidates to emit (legacy ALPHAS_PER_ROUND → per-intent fan-out).
    fanout = Column(Integer, nullable=True)

    # Arm provenance for the binary-can_submit bandit reward (symmetric across
    # PASS + FAIL so the per-arm posterior denominator is complete).
    bandit_arm = Column(String(40), nullable=True)
    rag_ab_arm = Column(String(40), nullable=True)

    # Orthogonal-breadth field steering (2026-06-09, PR-B). When set by the
    # scheduler (gated ENABLE_FIELD_SCREENING, explore-fraction of intents), the
    # HG generation node steers code-gen around this under-explored field. NULL =
    # legacy (no field steering). Migration r3c8a5d1f9b4.
    target_field = Column(String(200), nullable=True)

    # Frozen config: eval-band thresholds + llm_overrides + brain_role_snapshot.
    # NOT NULL + none_as_null + default=dict: writers must supply a real dict;
    # an explicit None would map to SQL NULL and fail-loud on the NOT NULL
    # (the intended contract for a frozen snapshot). server_default keeps the
    # DB column identical between create_all and the migration.
    config_snapshot = Column(
        JSONB(none_as_null=True), nullable=False, default=dict,
        server_default=text("'{}'::jsonb"),
    )
    prompt_version = Column(String(100), nullable=True)
    thresholds_version = Column(String(100), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )


class CandidateQueue(SQLAlchemyBase):
    """One ``is_valid`` candidate — flows HG → S → E.

    Lifecycle stage: ``PENDING_SIM`` → ``SIMULATING`` → ``PENDING_EVAL`` →
    ``EVALUATING`` → ``DONE`` | ``FAILED`` | ``PURGED``. Persists the
    ``pipeline/types.py`` Candidate (expression + lineage + trace) and, once S/E
    run, the SimResult (sim_settings/metrics/verdict).
    """

    __tablename__ = "candidate_queue"
    __table_args__ = (
        # S claim (stage='PENDING_SIM') + E claim (stage='PENDING_EVAL') +
        # lease-recycle (stage IN ('SIMULATING','EVALUATING') AND
        # lease_expires_at<now). Partial — terminal rows excluded.
        Index(
            "ix_candidate_queue_claim",
            "stage",
            "lease_expires_at",
            postgresql_where=(
                "stage IN ('PENDING_SIM', 'SIMULATING', 'PENDING_EVAL', 'EVALUATING')"
            ),
        ),
        Index("ix_candidate_queue_hyp_intent", "hyp_intent_id"),
        Index("ix_candidate_queue_task_id", "task_id"),
        Index("ix_candidate_queue_hypothesis_id", "current_hypothesis_id"),
        {"extend_existing": True},
    )

    # No index=True on the PK (see HypothesisIntent.id note).
    id = Column(Integer, primary_key=True)

    # --- lineage (hypotheses.id is THE anchor; no run_id) ---
    # All three FKs ondelete SET NULL so a parent purge (task hard-delete /
    # hyp_intent prune / hypothesis cleanup) never FK-blocks on candidate rows.
    hyp_intent_id = Column(
        Integer, ForeignKey("hyp_intent.id", ondelete="SET NULL"), nullable=True,
    )
    task_id = Column(
        Integer, ForeignKey("mining_tasks.id", ondelete="SET NULL"), nullable=True,
    )
    current_hypothesis_id = Column(
        Integer, ForeignKey("hypotheses.id", ondelete="SET NULL"), nullable=True,
    )

    # --- claim / lease machinery (consumed Phase 1b) ---
    # stage is NOT separately indexed — ix_candidate_queue_claim (composite
    # partial) serves the stage-prefixed claim/recycle scans.
    stage = Column(String(20), nullable=False, server_default="PENDING_SIM")
    claimed_by = Column(String(64), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    attempts = Column(Integer, nullable=False, server_default="0")

    # --- what S simulates ---
    expression = Column(Text, nullable=False)
    region = Column(String(10), nullable=False)
    universe = Column(String(50), nullable=True)
    delay = Column(Integer, nullable=False, server_default="1")
    dataset_id = Column(String(50), nullable=True, index=True)
    # RAG-derived category; node-read, projected to a column for attribution.
    # 200 (was 80): inferred values are <20 chars today, but BRAIN category strings
    # can reach ~203 — widened for headroom (zero FLAT impact; FLAT never writes it).
    dataset_category = Column(String(200), nullable=True)
    sim_settings = Column(JSONB(none_as_null=True), nullable=True)

    # --- role-snapshot first-class columns (终审 #7) ---
    # S reads effective_default_test_period (evaluation.py:1665); E reads
    # effective_sharpe_submit_min (evaluation.py:1936). NULL → caller's
    # getattr default (legacy User-role values) — fine, but we persist the
    # frozen snapshot so a Consultant-era intent keeps its testPeriod/gate.
    effective_default_test_period = Column(String(20), nullable=True)
    effective_sharpe_submit_min = Column(Float, nullable=True)

    # Arm provenance (reward symmetry across PASS + FAIL).
    bandit_arm = Column(String(40), nullable=True)
    rag_ab_arm = Column(String(40), nullable=True)

    # --- mutable result slots (filled by S then E) ---
    # The rest of the MiningState projection + default-OFF screen inputs
    # (_validation_findings / hypotheses); a missing key degrades the screen to
    # 'unknown', never crashes. server_default keeps create_all == migration.
    context = Column(
        JSONB(none_as_null=True), nullable=True, default=dict,
        server_default=text("'{}'::jsonb"),
    )
    # HG + S + E buffered trace steps, flushed by E in one per-candidate
    # iteration (preserves the existing per-candidate trace scoping).
    trace_records = Column(JSONB(none_as_null=True), nullable=True)
    # SimResult.metrics (sharpe/fitness/turnover/_sim_settings/...).
    sim_result = Column(JSONB(none_as_null=True), nullable=True)
    # Final verdict, projected to a column for verdict-sorted queue reads.
    verdict = Column(String(20), nullable=True)  # PASS / PROVISIONAL / FAIL / PENDING
    error = Column(Text, nullable=True)  # failure reason on FAILED stage

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )
