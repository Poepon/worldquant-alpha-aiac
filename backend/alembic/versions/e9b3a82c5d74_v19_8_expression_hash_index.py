"""V-19.8 expression_hash partial index for RESUME dedup

Adds partial index on (task_id, expression_hash) for RESUME path —
when V-19.4 resume_session needs to check whether an expression has
already been persisted in the current task (skip BRAIN sim on resume),
this index makes the lookup O(log n) instead of full task scan.

Partial: only index rows where expression_hash IS NOT NULL (legacy
rows from before W3 may have NULL hash).

Revision ID: e9b3a82c5d74
Revises: d8a2f15b9c63
Create Date: 2026-05-10
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e9b3a82c5d74"
down_revision: Union[str, Sequence[str], None] = "d8a2f15b9c63"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_alphas_task_expr_hash",
        "alphas",
        ["task_id", "expression_hash"],
        postgresql_where=sa.text("expression_hash IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_alphas_task_expr_hash", table_name="alphas")
