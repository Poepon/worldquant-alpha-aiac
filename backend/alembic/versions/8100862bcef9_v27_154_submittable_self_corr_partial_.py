"""v27_154 submittable self_corr partial expression index

Revision ID: 8100862bcef9
Revises: 08187ac90363
Create Date: 2026-05-14 23:04:04.145343

V-27.154: the "可提交" tab (list_alphas_by_tier submittable filter) and
refresh_iqc_batch(scope=submittable) both filter alphas on
CAST(metrics->>'_self_corr' AS FLOAT) — but alphas has no index on the
metrics JSONB, so the filter falls back to a sequential scan that worsens
as the table grows. Add a PARTIAL expression index whose WHERE clause
matches both call sites' shared submittable prefix
(can_submit IS TRUE AND date_submitted IS NULL).

Pure additive — one index, no data changes.

V-27.154 followup: built with CREATE INDEX CONCURRENTLY so it does not take
an ACCESS EXCLUSIVE lock on `alphas` for the duration of the build (which
would block all reads/writes on a large table). CONCURRENTLY cannot run
inside a transaction, so both upgrade and downgrade use
op.get_context().autocommit_block().
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8100862bcef9'
down_revision: Union[str, Sequence[str], None] = '08187ac90363'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.get_context().autocommit_block():
        op.create_index(
            'ix_alphas_submittable_self_corr',
            'alphas',
            [sa.text("((metrics->>'_self_corr')::float)")],
            unique=False,
            postgresql_where=sa.text(
                "can_submit IS TRUE AND date_submitted IS NULL"
            ),
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.get_context().autocommit_block():
        op.drop_index(
            'ix_alphas_submittable_self_corr',
            table_name='alphas',
            postgresql_concurrently=True,
            if_exists=True,
        )
