"""phase2-r5: extend r1a_attribution_log with 10 R5 columns + 2 indexes

Revision ID: 7e9f2a1b3c4d
Revises: 3b1c4e5d6a78
Create Date: 2026-05-18

Zero-risk additive migration per plan v1.0 §4.4:
- 10 new NULLABLE columns on r1a_attribution_log (no backfill needed)
- 2 NULL-safe indexes for GO gate SQL
- Existing 273 R1a rows get NULL for r5 cols, byte-equivalent for SELECTs not
  referencing r5_*
- PostgreSQL ALTER TABLE ADD COLUMN is metadata-only for nullable cols → 0 lock
  time on existing rows
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7e9f2a1b3c4d"
down_revision: Union[str, Sequence[str], None] = "3b1c4e5d6a78"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # === 10 new R5 columns on r1a_attribution_log ===
    op.add_column("r1a_attribution_log", sa.Column("r5_c1_aligned", sa.String(8), nullable=True))
    op.add_column("r1a_attribution_log", sa.Column("r5_c1_confidence", sa.Float(), nullable=True))
    op.add_column("r1a_attribution_log", sa.Column("r5_c1_reason", sa.Text(), nullable=True))
    op.add_column("r1a_attribution_log", sa.Column("r5_c2_aligned", sa.String(8), nullable=True))
    op.add_column("r1a_attribution_log", sa.Column("r5_c2_confidence", sa.Float(), nullable=True))
    op.add_column("r1a_attribution_log", sa.Column("r5_c2_reason", sa.Text(), nullable=True))
    op.add_column("r1a_attribution_log", sa.Column("r5_composite_score", sa.Float(), nullable=True))
    op.add_column("r1a_attribution_log", sa.Column("r5_agrees_r1a", sa.String(8), nullable=True))
    op.add_column("r1a_attribution_log", sa.Column("r5_hook_error", sa.Text(), nullable=True))
    op.add_column("r1a_attribution_log", sa.Column("r5_cost_usd", sa.Float(), nullable=True))

    # === 2 indexes for GO gate SQL ===
    op.create_index("ix_r1a_r5_c1_aligned", "r1a_attribution_log", ["r5_c1_aligned"])
    op.create_index("ix_r1a_r5_composite", "r1a_attribution_log", ["r5_composite_score"])


def downgrade() -> None:
    op.drop_index("ix_r1a_r5_composite", table_name="r1a_attribution_log")
    op.drop_index("ix_r1a_r5_c1_aligned", table_name="r1a_attribution_log")
    op.drop_column("r1a_attribution_log", "r5_cost_usd")
    op.drop_column("r1a_attribution_log", "r5_hook_error")
    op.drop_column("r1a_attribution_log", "r5_agrees_r1a")
    op.drop_column("r1a_attribution_log", "r5_composite_score")
    op.drop_column("r1a_attribution_log", "r5_c2_reason")
    op.drop_column("r1a_attribution_log", "r5_c2_confidence")
    op.drop_column("r1a_attribution_log", "r5_c2_aligned")
    op.drop_column("r1a_attribution_log", "r5_c1_reason")
    op.drop_column("r1a_attribution_log", "r5_c1_confidence")
    op.drop_column("r1a_attribution_log", "r5_c1_aligned")
