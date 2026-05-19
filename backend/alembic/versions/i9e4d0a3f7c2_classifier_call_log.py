"""pitfall classifier call log table

Revision ID: i9e4d0a3f7c2
Revises: h8d3c9f2e1b6
Create Date: 2026-05-19

One row per `feedback_agent._classify_pitfall_error_type` decision. Powers
/ops/classifier/stats so operators can see drop rate, top noise strings,
per-region breakdown, and timeline (Major #2 follow-up from the
negative-knowledge KB pollution fix).

Zero-risk additive:
  - Brand-new table, no FK constraints into existing tables.
  - inspector.has_table() guard for dev DBs using metadata.create_all().
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "i9e4d0a3f7c2"
down_revision: Union[str, Sequence[str], None] = "h8d3c9f2e1b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "classifier_call_log" in set(inspector.get_table_names()):
        return

    op.create_table(
        "classifier_call_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("iteration", sa.Integer(), nullable=True),
        sa.Column("region", sa.String(16), nullable=True),
        sa.Column("dataset_id", sa.String(64), nullable=True),
        sa.Column("error_type", sa.String(200), nullable=True),
        sa.Column("resolved_category", sa.String(32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_classifier_call_log_task_id", "classifier_call_log", ["task_id"]
    )
    op.create_index(
        "ix_classifier_call_log_created_at", "classifier_call_log", ["created_at"]
    )
    op.create_index(
        "ix_classifier_call_log_resolved_category",
        "classifier_call_log",
        ["resolved_category"],
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_classifier_call_log_resolved_category")
    op.execute("DROP INDEX IF EXISTS ix_classifier_call_log_created_at")
    op.execute("DROP INDEX IF EXISTS ix_classifier_call_log_task_id")
    op.drop_table("classifier_call_log")
