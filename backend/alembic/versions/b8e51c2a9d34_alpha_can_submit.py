"""alphas.can_submit + index for fast filter

Adds:
  - alphas.can_submit BOOLEAN NULL  (NULL = not checked yet, T/F = checked)
  - partial index on (factor_tier, can_submit) where can_submit IS NOT NULL,
    used by FactorLibrary submitted-state filter

Revision ID: b8e51c2a9d34
Revises: a4f2c7d11e83
Create Date: 2026-05-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b8e51c2a9d34"
down_revision: Union[str, Sequence[str], None] = "a4f2c7d11e83"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "alphas",
        sa.Column("can_submit", sa.Boolean(), nullable=True),
    )
    op.create_index(
        "ix_alphas_tier_can_submit",
        "alphas",
        ["factor_tier", "can_submit"],
        postgresql_where=sa.text("can_submit IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_alphas_tier_can_submit", table_name="alphas")
    op.drop_column("alphas", "can_submit")
