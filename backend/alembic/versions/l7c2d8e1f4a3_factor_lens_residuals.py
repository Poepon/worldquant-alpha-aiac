"""Phase 4 Sprint 2 B2 R13 — factor_lens_residuals table

Revision ID: l7c2d8e1f4a3
Revises: k6e8b2a9f3d1
Create Date: 2026-05-20

One row per (alpha_id, computed_at) — OLS decomposition of an alpha's
daily PnL series against the static factor-returns snapshot. Residual
sharpe + factor exposures + r² stamped per plan v5 §6.9 / v2 §4.6.

Zero-risk additive:
  - Brand-new table, FK to alphas ON DELETE CASCADE so alpha cleanup
    sweeps decomposition rows.
  - inspector guard for dev DBs using metadata.create_all().
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "l7c2d8e1f4a3"
down_revision: Union[str, Sequence[str], None] = "k6e8b2a9f3d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "factor_lens_residuals" in set(inspector.get_table_names()):
        return

    dialect = bind.dialect.name
    jsonb_type = (
        postgresql.JSONB(astext_type=sa.Text())
        if dialect == "postgresql"
        else sa.JSON()
    )

    op.create_table(
        "factor_lens_residuals",
        # BigInteger PK on PG; SQLite test fixtures fall through to Integer
        # (high-volume table — every PASS alpha gets a row when flag ON).
        sa.Column(
            "id",
            sa.BigInteger() if dialect == "postgresql" else sa.Integer(),
            primary_key=True,
        ),
        sa.Column(
            "alpha_id",
            sa.Integer(),
            sa.ForeignKey("alphas.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("residual_sharpe", sa.Float(), nullable=False),
        sa.Column(
            "factor_exposures",
            jsonb_type,
            nullable=False,
            server_default=sa.text("'{}'") if dialect == "postgresql" else None,
        ),
        sa.Column("r_squared", sa.Float(), nullable=True),
        sa.Column("ols_n_days", sa.Integer(), nullable=True),
        # "ols_daily" | "bucket_median" | "skipped"
        sa.Column("mode_used", sa.String(20), nullable=False),
        sa.Column("region", sa.String(10), nullable=True, index=True),
    )

    # Composite index for /ops/r13/factor-residuals time-range queries
    op.create_index(
        "ix_factor_lens_residuals_computed_at",
        "factor_lens_residuals",
        ["computed_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "factor_lens_residuals" not in set(inspector.get_table_names()):
        return
    existing_indexes = {
        ix["name"] for ix in inspector.get_indexes("factor_lens_residuals")
    }
    if "ix_factor_lens_residuals_computed_at" in existing_indexes:
        op.drop_index(
            "ix_factor_lens_residuals_computed_at",
            table_name="factor_lens_residuals",
        )
    op.drop_table("factor_lens_residuals")
