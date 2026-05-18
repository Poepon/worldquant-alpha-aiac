"""phase3-r1b-d: hotfix R1b.1a migration — wrong table name

Revision ID: a7d2f9e4b8c3
Revises: f4a8b2c1d6e3
Create Date: 2026-05-18

**Root cause** (discovered 2026-05-18 during R1b.3-v2 design): R1b.1a init
migration ``d6f8a3b1e9c4`` added ``parent_hypothesis_id`` + ``r1b_mutation_depth``
to a table called ``hypothesis`` (singular). The actual ORM table per
``backend/models/hypothesis.py`` is ``hypotheses`` (plural — confirmed by
``c7f9e21b3a47_phase2_b1_hypotheses_table.py`` which created the table).

Effect: when R1b.1a runs against PostgreSQL it errors with ``relation
"hypothesis" does not exist`` and the columns never land on the correct
table. The ORM model carries ``parent_hypothesis_id`` (Phase 2 era,
pre-existing per V-27.B comment) so reads/writes silently fail at the
DB layer if the column truly is missing.

Fix:
  1. Best-effort drop of the broken artifacts on ``hypothesis`` (singular)
     using IF EXISTS guards so this migration is safe whether the previous
     run succeeded, partially applied, or completely failed.
  2. Add the columns to ``hypotheses`` (plural) — the real table — with
     identical types + FK self-ref + index, matching the original R1b.1a
     intent.

Why not amend d6f8a3b1e9c4 in-place: that revision may have been applied
in a dev/staging DB; rewriting an applied migration breaks alembic_version
tracking. A new forward-only fix preserves the migration history.

Zero data impact: only new nullable columns + new FK/index. Existing
hypotheses rows stay untouched; their ``parent_hypothesis_id`` defaults
to NULL = original-root (consistent with R1b semantics where the
exploration LLM's first-round hypotheses are the chain roots).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a7d2f9e4b8c3"
down_revision: Union[str, Sequence[str], None] = "f4a8b2c1d6e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # === 1. Best-effort cleanup of broken d6f8a3b1e9c4 artifacts on
    #        the wrong (singular) table. Wrapped in IF EXISTS so this is
    #        a no-op on the (likely majority) case where d6f8a3b1e9c4
    #        crashed before any DDL landed. ===
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_name = 'hypothesis'
          ) THEN
            DROP INDEX IF EXISTS ix_hypothesis_parent_id;
            ALTER TABLE hypothesis
              DROP CONSTRAINT IF EXISTS fk_hypothesis_parent_id;
            ALTER TABLE hypothesis
              DROP COLUMN IF EXISTS parent_hypothesis_id;
            ALTER TABLE hypothesis
              DROP COLUMN IF EXISTS r1b_mutation_depth;
          END IF;
        END $$;
        """
    )

    # === 2. Add columns to the CORRECT (plural) table. The ORM model
    #        already declares parent_hypothesis_id; r1b_mutation_depth
    #        is added by this revision and lands in the ORM in the same
    #        PR (see backend/models/hypothesis.py). ===
    # IF NOT EXISTS guards make this idempotent — if a previous run via
    # raw psql succeeded, this is a clean no-op.
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'hypotheses'
              AND column_name = 'parent_hypothesis_id'
          ) THEN
            ALTER TABLE hypotheses
              ADD COLUMN parent_hypothesis_id INTEGER NULL;
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'hypotheses'
              AND column_name = 'r1b_mutation_depth'
          ) THEN
            ALTER TABLE hypotheses
              ADD COLUMN r1b_mutation_depth INTEGER NULL DEFAULT 0;
          END IF;
        END $$;
        """
    )

    # FK self-ref + index. Skip if already present to stay idempotent.
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'fk_hypotheses_parent_id'
          ) THEN
            ALTER TABLE hypotheses
              ADD CONSTRAINT fk_hypotheses_parent_id
              FOREIGN KEY (parent_hypothesis_id)
              REFERENCES hypotheses (id)
              ON DELETE SET NULL;
          END IF;
        END $$;
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_hypotheses_parent_id "
        "ON hypotheses (parent_hypothesis_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_hypotheses_parent_id")
    op.execute(
        "ALTER TABLE hypotheses "
        "DROP CONSTRAINT IF EXISTS fk_hypotheses_parent_id"
    )
    op.execute(
        "ALTER TABLE hypotheses "
        "DROP COLUMN IF EXISTS r1b_mutation_depth"
    )
    op.execute(
        "ALTER TABLE hypotheses "
        "DROP COLUMN IF EXISTS parent_hypothesis_id"
    )
