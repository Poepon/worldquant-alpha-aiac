"""V-25.B alpha_failures.hypothesis_id column for attribution chain

V-24.A audit revealed B6 should_abandon_hypothesis attribution data was
fragmented because FAIL alphas (written to alpha_failures) had no
hypothesis_id link — only PASS alphas (alphas table) had the column.
Adding the column closes the attribution loop so B5 v2 / B6 can read
PASS + FAIL counts per hypothesis from a single key.

NULLABLE + ON DELETE SET NULL — historical rows stay NULL (no
backfill possible since round→hypothesis mapping isn't preserved
elsewhere), hypothesis row cleanup never cascades into FAIL audit
trail.

Revision ID: f1c4e83d72a6
Revises: e9b3a82c5d74
Create Date: 2026-05-13
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f1c4e83d72a6"
down_revision: Union[str, Sequence[str], None] = "e9b3a82c5d74"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "alpha_failures",
        sa.Column("hypothesis_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_alpha_failures_hypothesis_id",
        "alpha_failures",
        "hypotheses",
        ["hypothesis_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_alpha_failures_hypothesis_id",
        "alpha_failures",
        ["hypothesis_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_alpha_failures_hypothesis_id", table_name="alpha_failures")
    op.drop_constraint(
        "fk_alpha_failures_hypothesis_id", "alpha_failures", type_="foreignkey"
    )
    op.drop_column("alpha_failures", "hypothesis_id")
