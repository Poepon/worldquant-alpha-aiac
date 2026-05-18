"""phase3-r1b-a: r1b_retry_log + hypothesis parent_id columns

Revision ID: d6f8a3b1e9c4
Revises: c5d9e1f3a7b8
Create Date: 2026-05-18

Per master plan §4.7 R1b + plan v1.3 §5.2 + §5.3 (~/.claude/plans/
phase3-r1b-costeer-loop-2026-05-18.md):

Cross-cutting Alembic for all R1b sub-phases. Two additive changes:

  1. NEW table ``r1b_retry_log`` — one row per CoSTEER loop firing
     (retry_impl OR mutate_hyp). Dedicated table per
     [[feedback_r1a_dedicated_log_table]] — R1b fires per FAIL alpha
     with typed attribution and lives independently of the alphas
     table. Plus 4 indexes for the cross-tab queries that drive the
     R1b GO gate.

  2. Two NEW columns on ``hypothesis`` table — ``parent_hypothesis_id``
     (FK self-reference for R1b.2 mutation chain audit) +
     ``r1b_mutation_depth`` (0 = original; bumped per mutation event).

Zero-risk additive:
  - r1b_retry_log is a brand-new table → DROP TABLE on downgrade
  - Hypothesis additions are NULLable cols with default 0 → backfill
    safe (existing rows get NULL parent + depth=0)
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "d6f8a3b1e9c4"
down_revision: Union[str, Sequence[str], None] = "c5d9e1f3a7b8"  # Q10 head
branch_labels = None
depends_on = None


def upgrade() -> None:
    # === 1. r1b_retry_log table ===
    op.create_table(
        "r1b_retry_log",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("round_idx", sa.Integer(), nullable=True),
        sa.Column("attempt_type", sa.String(20), nullable=False),
        sa.Column("triggering_attribution", sa.String(20)),
        sa.Column("triggering_attribution_source", sa.String(20)),
        sa.Column("original_expression_hash", sa.String(64)),
        sa.Column("original_alpha_id_brain", sa.String(64), nullable=True),
        sa.Column("original_hypothesis_id", sa.Integer(), nullable=True),
        sa.Column("original_quality_status", sa.String(20)),
        sa.Column("new_expression", sa.Text(), nullable=True),
        sa.Column("new_hypothesis_statement", sa.Text(), nullable=True),
        sa.Column("new_hypothesis_id", sa.Integer(), nullable=True),
        sa.Column("llm_changes_made", sa.Text(), nullable=True),
        sa.Column("outcome", sa.String(20), nullable=True),
        sa.Column("outcome_alpha_id_brain", sa.String(64), nullable=True),
        sa.Column("outcome_sharpe", sa.Float(), nullable=True),
        sa.Column("outcome_fitness", sa.Float(), nullable=True),
        sa.Column("llm_cost_usd", sa.Float(), nullable=True),
        sa.Column("llm_tokens_used", sa.Integer(), nullable=True),
        sa.Column("llm_model", sa.String(50), nullable=True),
        sa.Column("loop_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_r1b_task_id", "r1b_retry_log", ["task_id"])
    op.create_index("ix_r1b_created_at", "r1b_retry_log", ["created_at"])
    op.create_index("ix_r1b_attempt_type", "r1b_retry_log", ["attempt_type"])
    op.create_index("ix_r1b_outcome", "r1b_retry_log", ["outcome"])

    # === 2. hypothesis.parent_hypothesis_id + r1b_mutation_depth ===
    # NULL allowed → existing rows get NULL (original hypotheses with no
    # parent). r1b_mutation_depth defaults 0 = original.
    op.add_column(
        "hypothesis",
        sa.Column("parent_hypothesis_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "hypothesis",
        sa.Column(
            "r1b_mutation_depth",
            sa.Integer(),
            nullable=True,
            server_default=sa.text("0"),
        ),
    )
    # FK self-reference for audit chain — ON DELETE SET NULL so deleting
    # a root hypothesis doesn't cascade-destroy its descendant chain.
    op.create_foreign_key(
        "fk_hypothesis_parent_id",
        "hypothesis", "hypothesis",
        ["parent_hypothesis_id"], ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_hypothesis_parent_id", "hypothesis", ["parent_hypothesis_id"],
    )


def downgrade() -> None:
    # Hypothesis columns first
    op.drop_index("ix_hypothesis_parent_id", table_name="hypothesis")
    op.drop_constraint(
        "fk_hypothesis_parent_id", "hypothesis", type_="foreignkey",
    )
    op.drop_column("hypothesis", "r1b_mutation_depth")
    op.drop_column("hypothesis", "parent_hypothesis_id")
    # r1b_retry_log table + indexes
    op.drop_index("ix_r1b_outcome", table_name="r1b_retry_log")
    op.drop_index("ix_r1b_attempt_type", table_name="r1b_retry_log")
    op.drop_index("ix_r1b_created_at", table_name="r1b_retry_log")
    op.drop_index("ix_r1b_task_id", table_name="r1b_retry_log")
    op.drop_table("r1b_retry_log")
