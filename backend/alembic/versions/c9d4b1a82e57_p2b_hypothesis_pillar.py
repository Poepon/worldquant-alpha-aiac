"""P2-B Five Pillars factor classifier — hypothesis.pillar column

来源: docs/alphagbm_skills_research_2026-05-15.md skill `compare`.

Adds 1 column to ``hypotheses``:
- ``pillar`` String(20) NULL — momentum/value/quality/volatility/sentiment/other.
  NULL for legacy rows (created before 2026-05-15) — the pillar_classifier
  falls back to op/field static inference for those.

不用 Postgres ENUM(保留 LLM 输出灵活性 + 防 schema lock-in);
Python 层 PILLAR_VALUES 集合 validate。

Partial index ``ix_hypotheses_pillar_active``:per-pillar active hypothesis
count 查询。镜像 ``ix_hypotheses_region_active`` partial-where 模式。

Pure additive — no backfill (NULL 是 documented legacy fallback path)。

Revision ID: c9d4b1a82e57
Revises: b2c5d8e1a9f4
Create Date: 2026-05-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c9d4b1a82e57"
down_revision: Union[str, Sequence[str], None] = "b2c5d8e1a9f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "hypotheses",
        sa.Column("pillar", sa.String(length=20), nullable=True),
    )
    op.create_index(
        "ix_hypotheses_pillar_active",
        "hypotheses",
        ["pillar"],
        postgresql_where=sa.text(
            "pillar IS NOT NULL AND is_active IS TRUE"
        ),
    )


def downgrade() -> None:
    # MFX-7 lesson: drop_index BEFORE drop_column so PG's CASCADE doesn't
    # fight the partial-where clause re-creation on a subsequent upgrade.
    op.drop_index("ix_hypotheses_pillar_active", table_name="hypotheses")
    op.drop_column("hypotheses", "pillar")
