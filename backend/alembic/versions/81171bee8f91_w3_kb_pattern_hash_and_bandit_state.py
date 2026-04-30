"""w3_kb_pattern_hash_and_bandit_state

Adds:
  - knowledge_entries.pattern_hash VARCHAR(32) + UNIQUE INDEX ix_kb_pattern_hash
    Backfilled with frozen formula
    sha256(strip(pattern) + '|' + region + '|' + dataset_id)[:32]
    where region/dataset_id come from meta_data JSONB.
  - bandit_state table for cost-aware bandit persistence
    (region, dataset_id) PK + pulls/total_reward/sim_count_today/last_reset

Revision ID: 81171bee8f91
Revises: ddd301be2e08
Create Date: 2026-04-30 13:18:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "81171bee8f91"
down_revision: Union[str, Sequence[str], None] = "ddd301be2e08"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add pattern_hash column nullable first so backfill can run
    op.add_column(
        "knowledge_entries",
        sa.Column("pattern_hash", sa.String(length=32), nullable=True),
    )

    # 2. Backfill via PostgreSQL-native sha256 (pgcrypto). Formula MUST
    # match backend.models.knowledge.compute_pattern_hash exactly.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute(
        """
        UPDATE knowledge_entries
        SET pattern_hash = SUBSTRING(
            encode(
                digest(
                    BTRIM(COALESCE(pattern, ''))
                    || '|' || COALESCE(meta_data->>'region', '')
                    || '|' || COALESCE(meta_data->>'dataset_id', meta_data->>'dataset', ''),
                    'sha256'
                ),
                'hex'
            ),
            1, 32
        )
        WHERE pattern_hash IS NULL
        """
    )

    # 3. Disambiguate any duplicate pattern_hash so the UNIQUE index can be
    # added. Keep the oldest row (smallest id), suffix the rest and mark
    # them inactive.
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   ROW_NUMBER() OVER (PARTITION BY pattern_hash ORDER BY id ASC) AS rn
            FROM knowledge_entries
            WHERE pattern_hash IS NOT NULL
        )
        UPDATE knowledge_entries kb
        SET is_active = FALSE,
            pattern_hash = SUBSTRING(kb.pattern_hash, 1, 24)
                          || '_dup_' || LPAD(kb.id::text, 6, '0')
        FROM ranked
        WHERE kb.id = ranked.id AND ranked.rn > 1
        """
    )

    # 4. Now safe to add UNIQUE index
    op.create_index(
        "ix_kb_pattern_hash",
        "knowledge_entries",
        ["pattern_hash"],
        unique=True,
    )

    # 5. bandit_state table
    op.create_table(
        "bandit_state",
        sa.Column("region", sa.String(length=10), primary_key=True, nullable=False),
        sa.Column("dataset_id", sa.String(length=100), primary_key=True, nullable=False),
        sa.Column("pulls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_reward", sa.Float(), nullable=False, server_default="0"),
        sa.Column("sim_count_today", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "last_reset",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("bandit_state")
    op.drop_index("ix_kb_pattern_hash", table_name="knowledge_entries")
    op.drop_column("knowledge_entries", "pattern_hash")
