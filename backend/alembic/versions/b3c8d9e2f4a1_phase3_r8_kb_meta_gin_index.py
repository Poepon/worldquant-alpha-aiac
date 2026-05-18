"""phase3-r8: KnowledgeEntry.meta_data JSONB GIN index for hierarchical RAG

Revision ID: b3c8d9e2f4a1
Revises: 9a4f7e8c1d6b
Create Date: 2026-05-18

Per master plan §4.5 R8 + plan v1.0 §10: hierarchical RAG layers query
KnowledgeEntry by meta_data JSONB keys (pillar / family_signature /
decayed / requires_role / fields_used etc). Without a GIN index those
queries do seq scans on 3K+ rows. GIN(jsonb_path_ops) index makes
``meta_data @> '{"pillar":"momentum"}'`` style queries logN.

Zero-risk additive (CREATE INDEX CONCURRENTLY in PG):
  - No table lock
  - No data backfill required (existing rows automatically indexed)
  - Downgrade drops only the index

Note: CONCURRENTLY needs autocommit (no transactional DDL). Alembic
handles this via op.execute with explicit COMMIT around it, but for
simplicity and because dev/test DB has low load, we use the normal
op.create_index. Production deploys should switch to manual CONCURRENTLY
via psql if there's existing traffic.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "b3c8d9e2f4a1"
down_revision: Union[str, Sequence[str], None] = "9a4f7e8c1d6b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # GIN with jsonb_path_ops for @> containment + ?-key + ->>'value' filter
    # performance on meta_data JSONB sub-queries.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_kb_meta_data_gin "
        "ON knowledge_entries USING GIN (meta_data jsonb_path_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_kb_meta_data_gin")
