"""Auto-submit audit table (2026-06-04)

Revision ID: m4a9c7e2b1f8
Revises: f32eab522c86
Create Date: 2026-06-04

One row per candidate the auto-submit beat evaluates (shadow + live). Powers the
human-review surface (shadow would-submit list) and the live submit audit trail.

Zero-risk additive:
  - Brand-new table, no FK (alpha_pk plain Integer so audit survives alpha purge).
  - inspector.has_table() guard for dev DBs using metadata.create_all().
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "m4a9c7e2b1f8"
down_revision: Union[str, Sequence[str], None] = "f32eab522c86"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "auto_submit_audit" in set(inspector.get_table_names()):
        return

    op.create_table(
        "auto_submit_audit",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("alpha_pk", sa.Integer(), nullable=False),
        sa.Column("alpha_brain_id", sa.String(20), nullable=True),
        sa.Column("region", sa.String(20), nullable=True),
        sa.Column("mode", sa.String(10), nullable=False),       # shadow | live
        sa.Column("outcome", sa.String(20), nullable=False),    # would_submit|submitted|rejected|skipped|error
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.Column(
            "gate_results",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("brain_response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("beat_run_id", sa.String(40), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_auto_submit_audit_alpha_pk", "auto_submit_audit", ["alpha_pk"])
    op.create_index("ix_auto_submit_audit_region", "auto_submit_audit", ["region"])
    op.create_index("ix_auto_submit_audit_outcome", "auto_submit_audit", ["outcome"])
    op.create_index("ix_auto_submit_audit_beat_run_id", "auto_submit_audit", ["beat_run_id"])
    op.create_index("ix_auto_submit_audit_created_at", "auto_submit_audit", ["created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "auto_submit_audit" not in set(inspector.get_table_names()):
        return

    op.drop_index("ix_auto_submit_audit_created_at", "auto_submit_audit")
    op.drop_index("ix_auto_submit_audit_beat_run_id", "auto_submit_audit")
    op.drop_index("ix_auto_submit_audit_outcome", "auto_submit_audit")
    op.drop_index("ix_auto_submit_audit_region", "auto_submit_audit")
    op.drop_index("ix_auto_submit_audit_alpha_pk", "auto_submit_audit")
    op.drop_table("auto_submit_audit")
