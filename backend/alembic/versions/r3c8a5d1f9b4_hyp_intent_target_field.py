"""Orthogonal-breadth field steering: hyp_intent.target_field

Adds ``target_field VARCHAR(200) NULL`` to ``hyp_intent`` (PR-B, 2026-06-09).
Additive + ONLINE-SAFE (nullable). Set by the scheduler when ENABLE_FIELD_
SCREENING is ON (explore-fraction of intents); the HG generation node steers
code-gen around it. NULL = legacy no-steer → INERT until the new code/flag.

Revision ID: r3c8a5d1f9b4
Revises: r2b7f4c9a1e3
Create Date: 2026-06-09
"""
from alembic import op


revision = "r3c8a5d1f9b4"
down_revision = "r2b7f4c9a1e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE hyp_intent ADD COLUMN IF NOT EXISTS target_field VARCHAR(200)")


def downgrade() -> None:
    op.execute("ALTER TABLE hyp_intent DROP COLUMN IF EXISTS target_field")
