"""Phase 4 Sprint 3 A5.1 G10 — distilled_logic_library table

Revision ID: n5e6f7g8h9i0
Revises: l7c2d8e1f4a3
Create Date: 2026-05-20

One row per (week, pillar, region) — LLM-distilled summary of what
common logic the PASS alphas in that bucket share. Sunday 03:00 SH
cron writes a fresh batch; old rows stay (history preserved for the
A5.2 refine chain in Sprint 4) with `retired_at` flagging supersession.

Zero-risk additive:
  - Brand-new table, no FK to alphas (we record source_alpha_ids in
    JSONB instead — soft reference; alpha purges don't cascade-delete
    distilled logic)
  - Index on (created_at DESC, region) supports the dashboard / future
    retrieval (PR2)
  - inspector guard for dev DBs using metadata.create_all()
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "n5e6f7g8h9i0"
down_revision: Union[str, Sequence[str], None] = "l7c2d8e1f4a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "distilled_logic_library" in set(inspector.get_table_names()):
        return

    dialect = bind.dialect.name
    jsonb_type = (
        postgresql.JSONB(astext_type=sa.Text())
        if dialect == "postgresql"
        else sa.JSON()
    )

    op.create_table(
        "distilled_logic_library",
        sa.Column(
            "id",
            sa.BigInteger() if dialect == "postgresql" else sa.Integer(),
            primary_key=True,
        ),
        # LLM-distilled summary text (1-3 sentences)
        sa.Column("logic_text", sa.Text(), nullable=False),
        # Tokenized text for Jaccard similarity (PR2 retrieval). Stored
        # explicitly rather than re-tokenizing at query time.
        sa.Column("tokens", jsonb_type, nullable=False, default=list),
        # source alpha ids (List[int]) — soft reference, no FK
        sa.Column("source_alpha_ids", jsonb_type, nullable=False, default=list),
        sa.Column("pillar", sa.String(50), nullable=True, index=True),
        sa.Column("region", sa.String(10), nullable=False, index=True),
        # Week anchor: ISO Monday of the distillation week (date column
        # would be nicer but DateTime stays consistent with the rest of
        # the schema; comparing by date is fine).
        sa.Column(
            "distilled_at_week",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # LLM cost tracking — week cap enforcement reads from this column
        sa.Column("llm_cost_usd", sa.Float(), nullable=True),
        # Jaccard similarity to the previous-week entry in the same
        # (pillar, region) — diagnostic. 0.0 means very different (worth
        # retaining), 0.9+ means highly redundant.
        sa.Column("similarity_jaccard_to_prev_week", sa.Float(), nullable=True),
        # NULL = active, non-NULL = superseded by a later row. PR2's
        # refine_logic_library updates this; PR1 distill never sets it.
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        # Source LLM model id (Anthropic / OpenAI / DeepSeek family)
        sa.Column("llm_model", sa.String(80), nullable=True),
    )

    op.create_index(
        "ix_distilled_logic_created_region",
        "distilled_logic_library",
        ["created_at", "region"],
    )
    op.create_index(
        "ix_distilled_logic_active",
        "distilled_logic_library",
        ["region", "pillar"],
        postgresql_where=sa.text("retired_at IS NULL") if dialect == "postgresql" else None,
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "distilled_logic_library" not in set(inspector.get_table_names()):
        return
    existing = {ix["name"] for ix in inspector.get_indexes("distilled_logic_library")}
    for ix in ("ix_distilled_logic_created_region", "ix_distilled_logic_active"):
        if ix in existing:
            op.drop_index(ix, table_name="distilled_logic_library")
    op.drop_table("distilled_logic_library")
