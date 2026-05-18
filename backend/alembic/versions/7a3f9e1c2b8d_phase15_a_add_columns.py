"""phase15_a_add_columns_and_alembic_formalize_phase1_tables

Revision ID: 7a3f9e1c2b8d
Revises: 41ae82a9b859
Create Date: 2026-05-17

Phase 1.5-A (plan v1.3 §1). Pure additive migration — zero existing
column / row changes → zero risk to alpha behavior or running tasks.

Adds 4 columns:
  * mining_tasks.schedule        String(20)  default 'ONESHOT'
  * mining_tasks.starting_tier   Integer     default 1
  * mining_tasks.generation_strategy JSONB   default '["llm"]'::jsonb
  * experiment_runs.runtime_state    JSONB   default '{}'::jsonb

Alembic-formalizes 2 Phase 1-shipped dedicated tables (currently created
via metadata.create_all() dev fallback, missing from Alembic head):
  * direction_bandit_log  (Phase 1 R2/Q7 off-policy log)
  * ast_distance_log      (Phase 1 R3/Q8 AST distance log)

Each create_table() is guarded by inspector.has_table() so dev DBs that
already have the table via metadata.create_all() are not double-created.

Plan v1.3 fix MF-V1.4-1/2: all JSONB server_default uses sa.text("'X'::jsonb")
form with explicit ::jsonb cast (asyncpg requirement — without cast the
column would be 'text' type literal not jsonb).

Plan v1.3 fix V1.2-B4: SQLAlchemy model side in backend/models/task.py
ALSO declares Python-side default= for each new column, so ORM
constructors (MiningTask(...)) in 21 test fixture files don't fail
NOT NULL pre-flight check.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '7a3f9e1c2b8d'
down_revision: Union[str, Sequence[str], None] = '41ae82a9b859'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # === Part 1: 4 new columns ===
    op.add_column(
        "mining_tasks",
        sa.Column(
            "schedule",
            sa.String(20),
            nullable=False,
            server_default="ONESHOT",
        ),
    )
    op.add_column(
        "mining_tasks",
        sa.Column(
            "starting_tier",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.add_column(
        "mining_tasks",
        sa.Column(
            "generation_strategy",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[\"llm\"]'::jsonb"),
        ),
    )
    op.add_column(
        "experiment_runs",
        sa.Column(
            "runtime_state",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )

    # === Part 2: Alembic-formalize Phase 1 dedicated log tables ===
    # inspector.has_table() guard — dev DBs already have them via
    # metadata.create_all() dev fallback (Phase 1 shipped without Alembic).
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "direction_bandit_log" not in existing_tables:
        op.create_table(
            "direction_bandit_log",
            sa.Column("id", sa.BigInteger(), nullable=False, primary_key=True, autoincrement=True),
            sa.Column("task_id", sa.Integer(), nullable=True),
            sa.Column("round_idx", sa.Integer(), nullable=True),
            sa.Column("segment_id", sa.String(128), nullable=False),
            sa.Column("region", sa.String(50), nullable=True),
            sa.Column("dataset_category", sa.String(100), nullable=True),
            sa.Column("failure_pattern", sa.String(32), nullable=True),
            sa.Column("selected_arm", sa.String(64), nullable=False),
            sa.Column("observed_reward", sa.Float(), nullable=True),
            sa.Column("cold_start", sa.String(8), nullable=True),
            sa.Column("sampled_arm_probs", postgresql.JSONB(), nullable=True),
            sa.Column("bandit_version", sa.String(8), nullable=True, server_default="v1"),
            sa.Column("write_error", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )
        op.create_index("ix_dbl_task_id", "direction_bandit_log", ["task_id"])
        op.create_index("ix_dbl_segment_id", "direction_bandit_log", ["segment_id"])
        op.create_index("ix_dbl_created_at", "direction_bandit_log", ["created_at"])

    if "ast_distance_log" not in existing_tables:
        op.create_table(
            "ast_distance_log",
            sa.Column("id", sa.BigInteger(), nullable=False, primary_key=True, autoincrement=True),
            sa.Column("task_id", sa.Integer(), nullable=True),
            sa.Column("round_idx", sa.Integer(), nullable=True),
            sa.Column("expression", sa.Text(), nullable=False),
            sa.Column("expression_hash", sa.String(64), nullable=True),
            sa.Column("skeleton", sa.Text(), nullable=True),
            sa.Column("ast_distance_min", sa.Float(), nullable=True),
            sa.Column("ast_distance_mean", sa.Float(), nullable=True),
            sa.Column("ast_distance_max", sa.Float(), nullable=True),
            sa.Column("nearest_neighbor_hash", sa.String(64), nullable=True),
            sa.Column("history_window", sa.Integer(), nullable=True),
            sa.Column("tracker_version", sa.String(8), nullable=True, server_default="v1"),
            sa.Column("write_error", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )
        op.create_index("ix_adl_task_id", "ast_distance_log", ["task_id"])
        op.create_index("ix_adl_created_at", "ast_distance_log", ["created_at"])
        op.create_index("ix_adl_expression_hash", "ast_distance_log", ["expression_hash"])


def downgrade() -> None:
    """Downgrade schema.

    Bug M5 fix (review 2026-05-18):
      The upgrade() guards `create_table` for `direction_bandit_log` and
      `ast_distance_log` with `inspector.has_table()` because Phase 1 shipped
      those tables via `metadata.create_all()` dev fallback BEFORE Alembic
      formalized them here. We cannot tell from this revision whether a given
      DB had those tables pre-existing (and thus holding Phase 1 R1a / AST
      data) or whether this revision created them. To stay data-safe we
      DELIBERATELY DO NOT drop those two tables on downgrade — dropping them
      would wipe production / dev R1a attribution + AST distance history.

      If you actually want them gone, drop them manually:
        DROP TABLE ast_distance_log;
        DROP TABLE direction_bandit_log;
      Or re-run `python backend/migrations/init_database.py` to let
      `metadata.create_all()` recreate empty shells.

    The 4 added columns ARE safely reverted — those are guaranteed to have
    been added by this revision (no pre-existing variant in the wild).
    """
    import logging
    logger = logging.getLogger("alembic.runtime.migration")
    logger.warning(
        "[phase15-A downgrade / Bug M5 guard] NOT dropping direction_bandit_log / "
        "ast_distance_log — Phase 1 shipped them via metadata.create_all() before "
        "this revision Alembic-formalized them, so we cannot distinguish "
        "pre-existing data from data created post-upgrade. Drop manually if needed."
    )

    # Bug M5: do NOT drop the 2 dedicated log tables — preserves Phase 1
    # R1a / AST data on downgrade. Operator must drop manually if desired.
    # The 4 column reverts below remain safe (additive-only upgrade).

    op.drop_column("experiment_runs", "runtime_state")
    op.drop_column("mining_tasks", "generation_strategy")
    op.drop_column("mining_tasks", "starting_tier")
    op.drop_column("mining_tasks", "schedule")
