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
    """Downgrade schema.

    R1b.3-v2 review LOW 1 fix (2026-05-18, mirrors PR1a M5 commit 09fe704):
      The upgrade() guards the ``hypotheses`` column / FK / index additions
      with ``IF NOT EXISTS`` because:
        * ``parent_hypothesis_id`` is declared on the ORM model (Phase 2 era,
          V-27.B comment) and may have landed on the table via
          ``metadata.create_all()`` dev fallback BEFORE this migration ran.
        * ``r1b_mutation_depth`` is added in R1b.3-v2 alongside this hotfix
          but the ORM declaration similarly lets ``metadata.create_all()``
          land it in dev DBs without Alembic.
      We cannot tell from this revision whether a given DB had those columns
      pre-existing (and thus holding live R1b chain data) or whether this
      revision added them. To stay data-safe we DELIBERATELY DO NOT drop
      the two columns on downgrade — dropping them would wipe production
      / dev parent_hypothesis_id + r1b_mutation_depth history.

      If you actually want them gone, drop them manually:
        DROP INDEX IF EXISTS ix_hypotheses_parent_id;
        ALTER TABLE hypotheses DROP CONSTRAINT IF EXISTS fk_hypotheses_parent_id;
        ALTER TABLE hypotheses DROP COLUMN IF EXISTS r1b_mutation_depth;
        ALTER TABLE hypotheses DROP COLUMN IF EXISTS parent_hypothesis_id;

      The cleanup of the broken d6f8a3b1e9c4 singular-table artifacts in
      upgrade() is forward-only — there is nothing to revert.
    """
    import logging
    logger = logging.getLogger("alembic.runtime.migration")
    logger.warning(
        "[a7d2f9e4b8c3 downgrade / R1b.3-v2 review LOW 1 guard] NOT dropping "
        "hypotheses.parent_hypothesis_id / hypotheses.r1b_mutation_depth / "
        "fk_hypotheses_parent_id / ix_hypotheses_parent_id — the ORM model "
        "declares both columns so they may have been created via "
        "metadata.create_all() dev fallback before this revision Alembic-"
        "formalized them. Drop manually if needed."
    )

    # R1b.3-v2 review LOW 1: do NOT drop the columns / FK / index — preserves
    # any R1b chain data that landed via ORM metadata.create_all(). Operator
    # must drop manually if desired. Same asymmetry pattern as PR1a M5 fix
    # (commit 09fe704, revision 7a3f9e1c2b8d).
    op.execute("-- downgrade no-op per asymmetry pattern, see PR1a M5 (commit 09fe704)")
