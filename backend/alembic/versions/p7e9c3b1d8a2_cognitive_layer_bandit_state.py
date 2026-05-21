"""Phase 4 Tier E E1 — cognitive_layer_bandit_state table

Revision ID: p7e9c3b1d8a2
Revises: o6d4a8f2c5b7
Create Date: 2026-05-20

Per-layer Beta-Bernoulli bandit state for R8-v3 cognitive-layer
selection. One row per layer_id (7 rows). The weekly cron
``run_cognitive_layer_bandit_update`` aggregates
alpha.metrics['_cognitive_layer_used'] + PASS/FAIL into pass_count /
fail_count; node_hypothesis loads these into BanditArmStats so
COGNITIVE_LAYER_SELECT_MODE='bandit' samples from real posteriors
(was uniform-prior DOA: select_layer got an empty stats dict).

Zero-risk additive: brand-new tiny table (7 rows), inspector guard.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "p7e9c3b1d8a2"
down_revision: Union[str, Sequence[str], None] = "o6d4a8f2c5b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "cognitive_layer_bandit_state" in set(inspector.get_table_names()):
        return
    op.create_table(
        "cognitive_layer_bandit_state",
        sa.Column("layer_id", sa.String(64), primary_key=True),
        sa.Column("pass_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fail_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "cognitive_layer_bandit_state" in set(inspector.get_table_names()):
        op.drop_table("cognitive_layer_bandit_state")
