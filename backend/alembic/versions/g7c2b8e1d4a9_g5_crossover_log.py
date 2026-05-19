"""g5-phase-a: g5_crossover_log per-call crossover telemetry table

Revision ID: g7c2b8e1d4a9
Revises: f5b8a3c7d2e1
Create Date: 2026-05-19

G5 Phase A (light wiring) per master plan + memory
[[reference_competitive_analysis_ai_alpha_mining]] §3 trajectory mutation:

NEW table ``g5_crossover_log`` — one row per llm_crossover_alpha call.
parent_a / parent_b FK to alphas.id, offspring_expressions JSONB.
outcome_alpha_ids back-filled by next round when offspring PASS.

Zero-risk additive:
  - Brand-new table → DROP TABLE on downgrade
  - inspector.has_table() guard for dev DBs using
    metadata.create_all() startup fallback
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "g7c2b8e1d4a9"
down_revision: Union[str, Sequence[str], None] = "f5b8a3c7d2e1"  # G2 llm_call_log head
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    existing_indexes = (
        set(ix["name"] for ix in inspector.get_indexes("g5_crossover_log"))
        if "g5_crossover_log" in existing_tables
        else set()
    )

    if "g5_crossover_log" not in existing_tables:
        op.create_table(
            "g5_crossover_log",
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("task_id", sa.Integer(), nullable=True),
            sa.Column("run_id", sa.Integer(), nullable=True),
            sa.Column("round_idx", sa.Integer(), nullable=True),
            sa.Column("region", sa.String(10), nullable=True),
            sa.Column(
                "parent_a_alpha_id",
                sa.Integer(),
                sa.ForeignKey("alphas.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "parent_b_alpha_id",
                sa.Integer(),
                sa.ForeignKey("alphas.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("parent_a_sharpe", sa.Float(), nullable=True),
            sa.Column("parent_b_sharpe", sa.Float(), nullable=True),
            sa.Column("parent_a_pillar", sa.String(20), nullable=True),
            sa.Column("parent_b_pillar", sa.String(20), nullable=True),
            sa.Column("offspring_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "offspring_expressions",
                sa.dialects.postgresql.JSONB(astext_type=sa.Text()),
                nullable=True,
            ),
            sa.Column("llm_model", sa.String(50), nullable=True),
            sa.Column("llm_cost_usd", sa.Float(), nullable=True),
            sa.Column("llm_tokens_used", sa.Integer(), nullable=True),
            sa.Column(
                "outcome_alpha_ids",
                sa.dialects.postgresql.JSONB(astext_type=sa.Text()),
                nullable=True,
            ),
            sa.Column("outcome_pass_count", sa.Integer(), nullable=True),
            sa.Column("error_kind", sa.String(40), nullable=True),
            sa.Column("write_error", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )

    for ix_name, ix_cols in (
        ("ix_g5_task_id", ["task_id"]),
        ("ix_g5_created_at", ["created_at"]),
        ("ix_g5_parent_a", ["parent_a_alpha_id"]),
        ("ix_g5_parent_b", ["parent_b_alpha_id"]),
    ):
        if ix_name not in existing_indexes:
            op.create_index(ix_name, "g5_crossover_log", ix_cols)


def downgrade() -> None:
    for ix_name in (
        "ix_g5_parent_b",
        "ix_g5_parent_a",
        "ix_g5_created_at",
        "ix_g5_task_id",
    ):
        op.execute(f'DROP INDEX IF EXISTS {ix_name}')
    op.drop_table("g5_crossover_log")
