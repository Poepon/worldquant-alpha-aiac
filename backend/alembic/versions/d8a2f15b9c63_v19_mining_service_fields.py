"""V-19 mining service mode fields

Adds 4 columns to mining_tasks for persistent mining service mode:
  - mining_mode VARCHAR(30) DEFAULT 'DISCRETE'  ('DISCRETE' or 'CONTINUOUS_CASCADE')
  - cascade_phase VARCHAR(10) NULL  ('T1'/'T2'/'T3'/'IDLE')
  - cascade_round_idx INT DEFAULT 0
  - last_alpha_persisted_at TIMESTAMPTZ NULL  (watchdog liveness signal)

Plus partial index on active CONTINUOUS_CASCADE sessions per region (singleton constraint).

Revision ID: d8a2f15b9c63
Revises: c7f9e21b3a47
Create Date: 2026-05-10
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d8a2f15b9c63"
down_revision: Union[str, Sequence[str], None] = "c7f9e21b3a47"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "mining_tasks",
        sa.Column("mining_mode", sa.String(30), nullable=False, server_default="DISCRETE"),
    )
    op.add_column(
        "mining_tasks",
        sa.Column("cascade_phase", sa.String(10), nullable=True),
    )
    op.add_column(
        "mining_tasks",
        sa.Column("cascade_round_idx", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "mining_tasks",
        sa.Column("last_alpha_persisted_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Singleton constraint: at most 1 RUNNING/PAUSED CONTINUOUS_CASCADE task per region.
    # Watchdog + service start endpoints rely on this to prevent duplicate workers.
    op.create_index(
        "ix_mining_tasks_active_cascade_per_region",
        "mining_tasks",
        ["region"],
        unique=True,
        postgresql_where=sa.text(
            "mining_mode = 'CONTINUOUS_CASCADE' AND status IN ('RUNNING', 'PAUSED')"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_mining_tasks_active_cascade_per_region", table_name="mining_tasks")
    op.drop_column("mining_tasks", "last_alpha_persisted_at")
    op.drop_column("mining_tasks", "cascade_round_idx")
    op.drop_column("mining_tasks", "cascade_phase")
    op.drop_column("mining_tasks", "mining_mode")
