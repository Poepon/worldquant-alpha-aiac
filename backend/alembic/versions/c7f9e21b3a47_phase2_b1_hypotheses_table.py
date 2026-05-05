"""Phase 2 B1 — hypotheses table + alphas.hypothesis_id FK

Plan v5+ §Phase 2 B1: typed Hypothesis becomes a first-class DB row so:
- Cross-round alpha accumulation under same hypothesis_id is possible
- Lifecycle (PROPOSED → ACTIVE → PROMOTED / ABANDONED) is durable
- KB learning (Phase 2 B8) can key on hypothesis instead of dataset

Adds:
  - hypotheses table (24 columns, see Hypothesis model)
  - alphas.hypothesis_id INT NULL FK hypotheses(id) ON DELETE SET NULL
  - Partial indexes for active sampling, variant isolation, parent lineage
  - alphas(hypothesis_id) WHERE hypothesis_id IS NOT NULL — Phase 2 query path

Backwards compat: alphas.hypothesis (Text) column stays — legacy LLM-emitted
summary text. New typed path uses hypothesis_id FK + Hypothesis row.

Revision ID: c7f9e21b3a47
Revises: b8e51c2a9d34
Create Date: 2026-05-06
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c7f9e21b3a47"
down_revision: Union[str, Sequence[str], None] = "b8e51c2a9d34"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- hypotheses table ----
    op.create_table(
        "hypotheses",
        sa.Column("id", sa.Integer(), primary_key=True),

        # Core
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("kind", sa.String(length=30), nullable=False, server_default="INVESTMENT_THESIS"),
        sa.Column("target_tier", sa.Integer(), nullable=False, server_default="1"),

        # Classification
        sa.Column("expected_signal", sa.String(length=50), server_default="unknown"),
        sa.Column("confidence", sa.String(length=20), server_default="medium"),
        sa.Column("novelty", sa.String(length=30), server_default="established"),
        sa.Column("key_fields", postgresql.JSONB(), server_default="[]"),
        sa.Column("suggested_operators", postgresql.JSONB(), server_default="[]"),

        # Region binding
        sa.Column("region", sa.String(length=10), nullable=False),
        sa.Column("universe", sa.String(length=50), nullable=True),
        sa.Column("dataset_pool", postgresql.JSONB(), server_default="[]"),

        # Lineage (FK to alphas + self for ImprovementRule chains)
        sa.Column("parent_alpha_id", sa.Integer(), nullable=True),
        sa.Column("parent_hypothesis_id", sa.Integer(), nullable=True),

        # Variant isolation (Plan v5+ F-5)
        sa.Column("experiment_variant", sa.String(length=20), nullable=True),

        # Aggregated stats
        sa.Column("alpha_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pass_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sharpe_avg", sa.Float(), nullable=True),
        sa.Column("sharpe_max", sa.Float(), nullable=True),

        # Lifecycle
        sa.Column("status", sa.String(length=20), nullable=False, server_default="PROPOSED"),
        sa.Column("abandon_reason", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),

        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),

        sa.ForeignKeyConstraint(
            ["parent_alpha_id"], ["alphas.id"], ondelete="SET NULL",
            name="fk_hypotheses_parent_alpha",
        ),
        sa.ForeignKeyConstraint(
            ["parent_hypothesis_id"], ["hypotheses.id"], ondelete="SET NULL",
            name="fk_hypotheses_parent_hypothesis",
        ),
    )

    # Indexes (matching Hypothesis.__table_args__)
    op.create_index(
        "ix_hypotheses_id", "hypotheses", ["id"],
    )
    op.create_index(
        "ix_hypotheses_kind", "hypotheses", ["kind"],
    )
    op.create_index(
        "ix_hypotheses_target_tier", "hypotheses", ["target_tier"],
    )
    op.create_index(
        "ix_hypotheses_region", "hypotheses", ["region"],
    )
    op.create_index(
        "ix_hypotheses_status", "hypotheses", ["status"],
    )
    op.create_index(
        "ix_hypotheses_is_active", "hypotheses", ["is_active"],
    )
    # Partial: sampling path "give me active hypotheses for this region"
    op.create_index(
        "ix_hypotheses_region_active",
        "hypotheses",
        ["region", "is_active"],
        postgresql_where=sa.text("status IN ('PROPOSED', 'ACTIVE')"),
    )
    # Partial: variant isolation
    op.create_index(
        "ix_hypotheses_variant",
        "hypotheses",
        ["experiment_variant"],
        postgresql_where=sa.text("experiment_variant IS NOT NULL"),
    )
    # Partial: parent_alpha lineage
    op.create_index(
        "ix_hypotheses_parent_alpha",
        "hypotheses",
        ["parent_alpha_id"],
        postgresql_where=sa.text("parent_alpha_id IS NOT NULL"),
    )

    # ---- alphas.hypothesis_id ----
    op.add_column(
        "alphas",
        sa.Column("hypothesis_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_alphas_hypothesis",
        "alphas", "hypotheses",
        ["hypothesis_id"], ["id"],
        ondelete="SET NULL",
    )
    # Partial index: only Phase 2+ alphas have hypothesis_id; legacy rows
    # stay NULL forever, so index those that DO have it.
    op.create_index(
        "ix_alphas_hypothesis_id",
        "alphas",
        ["hypothesis_id"],
        postgresql_where=sa.text("hypothesis_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_alphas_hypothesis_id", table_name="alphas")
    op.drop_constraint("fk_alphas_hypothesis", "alphas", type_="foreignkey")
    op.drop_column("alphas", "hypothesis_id")

    op.drop_index("ix_hypotheses_parent_alpha", table_name="hypotheses")
    op.drop_index("ix_hypotheses_variant", table_name="hypotheses")
    op.drop_index("ix_hypotheses_region_active", table_name="hypotheses")
    op.drop_index("ix_hypotheses_is_active", table_name="hypotheses")
    op.drop_index("ix_hypotheses_status", table_name="hypotheses")
    op.drop_index("ix_hypotheses_region", table_name="hypotheses")
    op.drop_index("ix_hypotheses_target_tier", table_name="hypotheses")
    op.drop_index("ix_hypotheses_kind", table_name="hypotheses")
    op.drop_index("ix_hypotheses_id", table_name="hypotheses")
    op.drop_table("hypotheses")
