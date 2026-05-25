"""datasets/datafields normalization: split definition vs per-(delay,universe) cell stats

Revision ID: s1c7e9a2d4b8
Revises: r9a1c5e3b7f2
Create Date: 2026-05-26

Multi-cell breadth refactor. Mirrors BRAIN's data-sets / data-fields model (a
region-scoped *definition* + a ``data[]`` array of per-(region, delay, universe)
*cell* statistics) so mining can target (universe x delay) cells beyond the single
USA/TOP3000/delay=1 slice we have ever explored.

In-place repurpose (lowest-risk: ``datasets.id`` PK never changes, so the inbound
FK ``datafields.dataset_id -> datasets.id`` and every ``datafields.dataset_id``
value stay valid — no FK rewiring):
  - ``datasets``   becomes the dataset DEFINITION (UK ``(dataset_id, region)``);
    16 per-cell columns drop after their values migrate to ``dataset_cell_stats``.
  - ``datafields`` becomes the field DEFINITION (UK ``(dataset_id, field_id)``
    unchanged); 10 per-cell columns drop after migrating to ``datafield_cell_stats``;
    ``region`` is dropped (reachable via ``dataset_id -> datasets.region``).

Empirically safe at this revision (verified 2026-05-26): ``datasets`` = 17 rows,
all single-universe per (dataset_id, region) (0 multi-universe groups) →
``(dataset_id, region, universe)`` -> ``(dataset_id, region)`` UK collapse has no
collision; ``datafields`` = 7674 rows, 0 NULL/dangling FK.

PG-ONLY data migration. SQLite test fixtures build the post-refactor schema via
``metadata.create_all`` and never run this migration; guarded by a dialect check.
Idempotent: a top-level guard (``universe`` still on ``datasets``) makes a re-run a
no-op, and the data copy is gated on the cell_stats table being empty.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "s1c7e9a2d4b8"
down_revision: Union[str, Sequence[str], None] = "r9a1c5e3b7f2"
branch_labels = None
depends_on = None


# Per-cell columns that move OFF datasets (def) INTO dataset_cell_stats.
_DATASET_CELL_COLS = (
    "universe", "delay", "coverage", "value_score", "user_count", "alpha_count",
    "field_count", "pyramid_multiplier", "is_active", "mining_weight",
    "date_coverage", "themes", "resources", "last_synced_at",
    "alpha_success_count", "alpha_fail_count",
)
# Per-cell columns that move OFF datafields (def) INTO datafield_cell_stats.
# (region also drops, but it is restored from datasets on downgrade, not from a cell.)
_DATAFIELD_CELL_COLS = (
    "region", "universe", "delay", "date_coverage", "coverage", "pyramid_multiplier",
    "user_count", "alpha_count", "themes", "is_active",
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return  # SQLite/test schema is built post-refactor via create_all
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())
    ds_cols = {c["name"] for c in insp.get_columns("datasets")} if "datasets" in tables else set()

    # Already normalized (the per-cell `universe` column is gone) → no-op.
    if "universe" not in ds_cols:
        return

    # --- 1. create the two cell_stats tables ---
    if "dataset_cell_stats" not in tables:
        op.create_table(
            "dataset_cell_stats",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("dataset_ref", sa.Integer(), sa.ForeignKey("datasets.id"), nullable=False),
            sa.Column("universe", sa.String(length=50), nullable=False, server_default="TOP3000"),
            sa.Column("delay", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("coverage", sa.Float()),
            sa.Column("date_coverage", sa.Float()),
            sa.Column("value_score", sa.Integer()),
            sa.Column("user_count", sa.Integer()),
            sa.Column("alpha_count", sa.Integer()),
            sa.Column("field_count", sa.Integer()),
            sa.Column("pyramid_multiplier", sa.Float()),
            sa.Column("mining_weight", sa.Float(), server_default="1.0"),
            sa.Column("themes", JSONB()),
            sa.Column("resources", JSONB()),
            sa.Column("alpha_success_count", sa.Integer(), server_default="0"),
            sa.Column("alpha_fail_count", sa.Integer(), server_default="0"),
            sa.Column("is_active", sa.Boolean(), server_default=sa.true()),
            sa.Column("last_synced_at", sa.DateTime()),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
            sa.UniqueConstraint("dataset_ref", "delay", "universe", name="uq_dataset_cell"),
        )
    if "datafield_cell_stats" not in tables:
        op.create_table(
            "datafield_cell_stats",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("datafield_ref", sa.Integer(), sa.ForeignKey("datafields.id"), nullable=False),
            sa.Column("universe", sa.String(length=50), nullable=False, server_default="TOP3000"),
            sa.Column("delay", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("date_coverage", sa.Float()),
            sa.Column("coverage", sa.Float()),
            sa.Column("pyramid_multiplier", sa.Float()),
            sa.Column("user_count", sa.Integer()),
            sa.Column("alpha_count", sa.Integer()),
            sa.Column("themes", JSONB()),
            sa.Column("is_active", sa.Boolean(), server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
            sa.UniqueConstraint("datafield_ref", "delay", "universe", name="uq_datafield_cell"),
        )

    # --- 2. copy per-cell values into cell_stats (one cell per existing def row) ---
    # Gated on emptiness so a (transactionally impossible but cheap-to-guard)
    # re-entry can't double-insert.
    if bind.execute(sa.text("SELECT count(*) FROM dataset_cell_stats")).scalar() == 0:
        op.execute(
            """
            INSERT INTO dataset_cell_stats
              (dataset_ref, universe, delay, coverage, date_coverage, value_score,
               user_count, alpha_count, field_count, pyramid_multiplier, mining_weight,
               themes, resources, alpha_success_count, alpha_fail_count, is_active,
               last_synced_at)
            SELECT id, COALESCE(universe, 'TOP3000'), COALESCE(delay, 1), coverage,
                   date_coverage, value_score, user_count, alpha_count, field_count,
                   pyramid_multiplier, COALESCE(mining_weight, 1.0), themes, resources,
                   COALESCE(alpha_success_count, 0), COALESCE(alpha_fail_count, 0),
                   COALESCE(is_active, TRUE), last_synced_at
            FROM datasets
            """
        )
    if bind.execute(sa.text("SELECT count(*) FROM datafield_cell_stats")).scalar() == 0:
        op.execute(
            """
            INSERT INTO datafield_cell_stats
              (datafield_ref, universe, delay, date_coverage, coverage,
               pyramid_multiplier, user_count, alpha_count, themes, is_active)
            SELECT id, COALESCE(universe, 'TOP3000'), COALESCE(delay, 1), date_coverage,
                   coverage, pyramid_multiplier, user_count, alpha_count, themes,
                   COALESCE(is_active, TRUE)
            FROM datafields
            """
        )

    # --- 3. swap datasets UK (drop universe-grain, add region-grain) ---
    op.drop_constraint("uq_dataset_region_universe", "datasets", type_="unique")
    op.create_unique_constraint("uq_dataset_region", "datasets", ["dataset_id", "region"])

    # --- 4. drop the now-migrated per-cell columns ---
    for col in _DATASET_CELL_COLS:
        op.drop_column("datasets", col)
    for col in _DATAFIELD_CELL_COLS:
        op.drop_column("datafields", col)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())
    ds_cols = {c["name"] for c in insp.get_columns("datasets")} if "datasets" in tables else set()

    # Already at the old (denormalized) schema → no-op.
    if "universe" in ds_cols:
        return

    # --- 1. re-add per-cell columns (nullable; backfill before re-imposing NOT NULL) ---
    op.add_column("datasets", sa.Column("universe", sa.String(length=50)))
    op.add_column("datasets", sa.Column("delay", sa.Integer()))
    op.add_column("datasets", sa.Column("coverage", sa.Float()))
    op.add_column("datasets", sa.Column("value_score", sa.Integer()))
    op.add_column("datasets", sa.Column("user_count", sa.Integer()))
    op.add_column("datasets", sa.Column("alpha_count", sa.Integer()))
    op.add_column("datasets", sa.Column("field_count", sa.Integer()))
    op.add_column("datasets", sa.Column("pyramid_multiplier", sa.Float()))
    op.add_column("datasets", sa.Column("is_active", sa.Boolean()))
    op.add_column("datasets", sa.Column("mining_weight", sa.Float()))
    op.add_column("datasets", sa.Column("date_coverage", sa.Float()))
    op.add_column("datasets", sa.Column("themes", JSONB()))
    op.add_column("datasets", sa.Column("resources", JSONB()))
    op.add_column("datasets", sa.Column("last_synced_at", sa.DateTime()))
    op.add_column("datasets", sa.Column("alpha_success_count", sa.Integer()))
    op.add_column("datasets", sa.Column("alpha_fail_count", sa.Integer()))

    op.add_column("datafields", sa.Column("region", sa.String(length=10)))
    op.add_column("datafields", sa.Column("universe", sa.String(length=50)))
    op.add_column("datafields", sa.Column("delay", sa.Integer()))
    op.add_column("datafields", sa.Column("date_coverage", sa.Float()))
    op.add_column("datafields", sa.Column("coverage", sa.Float()))
    op.add_column("datafields", sa.Column("pyramid_multiplier", sa.Float()))
    op.add_column("datafields", sa.Column("user_count", sa.Integer()))
    op.add_column("datafields", sa.Column("alpha_count", sa.Integer()))
    op.add_column("datafields", sa.Column("themes", JSONB()))
    op.add_column("datafields", sa.Column("is_active", sa.Boolean()))

    # --- 2. merge the preferred cell (TOP3000/delay=1 if present, else lowest id) back ---
    op.execute(
        """
        UPDATE datasets d SET
            universe = c.universe, delay = c.delay, coverage = c.coverage,
            date_coverage = c.date_coverage, value_score = c.value_score,
            user_count = c.user_count, alpha_count = c.alpha_count,
            field_count = c.field_count, pyramid_multiplier = c.pyramid_multiplier,
            mining_weight = c.mining_weight, themes = c.themes, resources = c.resources,
            alpha_success_count = c.alpha_success_count,
            alpha_fail_count = c.alpha_fail_count, is_active = c.is_active,
            last_synced_at = c.last_synced_at
        FROM dataset_cell_stats c
        WHERE c.dataset_ref = d.id
          AND c.id = (
              SELECT c2.id FROM dataset_cell_stats c2 WHERE c2.dataset_ref = d.id
              ORDER BY (c2.universe = 'TOP3000' AND c2.delay = 1) DESC, c2.id
              LIMIT 1
          )
        """
    )
    op.execute(
        """
        UPDATE datafields f SET
            universe = c.universe, delay = c.delay, date_coverage = c.date_coverage,
            coverage = c.coverage, pyramid_multiplier = c.pyramid_multiplier,
            user_count = c.user_count, alpha_count = c.alpha_count, themes = c.themes,
            is_active = c.is_active
        FROM datafield_cell_stats c
        WHERE c.datafield_ref = f.id
          AND c.id = (
              SELECT c2.id FROM datafield_cell_stats c2 WHERE c2.datafield_ref = f.id
              ORDER BY (c2.universe = 'TOP3000' AND c2.delay = 1) DESC, c2.id
              LIMIT 1
          )
        """
    )
    # datafields.region was NOT a cell column — restore from the parent dataset.
    op.execute("UPDATE datafields f SET region = d.region FROM datasets d WHERE f.dataset_id = d.id")

    # --- 3. re-impose original NOT NULL constraints ---
    op.alter_column("datasets", "universe", existing_type=sa.String(length=50), nullable=False)
    op.alter_column("datafields", "region", existing_type=sa.String(length=10), nullable=False)
    op.alter_column("datafields", "universe", existing_type=sa.String(length=50), nullable=False)

    # --- 4. swap datasets UK back to the universe grain ---
    op.drop_constraint("uq_dataset_region", "datasets", type_="unique")
    op.create_unique_constraint(
        "uq_dataset_region_universe", "datasets", ["dataset_id", "region", "universe"]
    )

    # --- 5. drop the cell_stats tables ---
    op.drop_table("datafield_cell_stats")
    op.drop_table("dataset_cell_stats")
