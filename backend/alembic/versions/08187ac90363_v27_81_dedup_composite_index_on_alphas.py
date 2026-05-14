"""v27_81 dedup composite index on alphas

Revision ID: 08187ac90363
Revises: 13a3a7a97b56
Create Date: 2026-05-14 22:06:09.031720

V-27.81: filter_unsimulated_expressions (selection_strategy.py) SELECTs
alphas by (expression_hash, region, universe) but there is no index on that
triple — it falls back to ix_alphas_task_expr_hash (task-keyed) or a scan.
Add a NON-UNIQUE partial composite index to speed up the dedup query.

NOT a unique constraint: expression_hash is a pure-expression md5 (no delay/
decay/neutralization), so the same expression under different settings is a
legitimately distinct alpha — a unique constraint would reject it. The 48
existing duplicate groups (59 excess rows) are intentionally left in place.
In-flight simulate dedup is handled by the Redis claim_simulate_slot lock;
data correctness by the existing alpha_id unique constraint.

Pure additive — one index, no data changes.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '08187ac90363'
down_revision: Union[str, Sequence[str], None] = '13a3a7a97b56'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index(
        'ix_alphas_exprhash_region_universe',
        'alphas',
        ['expression_hash', 'region', 'universe'],
        unique=False,
        postgresql_where=sa.text('expression_hash IS NOT NULL'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_alphas_exprhash_region_universe', table_name='alphas')
