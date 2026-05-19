"""Phase 4 Sprint 1 A2 — R14 task_stop_loss_events table

Revision ID: j5b1a7e3c2f4
Revises: i9e4d0a3f7c2
Create Date: 2026-05-19

One row per R14 stop_loss trigger event. Powers /ops/task-stop-loss/recent +
the audit trail of which tasks the EMA / consecutive_zero policy paused.

Zero-risk additive:
  - Brand-new table, FK to mining_tasks (ON DELETE CASCADE so task purge
    sweeps events).
  - inspector.has_table() guard for dev DBs using metadata.create_all().
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "j5b1a7e3c2f4"
down_revision: Union[str, Sequence[str], None] = "i9e4d0a3f7c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "task_stop_loss_events" in set(inspector.get_table_names()):
        return

    op.create_table(
        "task_stop_loss_events",
        # Integer (4-byte) matches the model — stop_loss events are
        # low-volume (a handful per task lifetime). Keeps SQLite test
        # fixtures auto-incrementing without explicit autoincrement=True.
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "task_id",
            sa.Integer(),
            sa.ForeignKey("mining_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "triggered_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # 'pass_rate_floor' = EMA dipped below TASK_STOP_LOSS_PASS_RATE_FLOOR
        # 'consecutive_zero' = N consecutive rounds with 0 PASS alpha
        # 'manual_override' = ops console clear / debug
        sa.Column("trigger_reason", sa.String(40), nullable=False),
        sa.Column("ema_pass_rate", sa.Float(), nullable=True),
        sa.Column("consecutive_zero_rounds", sa.Integer(), nullable=True),
        sa.Column("rounds_completed", sa.Integer(), nullable=True),
        sa.Column("ema_window_pass_count", sa.Integer(), nullable=True),
        sa.Column(
            "meta_data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_task_stop_loss_task_id",
        "task_stop_loss_events",
        ["task_id"],
    )
    op.create_index(
        "ix_task_stop_loss_triggered_at",
        "task_stop_loss_events",
        ["triggered_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "task_stop_loss_events" not in set(inspector.get_table_names()):
        return

    op.drop_index("ix_task_stop_loss_triggered_at", "task_stop_loss_events")
    op.drop_index("ix_task_stop_loss_task_id", "task_stop_loss_events")
    op.drop_table("task_stop_loss_events")
