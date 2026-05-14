"""add hypothesis_round_stats table (V-27.92)

Revision ID: 13a3a7a97b56
Revises: d3a8f1b27c95
Create Date: 2026-05-14 20:48:49.508572

V-27.92: per-hypothesis per-round outcome detail — the authoritative input
for should_abandon_hypothesis, replacing the in-memory
state.hypothesis_round_history that was lost on worker restart / Celery
task-boundary switch.

Pure additive — one new table + its indexes, NO data backfill. Pre-migration
PROPOSED/ACTIVE hypotheses have no detail rows, so should_abandon returns
False for them until N fresh rounds accumulate (semantically "re-observe"
— safer than reading lost in-memory history; alpha rows carry no
round_index so a precise backfill is impossible anyway).

NOTE: alembic autogenerate also surfaced a pile of unrelated pre-existing
model-vs-DB drift (backup tables, SMALLINT/Integer mismatches, index renames,
incl. ix_mining_tasks_active_cascade_per_region). That drift is deliberately
NOT included here — this revision only touches hypothesis_round_stats.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '13a3a7a97b56'
down_revision: Union[str, Sequence[str], None] = 'd3a8f1b27c95'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'hypothesis_round_stats',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('hypothesis_id', sa.Integer(), nullable=False),
        sa.Column('task_id', sa.Integer(), nullable=False),
        sa.Column('round_index', sa.Integer(), nullable=False),
        sa.Column('alpha_count', sa.Integer(), nullable=False),
        sa.Column('pass_count', sa.Integer(), nullable=False),
        sa.Column('syntax_fail_count', sa.Integer(), nullable=False),
        sa.Column('simulate_fail_count', sa.Integer(), nullable=False),
        sa.Column('quality_fail_count', sa.Integer(), nullable=False),
        sa.Column('flip_alpha_count', sa.Integer(), nullable=False),
        sa.Column('flip_pass_count', sa.Integer(), nullable=False),
        sa.Column('retryable_count', sa.Integer(), nullable=False),
        sa.Column('attribution', sa.String(length=20), nullable=True),
        sa.Column('attribution_reason', sa.Text(), nullable=True),
        sa.Column('best_sharpe', sa.Float(), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['hypothesis_id'], ['hypotheses.id'], ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(['task_id'], ['mining_tasks.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_hrs_hid_round', 'hypothesis_round_stats',
        ['hypothesis_id', 'round_index'], unique=False,
    )
    op.create_index(
        op.f('ix_hypothesis_round_stats_hypothesis_id'),
        'hypothesis_round_stats', ['hypothesis_id'], unique=False,
    )
    op.create_index(
        op.f('ix_hypothesis_round_stats_id'),
        'hypothesis_round_stats', ['id'], unique=False,
    )
    op.create_index(
        op.f('ix_hypothesis_round_stats_task_id'),
        'hypothesis_round_stats', ['task_id'], unique=False,
    )
    # Uniqueness key for upsert-on-checkpoint-replay (B5 LangGraph replay).
    op.create_index(
        'uq_hrs_hid_round_task', 'hypothesis_round_stats',
        ['hypothesis_id', 'round_index', 'task_id'], unique=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('uq_hrs_hid_round_task', table_name='hypothesis_round_stats')
    op.drop_index(
        op.f('ix_hypothesis_round_stats_task_id'),
        table_name='hypothesis_round_stats',
    )
    op.drop_index(
        op.f('ix_hypothesis_round_stats_id'),
        table_name='hypothesis_round_stats',
    )
    op.drop_index(
        op.f('ix_hypothesis_round_stats_hypothesis_id'),
        table_name='hypothesis_round_stats',
    )
    op.drop_index('ix_hrs_hid_round', table_name='hypothesis_round_stats')
    op.drop_table('hypothesis_round_stats')
