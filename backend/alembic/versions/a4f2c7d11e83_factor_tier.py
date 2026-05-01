"""factor_tier system: T1/T2/T3 columns + status transition audit

Adds:
  - alphas.factor_tier SMALLINT (NULL allowed; partial index where NOT NULL)
  - alphas.parent_alpha_id INTEGER FK alphas.id (lineage chain)
  - alphas.metrics_snapshot_at TIMESTAMPTZ (last refresh marker)
  - knowledge_entries.factor_tier SMALLINT (top-level column, partial index)
  - alpha_status_transitions table (append-only audit log)

Down-migration restores schema exactly.

Revision ID: a4f2c7d11e83
Revises: 81171bee8f91
Create Date: 2026-05-01 00:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a4f2c7d11e83"
down_revision: Union[str, Sequence[str], None] = "81171bee8f91"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. alphas: tier columns + lineage + snapshot timestamp
    op.add_column(
        "alphas",
        sa.Column("factor_tier", sa.SmallInteger(), nullable=True),
    )
    op.add_column(
        "alphas",
        sa.Column("parent_alpha_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "alphas",
        sa.Column("metrics_snapshot_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_alphas_parent",
        "alphas",
        "alphas",
        ["parent_alpha_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # Partial indexes — most rows will be NULL on factor_tier until backfill,
    # and lineage queries only ever target rows where parent_alpha_id IS NOT NULL.
    op.execute(
        """
        CREATE INDEX ix_alphas_factor_tier
        ON alphas (factor_tier)
        WHERE factor_tier IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX ix_alphas_parent
        ON alphas (parent_alpha_id)
        WHERE parent_alpha_id IS NOT NULL
        """
    )

    # 2. knowledge_entries: factor_tier top-level column with partial index.
    # tier is a derived attribute of pattern; classify_tier(pattern) must agree
    # (enforced at upsert layer in KnowledgeRepository).
    op.add_column(
        "knowledge_entries",
        sa.Column("factor_tier", sa.SmallInteger(), nullable=True),
    )
    op.execute(
        """
        CREATE INDEX ix_kb_factor_tier
        ON knowledge_entries (factor_tier)
        WHERE factor_tier IS NOT NULL
        """
    )

    # 3. alpha_status_transitions audit table — append-only
    op.create_table(
        "alpha_status_transitions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "alpha_id",
            sa.Integer(),
            sa.ForeignKey("alphas.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("old_status", sa.String(length=50), nullable=True),
        sa.Column("new_status", sa.String(length=50), nullable=False),
        sa.Column("sharpe_at_transition", sa.Float(), nullable=True),
        sa.Column("reason", sa.String(length=200), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=True),
        sa.Column(
            "transitioned_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_status_trans_alpha",
        "alpha_status_transitions",
        ["alpha_id", "transitioned_at"],
    )
    op.create_index(
        "ix_status_trans_time",
        "alpha_status_transitions",
        ["transitioned_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_status_trans_time", table_name="alpha_status_transitions")
    op.drop_index("ix_status_trans_alpha", table_name="alpha_status_transitions")
    op.drop_table("alpha_status_transitions")

    op.execute("DROP INDEX IF EXISTS ix_kb_factor_tier")
    op.drop_column("knowledge_entries", "factor_tier")

    op.execute("DROP INDEX IF EXISTS ix_alphas_parent")
    op.execute("DROP INDEX IF EXISTS ix_alphas_factor_tier")
    op.drop_constraint("fk_alphas_parent", "alphas", type_="foreignkey")
    op.drop_column("alphas", "metrics_snapshot_at")
    op.drop_column("alphas", "parent_alpha_id")
    op.drop_column("alphas", "factor_tier")
