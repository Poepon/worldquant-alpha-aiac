"""p3_feature_flag_override_and_audit_tables

Revision ID: 41ae82a9b859
Revises: c9d4b1a82e57
Create Date: 2026-05-16 03:12:29.983475

Adds two tables that back the runtime feature flag system used by the
ops dashboard (/ops/feature-flags):

* ``feature_flag_overrides`` — one row per overridden ENABLE_* flag.
  Read by the cache refresher in backend/config.py + cleared by ops
  console "Reset" button.
* ``feature_flag_audit`` — append-only log of every flip / clear,
  rendered in the audit Drawer Timeline.

NOTE — autogenerate produced a number of spurious diffs against
historical schema drift (legacy backup tables, SMALLINT→Integer churn,
partial-index renames). Those are intentionally **not** included here;
this migration is scoped to the new tables only so the next ops engineer
can read it and immediately understand what changed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '41ae82a9b859'
down_revision: Union[str, Sequence[str], None] = 'c9d4b1a82e57'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'feature_flag_overrides',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('flag_name', sa.String(length=80), nullable=False),
        sa.Column('flag_value', sa.Text(), nullable=False),
        sa.Column('flag_type', sa.String(length=20), nullable=False, server_default='bool'),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_by', sa.String(length=64), nullable=True, server_default='system'),
        sa.Column('note', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('flag_name'),
    )

    op.create_table(
        'feature_flag_audit',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('flag_name', sa.String(length=80), nullable=False),
        sa.Column('old_value', sa.Text(), nullable=True),
        sa.Column('new_value', sa.Text(), nullable=False),
        sa.Column('action', sa.String(length=20), nullable=False),
        sa.Column('actor', sa.String(length=64), nullable=False, server_default='ops_console'),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_feature_flag_audit_flag_name'),
        'feature_flag_audit',
        ['flag_name'],
        unique=False,
    )
    op.create_index(
        op.f('ix_feature_flag_audit_created_at'),
        'feature_flag_audit',
        ['created_at'],
        unique=False,
    )
    op.create_index(
        'ix_feature_flag_audit_name_created',
        'feature_flag_audit',
        ['flag_name', 'created_at'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_feature_flag_audit_name_created', table_name='feature_flag_audit')
    op.drop_index(op.f('ix_feature_flag_audit_created_at'), table_name='feature_flag_audit')
    op.drop_index(op.f('ix_feature_flag_audit_flag_name'), table_name='feature_flag_audit')
    op.drop_table('feature_flag_audit')
    op.drop_table('feature_flag_overrides')
