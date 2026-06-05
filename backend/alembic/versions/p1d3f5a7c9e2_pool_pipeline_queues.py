"""Pool pipeline queues — hyp_intent + candidate_queue + alpha_failures.metrics

Revision ID: p1d3f5a7c9e2
Revises: m4a9c7e2b1f8
Create Date: 2026-06-05

Phase 0 foundation of the four-pool decoupling
(docs/four_pool_decoupling_plan_2026-06-05.md). Zero behaviour change — these
objects are INERT until Phase 1b wires the resident HG/S/E pools.

Additive only:
  - hyp_intent       (new) — HG pool claim source; carries frozen config_snapshot.
  - candidate_queue  (new) — HG→S→E lease queue; persists Candidate + SimResult.
  - alpha_failures.metrics (new JSONB col) — E pool persists FAIL signal payload.

No run_id FK on the new tables (lineage anchors on hypotheses.id);
experiment_runs is left untouched — its retire/keep decision is plan §6 open-Q1,
resolved as option (a) "read-only legacy", deferred to >= Phase 1d. alphas.run_id
/ trace_steps.run_id stay nullable. All FKs use ondelete=SET NULL so a parent
purge never FK-blocks on queue rows.

Idempotent: inspector guards on every object so dev DBs that already ran
SQLAlchemyBase.metadata.create_all() (init_db / conftest fixtures) upgrade
cleanly. Partial indexes are dialect-guarded (PostgreSQL only; SQLite gets a
plain composite index).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "p1d3f5a7c9e2"
down_revision: Union[str, Sequence[str], None] = "m4a9c7e2b1f8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    dialect = bind.dialect.name
    existing = set(inspector.get_table_names())

    # ---- hyp_intent (HG claim source) ----
    if "hyp_intent" not in existing:
        op.create_table(
            "hyp_intent",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "task_id",
                sa.Integer(),
                sa.ForeignKey("mining_tasks.id", ondelete="SET NULL"),
                nullable=True,
            ),
            # claim / lease
            sa.Column("stage", sa.String(20), server_default="PENDING", nullable=False),
            sa.Column("claimed_by", sa.String(64), nullable=True),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
            # generation scope
            sa.Column("region", sa.String(10), nullable=False),
            sa.Column("universe", sa.String(50), nullable=True),
            sa.Column("dataset_id", sa.String(50), nullable=True),
            sa.Column("delay", sa.Integer(), server_default="1", nullable=False),
            sa.Column("fanout", sa.Integer(), nullable=True),
            sa.Column("bandit_arm", sa.String(40), nullable=True),
            sa.Column("rag_ab_arm", sa.String(40), nullable=True),
            # frozen config
            sa.Column(
                "config_snapshot",
                postgresql.JSONB(astext_type=sa.Text()),
                server_default=sa.text("'{}'::jsonb"),
                nullable=False,
            ),
            sa.Column("prompt_version", sa.String(100), nullable=True),
            sa.Column("thresholds_version", sa.String(100), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False,
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False,
            ),
        )
        if dialect == "postgresql":
            op.create_index(
                "ix_hyp_intent_claim",
                "hyp_intent",
                ["stage", "lease_expires_at"],
                postgresql_where=sa.text("stage IN ('PENDING', 'CLAIMED')"),
            )
        else:
            op.create_index("ix_hyp_intent_claim", "hyp_intent", ["stage", "lease_expires_at"])
        op.create_index("ix_hyp_intent_task_id", "hyp_intent", ["task_id"])
        op.create_index("ix_hyp_intent_dataset_id", "hyp_intent", ["dataset_id"])

    # ---- candidate_queue (HG→S→E lease queue; FK → hyp_intent) ----
    if "candidate_queue" not in existing:
        op.create_table(
            "candidate_queue",
            sa.Column("id", sa.Integer(), primary_key=True),
            # lineage (hypotheses.id is the anchor; no run_id). All FKs SET NULL.
            sa.Column(
                "hyp_intent_id",
                sa.Integer(),
                sa.ForeignKey("hyp_intent.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "task_id",
                sa.Integer(),
                sa.ForeignKey("mining_tasks.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "current_hypothesis_id",
                sa.Integer(),
                sa.ForeignKey("hypotheses.id", ondelete="SET NULL"),
                nullable=True,
            ),
            # claim / lease
            sa.Column("stage", sa.String(20), server_default="PENDING_SIM", nullable=False),
            sa.Column("claimed_by", sa.String(64), nullable=True),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
            # what S simulates
            sa.Column("expression", sa.Text(), nullable=False),
            sa.Column("region", sa.String(10), nullable=False),
            sa.Column("universe", sa.String(50), nullable=True),
            sa.Column("delay", sa.Integer(), server_default="1", nullable=False),
            sa.Column("dataset_id", sa.String(50), nullable=True),
            sa.Column("dataset_category", sa.String(80), nullable=True),
            sa.Column("sim_settings", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            # role-snapshot first-class columns (终审 #7)
            sa.Column("effective_default_test_period", sa.String(20), nullable=True),
            sa.Column("effective_sharpe_submit_min", sa.Float(), nullable=True),
            sa.Column("bandit_arm", sa.String(40), nullable=True),
            sa.Column("rag_ab_arm", sa.String(40), nullable=True),
            # mutable result slots
            sa.Column(
                "context",
                postgresql.JSONB(astext_type=sa.Text()),
                server_default=sa.text("'{}'::jsonb"),
                nullable=True,
            ),
            sa.Column("trace_records", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("sim_result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("verdict", sa.String(20), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False,
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False,
            ),
        )
        if dialect == "postgresql":
            op.create_index(
                "ix_candidate_queue_claim",
                "candidate_queue",
                ["stage", "lease_expires_at"],
                postgresql_where=sa.text(
                    "stage IN ('PENDING_SIM', 'SIMULATING', 'PENDING_EVAL', 'EVALUATING')"
                ),
            )
        else:
            op.create_index(
                "ix_candidate_queue_claim", "candidate_queue", ["stage", "lease_expires_at"],
            )
        op.create_index("ix_candidate_queue_hyp_intent", "candidate_queue", ["hyp_intent_id"])
        op.create_index("ix_candidate_queue_task_id", "candidate_queue", ["task_id"])
        op.create_index(
            "ix_candidate_queue_hypothesis_id", "candidate_queue", ["current_hypothesis_id"],
        )
        op.create_index("ix_candidate_queue_dataset_id", "candidate_queue", ["dataset_id"])

    # ---- alpha_failures.metrics (E pool persists FAIL signal payload) ----
    if "alpha_failures" in existing:
        cols = {c["name"] for c in inspector.get_columns("alpha_failures")}
        if "metrics" not in cols:
            op.add_column(
                "alpha_failures",
                sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    # drop the added column first
    if "alpha_failures" in existing:
        cols = {c["name"] for c in inspector.get_columns("alpha_failures")}
        if "metrics" in cols:
            op.drop_column("alpha_failures", "metrics")

    # candidate_queue before hyp_intent (FK dependency)
    if "candidate_queue" in existing:
        op.drop_index("ix_candidate_queue_dataset_id", "candidate_queue")
        op.drop_index("ix_candidate_queue_hypothesis_id", "candidate_queue")
        op.drop_index("ix_candidate_queue_task_id", "candidate_queue")
        op.drop_index("ix_candidate_queue_hyp_intent", "candidate_queue")
        op.drop_index("ix_candidate_queue_claim", "candidate_queue")
        op.drop_table("candidate_queue")

    if "hyp_intent" in existing:
        op.drop_index("ix_hyp_intent_dataset_id", "hyp_intent")
        op.drop_index("ix_hyp_intent_task_id", "hyp_intent")
        op.drop_index("ix_hyp_intent_claim", "hyp_intent")
        op.drop_table("hyp_intent")
