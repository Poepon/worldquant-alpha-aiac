"""phase3-r9: simulation_cache table for BRAIN sim result caching

Revision ID: 9a4f7e8c1d6b
Revises: 7e9f2a1b3c4d
Create Date: 2026-05-18

Per master plan §4.5 R9: avoid re-simulating known (region, universe,
expression, settings) tuples on BRAIN — cached result returned instead.
Estimated 40-60% BRAIN cost reduction on duplicate-heavy workloads
(cascade T2/T3 wrappers, flat-F1 dataset cycling).

Schema:
  - cache_key (sha256 prefix of canonical inputs) UNIQUE
  - region / universe / expression (forensic) / expression_hash
  - settings_json (JSON: delay/decay/neutralization/truncation/test_period)
  - result_json (JSON: BRAIN-returned result dict)
  - success (bool)
  - cached_at + accessed_at + access_count (TTL + analytics)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9a4f7e8c1d6b"
down_revision: Union[str, Sequence[str], None] = "7e9f2a1b3c4d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "simulation_cache",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("cache_key", sa.String(64), nullable=False),
        sa.Column("region", sa.String(20), nullable=False),
        sa.Column("universe", sa.String(50), nullable=False),
        sa.Column("expression", sa.Text(), nullable=False),
        sa.Column("expression_hash", sa.String(64), nullable=False),
        sa.Column("settings_json", sa.JSON(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("cached_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("accessed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("access_count", sa.Integer(), server_default="1", nullable=False),
    )
    op.create_index("ix_sim_cache_key", "simulation_cache", ["cache_key"], unique=True)
    op.create_index("ix_sim_cache_expression_hash", "simulation_cache", ["expression_hash"])
    op.create_index("ix_sim_cache_cached_at", "simulation_cache", ["cached_at"])
    op.create_index("ix_sim_cache_region_universe", "simulation_cache", ["region", "universe"])


def downgrade() -> None:
    op.drop_index("ix_sim_cache_region_universe", table_name="simulation_cache")
    op.drop_index("ix_sim_cache_cached_at", table_name="simulation_cache")
    op.drop_index("ix_sim_cache_expression_hash", table_name="simulation_cache")
    op.drop_index("ix_sim_cache_key", table_name="simulation_cache")
    op.drop_table("simulation_cache")
