"""r8_query_log: per-call hierarchical RAG layer telemetry (2026-05-18)

Revision ID: b2e5c9f1d847
Revises: a7d2f9e4b8c3
Create Date: 2026-05-18

Closes the R8 kb-shape memory's listed future work: kb-shape gives
*corpus*-level visibility (how many SUCCESS_PATTERN entries exist),
but layer_hits per query were ephemeral in RAGResult dataclass. This
table records one row per query_hierarchical call so operators can see
*runtime* L0/L1/L2/L3 hit distribution + cache effectiveness.

Schema:
  - task_id (nullable — RAG can fire from background jobs without a task)
  - region / dataset_id (nullable; not always present in query context)
  - current_expression_hash (sha256 prefix; join-back to alphas)
  - layer_hits: JSONB {L0_exact: N, L1_pillar: N, L2_family: N, L3_field: N}
  - total_queries: int (sum of layer hits — sanity check)
  - cache_hit: bool — Redis short-circuit
  - had_failure_tree_elevation: bool — R1b.3-v2 L2 Jaccard bonus fired
  - created_at: timezone-aware, server_default now()
  - Indexes on created_at + task_id for the standard window/per-task aggregates

Flag-gated by ENABLE_R8_QUERY_LOG (default OFF). When OFF the table
exists but no writes happen — zero risk to existing R8 callers.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "b2e5c9f1d847"
down_revision: Union[str, Sequence[str], None] = "a7d2f9e4b8c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # inspector.has_table() guard — dev DBs created via
    # database.init_db()'s metadata.create_all() fallback already have the
    # r8_query_log table (ORM-mapped in backend/models/r8_query_log.py).
    # When the chain replays from an earlier stamp, an unguarded
    # create_table would crash with DuplicateTable. Surfaced by
    # test_alembic_chain_pg.py::test_chain_upgrade_from_intermediate_revision.
    # Pattern mirrors c5d9e1f3a7b8 Q10 PR1b.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    existing_indexes = (
        set(ix["name"] for ix in inspector.get_indexes("r8_query_log"))
        if "r8_query_log" in existing_tables
        else set()
    )

    if "r8_query_log" not in existing_tables:
        op.create_table(
            "r8_query_log",
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("task_id", sa.Integer(), nullable=True),
            sa.Column("region", sa.String(length=8), nullable=True),
            sa.Column("dataset_id", sa.String(length=64), nullable=True),
            sa.Column("current_expression_hash", sa.String(length=64), nullable=True),
            sa.Column(
                "layer_hits", JSONB(), nullable=True,
                server_default=sa.text("'{}'::jsonb"),
            ),
            sa.Column("total_queries", sa.Integer(), nullable=True, server_default="0"),
            sa.Column("cache_hit", sa.Boolean(), nullable=True, server_default=sa.text("false")),
            sa.Column(
                "had_failure_tree_elevation", sa.Boolean(),
                nullable=True, server_default=sa.text("false"),
            ),
            sa.Column(
                "created_at", sa.DateTime(timezone=True),
                nullable=False, server_default=sa.text("now()"),
            ),
        )

    for ix_name, ix_cols in (
        ("ix_r8q_created_at", ["created_at"]),
        ("ix_r8q_task_id", ["task_id"]),
    ):
        if ix_name not in existing_indexes:
            op.create_index(ix_name, "r8_query_log", ix_cols)


def downgrade() -> None:
    # Symmetric guard — drop only if present (mirrors upgrade's guard so
    # downgrade is safe on partially-applied DBs).
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "r8_query_log" not in set(inspector.get_table_names()):
        return
    existing_indexes = set(
        ix["name"] for ix in inspector.get_indexes("r8_query_log")
    )
    for ix_name in ("ix_r8q_task_id", "ix_r8q_created_at"):
        if ix_name in existing_indexes:
            op.drop_index(ix_name, table_name="r8_query_log")
    op.drop_table("r8_query_log")
