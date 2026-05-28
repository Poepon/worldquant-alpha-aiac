"""phase16-A: optimization closure Stage A — optimization_runs + alphas links

Revision ID: f32eab522c86
Revises: c3f9a7d2e4b8, s1c7e9a2d4b8
Create Date: 2026-05-28

Merge-and-add: at session start the chain had two unmerged heads —
``c3f9a7d2e4b8`` (phase15-D drop legacy cascade cols, 2026-05-18) and
``s1c7e9a2d4b8`` (dataset/datafield cell-stats normalization, 2026-05-26;
both already in master via commit 6a37cb7). This revision joins them so
``alembic upgrade head`` (singular) resolves going forward.

Stage A of the optimization closure plan (docs/optimization_closure_plan_v1_2026-05-28.md).

Adds:
  * ``optimization_runs`` table — one row per OptimizationService cycle
    (open → variants simulated → winners persisted → submit decisions → finish).
    Stage A's GO/STOP gate (14d conversion rate ≥ 20%) is computed via this
    table; alphas.metrics JSONB would make the same query un-indexable.

  * ``alphas.optimization_run_id`` — FK back-link from optimization-produced
    rows to their cycle. NULL for mining-origin alphas (the historical 99%).

  * ``alphas.parent_alpha_family_id`` — root of the parent_alpha_id chain.
    Self.id for root rows, parent.parent_alpha_family_id otherwise. Used by
    Stage A's dedup so we don't re-spawn variants from the same lineage on
    every cycle. Backfilled once here via WITH RECURSIVE.

  * ``alphas.expression_hash`` — added IF NOT EXISTS as a safety net. The
    ORM has declared the column for a while; production DBs already have
    it filled (mining_agent + workflow write on every PASS). Guard is for
    fresh / minimally-migrated dev DBs that bypassed the original add.

Inspector guards mirror b2e5c9f1d847 / c3f9a7d2e4b8 so the migration is
idempotent + replay-safe (matters for the in-memory sqlite test fixture
that runs metadata.create_all() before Alembic).

Downgrade re-removes the columns + table; family_id backfill values are
NOT restored (they're derived from parent_alpha_id which is preserved).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "f32eab522c86"
down_revision: Union[str, Sequence[str], None] = ("c3f9a7d2e4b8", "s1c7e9a2d4b8")
branch_labels = None
depends_on = None


def _is_postgres(bind) -> bool:
    return bind.dialect.name == "postgresql"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # ---- optimization_runs table -------------------------------------------
    if "optimization_runs" not in existing_tables:
        op.create_table(
            "optimization_runs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "parent_alpha_id",
                sa.Integer(),
                sa.ForeignKey("alphas.id"),
                nullable=False,
            ),
            sa.Column("generator_name", sa.String(length=64), nullable=False),
            sa.Column("trigger_source", sa.String(length=32), nullable=False),
            sa.Column(
                "n_variants", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column(
                "n_winners", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column(
                "n_submitted", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column(
                "sim_budget_used",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column("sim_budget_granted", sa.Integer(), nullable=False),
            sa.Column(
                "cycle_started_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column("cycle_finished_at", sa.DateTime(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column(
                "cycle_metadata",
                JSONB() if _is_postgres(bind) else sa.JSON(),
                nullable=True,
                server_default=sa.text("'{}'::jsonb")
                if _is_postgres(bind)
                else sa.text("'{}'"),
            ),
        )

    existing_indexes_opt = (
        set(ix["name"] for ix in inspector.get_indexes("optimization_runs"))
        if "optimization_runs" in set(inspector.get_table_names())
        else set()
    )
    for ix_name, ix_cols in (
        ("ix_opt_runs_parent", ["parent_alpha_id"]),
        ("ix_opt_runs_started", ["cycle_started_at"]),
    ):
        if ix_name not in existing_indexes_opt:
            op.create_index(ix_name, "optimization_runs", ix_cols)

    # ---- alphas: new columns -----------------------------------------------
    existing_alpha_cols = (
        {c["name"] for c in inspector.get_columns("alphas")}
        if "alphas" in set(inspector.get_table_names())
        else set()
    )

    # expression_hash safety net (ORM has it; DB may not in fresh installs)
    if "expression_hash" not in existing_alpha_cols:
        op.add_column(
            "alphas",
            sa.Column("expression_hash", sa.String(length=64), nullable=True),
        )

    if "optimization_run_id" not in existing_alpha_cols:
        op.add_column(
            "alphas",
            sa.Column(
                "optimization_run_id",
                sa.Integer(),
                sa.ForeignKey("optimization_runs.id"),
                nullable=True,
            ),
        )

    if "parent_alpha_family_id" not in existing_alpha_cols:
        op.add_column(
            "alphas",
            sa.Column(
                "parent_alpha_family_id",
                sa.Integer(),
                sa.ForeignKey("alphas.id"),
                nullable=True,
            ),
        )

    # ---- alphas: new indexes (partial where supported by PG) ---------------
    existing_alpha_ix = {
        ix["name"] for ix in inspector.get_indexes("alphas")
    }
    if _is_postgres(bind):
        if "ix_alphas_expr_hash" not in existing_alpha_ix:
            op.execute(
                "CREATE INDEX IF NOT EXISTS ix_alphas_expr_hash "
                "ON alphas(expression_hash) WHERE expression_hash IS NOT NULL"
            )
        if "ix_alphas_opt_run" not in existing_alpha_ix:
            op.execute(
                "CREATE INDEX IF NOT EXISTS ix_alphas_opt_run "
                "ON alphas(optimization_run_id) "
                "WHERE optimization_run_id IS NOT NULL"
            )
        if "ix_alphas_family" not in existing_alpha_ix:
            op.execute(
                "CREATE INDEX IF NOT EXISTS ix_alphas_family "
                "ON alphas(parent_alpha_family_id) "
                "WHERE parent_alpha_family_id IS NOT NULL"
            )
    else:
        # sqlite test fixture — full indexes are fine
        for ix_name, ix_cols in (
            ("ix_alphas_expr_hash", ["expression_hash"]),
            ("ix_alphas_opt_run", ["optimization_run_id"]),
            ("ix_alphas_family", ["parent_alpha_family_id"]),
        ):
            if ix_name not in existing_alpha_ix:
                op.create_index(ix_name, "alphas", ix_cols)

    # ---- one-shot backfill: parent_alpha_family_id via WITH RECURSIVE -----
    # Roots (parent_alpha_id IS NULL) seed family_id = self.id. Descendants
    # inherit chain.family_id one hop at a time. PG-specific; sqlite test
    # fixture has nothing to backfill (empty table).
    if _is_postgres(bind):
        op.execute(
            """
            WITH RECURSIVE chain AS (
                SELECT id, id AS family_id
                FROM alphas
                WHERE parent_alpha_id IS NULL
                UNION ALL
                SELECT a.id, c.family_id
                FROM alphas a
                JOIN chain c ON a.parent_alpha_id = c.id
            )
            UPDATE alphas
            SET parent_alpha_family_id = chain.family_id
            FROM chain
            WHERE alphas.id = chain.id
              AND alphas.parent_alpha_family_id IS NULL;
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # alphas — drop indexes + columns we added
    if "alphas" in existing_tables:
        existing_alpha_ix = {ix["name"] for ix in inspector.get_indexes("alphas")}
        for ix_name in (
            "ix_alphas_family",
            "ix_alphas_opt_run",
            "ix_alphas_expr_hash",
        ):
            if ix_name in existing_alpha_ix:
                op.drop_index(ix_name, table_name="alphas")
        existing_alpha_cols = {c["name"] for c in inspector.get_columns("alphas")}
        for col in (
            "parent_alpha_family_id",
            "optimization_run_id",
            # expression_hash is intentionally NOT dropped — it predates this
            # migration in any non-trivial DB; safer to leave it.
        ):
            if col in existing_alpha_cols:
                op.drop_column("alphas", col)

    # optimization_runs — drop
    if "optimization_runs" in existing_tables:
        existing_opt_ix = {
            ix["name"] for ix in inspector.get_indexes("optimization_runs")
        }
        for ix_name in ("ix_opt_runs_started", "ix_opt_runs_parent"):
            if ix_name in existing_opt_ix:
                op.drop_index(ix_name, table_name="optimization_runs")
        op.drop_table("optimization_runs")
