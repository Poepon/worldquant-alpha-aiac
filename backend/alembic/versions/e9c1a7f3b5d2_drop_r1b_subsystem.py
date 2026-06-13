"""drop R1b subsystem schema (r1b_retry_log table + hypotheses r1b columns)

The R1b CoSTEER retry/mutate loop was retired 2026-06-13 (code removed: r1b_loop /
r1b_persistence / prompts / failure_tree / workflow wiring / ops endpoints / flags).
This forward migration drops its data-bearing schema:
  - table  r1b_retry_log              (867 historical rows — last write 2026-06-05,
                                        before the pool cutover; no live writer)
  - column hypotheses.parent_hypothesis_id  (CoSTEER mutation-chain FK backbone)
  - column hypotheses.r1b_mutation_depth    (chain depth counter)

Postgres DROP COLUMN cascades the FK constraint + index automatically.
Downgrade recreates the structure (schema only — dropped data is NOT restored).

Revision ID: e9c1a7f3b5d2
Revises: r3c8a5d1f9b4
Create Date: 2026-06-13
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "e9c1a7f3b5d2"
down_revision = "r3c8a5d1f9b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop the two R1b mutation-chain columns on hypotheses (Postgres auto-drops
    # the parent_hypothesis_id FK constraint + any index with the column).
    op.drop_column("hypotheses", "r1b_mutation_depth")
    op.drop_column("hypotheses", "parent_hypothesis_id")
    # Drop the dedicated retry/mutate outcome log table.
    op.drop_table("r1b_retry_log")


def downgrade() -> None:
    # Recreate r1b_retry_log (schema only; historical rows are not restored).
    op.create_table(
        "r1b_retry_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("round_idx", sa.Integer(), nullable=True),
        sa.Column("attempt_type", sa.String(), nullable=False),
        sa.Column("triggering_attribution", sa.String(), nullable=True),
        sa.Column("triggering_attribution_source", sa.String(), nullable=True),
        sa.Column("original_expression_hash", sa.String(), nullable=True),
        sa.Column("original_alpha_id_brain", sa.String(), nullable=True),
        sa.Column("original_hypothesis_id", sa.Integer(), nullable=True),
        sa.Column("original_quality_status", sa.String(), nullable=True),
        sa.Column("new_expression", sa.Text(), nullable=True),
        sa.Column("new_hypothesis_statement", sa.Text(), nullable=True),
        sa.Column("new_hypothesis_id", sa.Integer(), nullable=True),
        sa.Column("llm_changes_made", sa.Text(), nullable=True),
        sa.Column("outcome", sa.String(), nullable=True),
        sa.Column("outcome_alpha_id_brain", sa.String(), nullable=True),
        sa.Column("outcome_sharpe", sa.Float(), nullable=True),
        sa.Column("outcome_fitness", sa.Float(), nullable=True),
        sa.Column("llm_cost_usd", sa.Float(), nullable=True),
        sa.Column("llm_tokens_used", sa.Integer(), nullable=True),
        sa.Column("llm_model", sa.String(), nullable=True),
        sa.Column("loop_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
    )
    # Recreate the hypotheses mutation-chain columns.
    op.add_column(
        "hypotheses",
        sa.Column("parent_hypothesis_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "hypotheses_parent_hypothesis_id_fkey", "hypotheses", "hypotheses",
        ["parent_hypothesis_id"], ["id"], ondelete="SET NULL",
    )
    op.add_column(
        "hypotheses",
        sa.Column("r1b_mutation_depth", sa.Integer(), nullable=True, server_default="0"),
    )
