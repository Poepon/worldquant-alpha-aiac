"""phase3-r1b-a: r1b_retry_log + hypothesis parent_id columns

Revision ID: d6f8a3b1e9c4
Revises: c5d9e1f3a7b8
Create Date: 2026-05-18

Per master plan §4.7 R1b + plan v1.3 §5.2 + §5.3 (~/.claude/plans/
phase3-r1b-costeer-loop-2026-05-18.md):

Cross-cutting Alembic for all R1b sub-phases. Two additive changes:

  1. NEW table ``r1b_retry_log`` — one row per CoSTEER loop firing
     (retry_impl OR mutate_hyp). Dedicated table per
     [[feedback_r1a_dedicated_log_table]] — R1b fires per FAIL alpha
     with typed attribution and lives independently of the alphas
     table. Plus 4 indexes for the cross-tab queries that drive the
     R1b GO gate.

  2. Two NEW columns on ``hypothesis`` table — ``parent_hypothesis_id``
     (FK self-reference for R1b.2 mutation chain audit) +
     ``r1b_mutation_depth`` (0 = original; bumped per mutation event).

Zero-risk additive:
  - r1b_retry_log is a brand-new table → DROP TABLE on downgrade
  - Hypothesis additions are NULLable cols with default 0 → backfill
    safe (existing rows get NULL parent + depth=0)
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "d6f8a3b1e9c4"
down_revision: Union[str, Sequence[str], None] = "c5d9e1f3a7b8"  # Q10 head
branch_labels = None
depends_on = None


def upgrade() -> None:
    # === 1. r1b_retry_log table ===
    # inspector.has_table() guard — dev DBs created via
    # database.init_db()'s metadata.create_all() fallback already have the
    # table; without this guard `alembic upgrade head` raises DuplicateTable.
    # Same lesson as 7a3f9e1c2b8d phase15-A + c5d9e1f3a7b8 Q10 PR1b. Surfaced
    # by test_alembic_chain_pg.py XFAIL tests (eae52fa).
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    existing_indexes = (
        set(ix["name"] for ix in inspector.get_indexes("r1b_retry_log"))
        if "r1b_retry_log" in existing_tables
        else set()
    )

    if "r1b_retry_log" not in existing_tables:
        op.create_table(
            "r1b_retry_log",
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("task_id", sa.Integer(), nullable=True),
            sa.Column("round_idx", sa.Integer(), nullable=True),
            sa.Column("attempt_type", sa.String(20), nullable=False),
            sa.Column("triggering_attribution", sa.String(20)),
            sa.Column("triggering_attribution_source", sa.String(20)),
            sa.Column("original_expression_hash", sa.String(64)),
            sa.Column("original_alpha_id_brain", sa.String(64), nullable=True),
            sa.Column("original_hypothesis_id", sa.Integer(), nullable=True),
            sa.Column("original_quality_status", sa.String(20)),
            sa.Column("new_expression", sa.Text(), nullable=True),
            sa.Column("new_hypothesis_statement", sa.Text(), nullable=True),
            sa.Column("new_hypothesis_id", sa.Integer(), nullable=True),
            sa.Column("llm_changes_made", sa.Text(), nullable=True),
            sa.Column("outcome", sa.String(20), nullable=True),
            sa.Column("outcome_alpha_id_brain", sa.String(64), nullable=True),
            sa.Column("outcome_sharpe", sa.Float(), nullable=True),
            sa.Column("outcome_fitness", sa.Float(), nullable=True),
            sa.Column("llm_cost_usd", sa.Float(), nullable=True),
            sa.Column("llm_tokens_used", sa.Integer(), nullable=True),
            sa.Column("llm_model", sa.String(50), nullable=True),
            sa.Column("loop_error", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )

    for ix_name, ix_cols in (
        ("ix_r1b_task_id", ["task_id"]),
        ("ix_r1b_created_at", ["created_at"]),
        ("ix_r1b_attempt_type", ["attempt_type"]),
        ("ix_r1b_outcome", ["outcome"]),
    ):
        if ix_name not in existing_indexes:
            op.create_index(ix_name, "r1b_retry_log", ix_cols)

    # === 2. hypothesis.parent_hypothesis_id + r1b_mutation_depth ===
    # 2026-05-18 retrofit: the original add_column calls targeted table
    # "hypothesis" (singular) — but the real ORM __tablename__ is
    # "hypotheses" (plural, created by c7f9e21b3a47). Against a real PG
    # this block crashed with `relation "hypothesis" does not exist`,
    # blocking the entire migration chain. The forward fix
    # (a7d2f9e4b8c3, R1b-D hotfix) adds the columns to the correct
    # `hypotheses` table with IF NOT EXISTS guards. To unblock the
    # chain and let alembic actually REACH a7d2f9e4b8c3, this revision
    # is now a Postgres-side IF EXISTS no-op against the wrong table.
    # All cleanup + correct DDL lives in a7d2f9e4b8c3.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_name = 'hypothesis'
          ) THEN
            ALTER TABLE hypothesis
              ADD COLUMN IF NOT EXISTS parent_hypothesis_id INTEGER NULL;
            ALTER TABLE hypothesis
              ADD COLUMN IF NOT EXISTS r1b_mutation_depth INTEGER NULL DEFAULT 0;
            -- FK + index added below only when the columns landed
            IF NOT EXISTS (
              SELECT 1 FROM pg_constraint WHERE conname = 'fk_hypothesis_parent_id'
            ) THEN
              ALTER TABLE hypothesis
                ADD CONSTRAINT fk_hypothesis_parent_id
                FOREIGN KEY (parent_hypothesis_id)
                REFERENCES hypothesis (id)
                ON DELETE SET NULL;
            END IF;
            CREATE INDEX IF NOT EXISTS ix_hypothesis_parent_id
              ON hypothesis (parent_hypothesis_id);
          END IF;
          -- If table "hypothesis" doesn't exist (the typical case),
          -- silently skip; a7d2f9e4b8c3 will add the columns to the
          -- correct plural table.
        END $$;
        """
    )


def downgrade() -> None:
    # Hypothesis columns first — defensive IF EXISTS to match the
    # idempotent upgrade pattern (the wrong-table block was a no-op on
    # missing `hypothesis`; nothing to undo there).
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
              DROP COLUMN IF EXISTS r1b_mutation_depth;
            ALTER TABLE hypothesis
              DROP COLUMN IF EXISTS parent_hypothesis_id;
          END IF;
        END $$;
        """
    )
    # r1b_retry_log table + indexes — symmetric guards mirror the
    # upgrade's inspector-based DuplicateTable guard so partially-applied
    # downgrade is safe.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "r1b_retry_log" in set(inspector.get_table_names()):
        existing_indexes = set(
            ix["name"] for ix in inspector.get_indexes("r1b_retry_log")
        )
        for ix_name in (
            "ix_r1b_outcome",
            "ix_r1b_attempt_type",
            "ix_r1b_created_at",
            "ix_r1b_task_id",
        ):
            if ix_name in existing_indexes:
                op.drop_index(ix_name, table_name="r1b_retry_log")
        op.drop_table("r1b_retry_log")
