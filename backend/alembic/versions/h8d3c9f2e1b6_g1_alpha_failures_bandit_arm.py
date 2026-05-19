"""g1-followup: alpha_failures.bandit_arm_recommended for FAIL-path G1 stamp

Revision ID: h8d3c9f2e1b6
Revises: g7c2b8e1d4a9
Create Date: 2026-05-19

G1 Phase A (8854d8f) stamped only PASS alphas (Alpha.metrics JSONB key
"_direction_bandit_recommended_arm"). G1 follow-up (2afbcb2) extended
the stamp to the buffered PASS-path too. Both paths leave FAIL alphas
unstamped, so /ops/direction-bandit/telemetry's per-arm pass-rate uses a
PASS-only denominator — half-blind Bayesian posterior.

This revision adds the symmetric column to AlphaFailure so FAIL rows
can carry the same provenance. workflow.run_with_persistence stamps
both PASS (Alpha.metrics) and FAIL (alpha_failures.bandit_arm_recommended)
using the same _g1_bandit_arm value read once per batch.

Schema choice rationale:
  - AlphaFailure has no metrics JSONB column (unlike Alpha) — adding one
    just for this would be a large schema change. A plain VARCHAR(40)
    column matches the bandit arm name shape (max DIRECTION_BANDIT_ARMS
    label is "knowledge_pattern" = 17 chars). Indexed for the per-arm
    GROUP BY in /ops/direction-bandit/telemetry.
  - String(40) chosen for ~2x headroom over the longest expected arm
    name without overcommitting JSONB indirection cost.

Zero-risk additive:
  - Brand-new NULLable column → existing rows stay NULL → telemetry SQL
    treats them as "(none)" bucket (same pattern as the rest of the
    direction-bandit endpoint).
  - inspector.has_column() guard for dev DBs using metadata.create_all().
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "h8d3c9f2e1b6"
down_revision: Union[str, Sequence[str], None] = "g7c2b8e1d4a9"  # G5 crossover_log head
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "alpha_failures" not in set(inspector.get_table_names()):
        # Defensive — table should exist (legacy), bail if not so test fixtures
        # using metadata.create_all() at startup will pick it up via the model
        return

    existing_cols = {c["name"] for c in inspector.get_columns("alpha_failures")}
    if "bandit_arm_recommended" not in existing_cols:
        op.add_column(
            "alpha_failures",
            sa.Column("bandit_arm_recommended", sa.String(40), nullable=True),
        )

    existing_indexes = {ix["name"] for ix in inspector.get_indexes("alpha_failures")}
    if "ix_alpha_failures_bandit_arm_recommended" not in existing_indexes:
        op.create_index(
            "ix_alpha_failures_bandit_arm_recommended",
            "alpha_failures",
            ["bandit_arm_recommended"],
        )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_alpha_failures_bandit_arm_recommended")
    op.drop_column("alpha_failures", "bandit_arm_recommended")
