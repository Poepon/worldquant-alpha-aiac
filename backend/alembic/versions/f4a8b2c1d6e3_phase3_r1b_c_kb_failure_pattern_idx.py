"""phase3-r1b-c: partial index on KnowledgeEntry.pattern WHERE FAILURE_PITFALL

Revision ID: f4a8b2c1d6e3
Revises: e7a2c4b9d5f1
Create Date: 2026-05-18

R1b.3 review LOW (2026-05-18). ``record_failure_tree`` in
``backend/knowledge_extraction.py`` does:

    SELECT ... FROM knowledge_entries
     WHERE pattern = :pattern AND entry_type = :entry_type

to dedupe before UPSERTing the failure tree. ``KnowledgeEntry.pattern``
has NO index today — the only existing pattern-related index is
``ix_kb_pattern_hash`` (UNIQUE on ``pattern_hash``) which is irrelevant
here. At <10k rows a SeqScan is fine; production KB already sits at
~3k entries and is growing, so this is a future bottleneck.

Partial index rationale (vs full composite ``(entry_type, pattern)``):
  - FAILURE_PITFALL is ~10-20% of total KB rows → index is ~5-10x smaller
  - ``record_failure_tree`` is the only caller filtering on
    ``entry_type='FAILURE_PITFALL'`` AND ``pattern=?`` together
  - Smaller index = less write overhead on every KB INSERT
  - Other paths (SUCCESS_PATTERN lookups in rag_service.py /
    feedback_agent.py) can be optimized separately if needed; this fix
    is intentionally surgical per R1b.3 review scope

Zero-risk additive:
  - No table lock (CREATE INDEX IF NOT EXISTS, idempotent re-run safe)
  - No data backfill required
  - Downgrade drops only the index
"""
from typing import Sequence, Union

from alembic import op


revision: str = "f4a8b2c1d6e3"
down_revision: Union[str, Sequence[str], None] = "e7a2c4b9d5f1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS guards against idempotent re-run + tests that
    # share the same DB state across pytest sessions.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_kb_failure_pattern "
        "ON knowledge_entries (pattern) "
        "WHERE entry_type = 'FAILURE_PITFALL'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_kb_failure_pattern")
