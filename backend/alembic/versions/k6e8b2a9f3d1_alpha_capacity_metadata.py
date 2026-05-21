"""Phase 4 Sprint 2 B1 R11 — alpha_capacity_metadata

Revision ID: k6e8b2a9f3d1
Revises: k6f8a3d2c1b9
Create Date: 2026-05-20

Adds USD capacity estimate column to alphas table per plan v5 §6.8 / v2
§4.5. PASS alpha persist time stamps capacity_usd_estimate via
backend.services.capacity_estimator.estimate.

Zero-risk additive:
  - New nullable Float column — existing rows stay NULL.
  - Partial index on non-NULL only — keeps /ops/r11/capacity-stats range
    scans cheap without inflating index size for the 99% of rows that
    are NULL (only PASS alphas with ENABLE_CAPACITY_SCORE=True at sim
    time get stamped).
  - inspector guard so dev DBs using metadata.create_all() don't double-
    apply.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "k6e8b2a9f3d1"
down_revision: Union[str, Sequence[str], None] = "k6f8a3d2c1b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("alphas")}

    if "capacity_usd_estimate" not in cols:
        op.add_column(
            "alphas",
            sa.Column("capacity_usd_estimate", sa.Float(), nullable=True),
        )

    # Partial index — only non-NULL rows. PostgreSQL syntax; SQLite test
    # fixtures fall back to a non-partial index (still useful).
    dialect = bind.dialect.name
    existing_indexes = {ix["name"] for ix in inspector.get_indexes("alphas")}
    if "ix_alphas_capacity_usd" not in existing_indexes:
        if dialect == "postgresql":
            op.create_index(
                "ix_alphas_capacity_usd",
                "alphas",
                ["capacity_usd_estimate"],
                postgresql_where=sa.text("capacity_usd_estimate IS NOT NULL"),
            )
        else:
            op.create_index(
                "ix_alphas_capacity_usd",
                "alphas",
                ["capacity_usd_estimate"],
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_indexes = {ix["name"] for ix in inspector.get_indexes("alphas")}
    if "ix_alphas_capacity_usd" in existing_indexes:
        op.drop_index("ix_alphas_capacity_usd", table_name="alphas")

    cols = {c["name"] for c in inspector.get_columns("alphas")}
    if "capacity_usd_estimate" in cols:
        op.drop_column("alphas", "capacity_usd_estimate")
