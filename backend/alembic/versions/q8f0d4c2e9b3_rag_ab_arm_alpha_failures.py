"""rag-ab: alpha_failures.rag_ab_arm for RAG category-overlap A/B FAIL-path stamp

Revision ID: q8f0d4c2e9b3
Revises: p7e9c3b1d8a2
Create Date: 2026-05-21

RAG category-overlap A/B harness (2026-05-21) measures whether P0's
dataset-category-overlap retrieval improves mining. The "real BRAIN sim"
denominator is dominated by alpha_failures (~40:1 vs alphas), so FAIL rows
must carry the per-round arm to compute PASS-per-sim by arm. Symmetric with
Alpha.metrics["_rag_ab_arm"] on the PASS path. Mirrors the G1
alpha_failures.bandit_arm_recommended column (h8d3c9f2e1b6).

Zero-risk additive: brand-new NULLable VARCHAR(40) column; existing rows stay
NULL → report treats them as outside the A/B window. inspector guard for dev
DBs using metadata.create_all().
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "q8f0d4c2e9b3"
down_revision: Union[str, Sequence[str], None] = "p7e9c3b1d8a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "alpha_failures" not in set(inspector.get_table_names()):
        return

    existing_cols = {c["name"] for c in inspector.get_columns("alpha_failures")}
    if "rag_ab_arm" not in existing_cols:
        op.add_column(
            "alpha_failures",
            sa.Column("rag_ab_arm", sa.String(40), nullable=True),
        )

    existing_indexes = {ix["name"] for ix in inspector.get_indexes("alpha_failures")}
    if "ix_alpha_failures_rag_ab_arm" not in existing_indexes:
        op.create_index(
            "ix_alpha_failures_rag_ab_arm",
            "alpha_failures",
            ["rag_ab_arm"],
        )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_alpha_failures_rag_ab_arm")
    op.drop_column("alpha_failures", "rag_ab_arm")
