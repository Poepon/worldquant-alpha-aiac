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
    op.create_index("ix_r8q_created_at", "r8_query_log", ["created_at"])
    op.create_index("ix_r8q_task_id", "r8_query_log", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_r8q_task_id", table_name="r8_query_log")
    op.drop_index("ix_r8q_created_at", table_name="r8_query_log")
    op.drop_table("r8_query_log")
