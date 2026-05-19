"""Phase 4 Sprint 3 follow-up — distilled_logic_library unique constraint

Revision ID: o6d4a8f2c5b7
Revises: n5e6f7g8h9i0
Create Date: 2026-05-20

F2 review fix: prevent double-fire from creating two 'active' rows for
the same (distilled_at_week, region, pillar) tuple. Combined with the
SH-timezone _week_anchor (services/logic_distill_service.py) so cron
double-fires at SH-Sunday boundaries hit IntegrityError on the second
INSERT, surfaced as a warning in the task result dict.

Active-row variant: when ``retired_at`` is non-NULL (Sprint 4 PR2 marks
superseded), the same (week, region, pillar) MAY appear again — so the
unique constraint is partial WHERE retired_at IS NULL on Postgres; on
SQLite (no partial unique index) we accept the broader constraint.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "o6d4a8f2c5b7"
down_revision: Union[str, Sequence[str], None] = "n5e6f7g8h9i0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "distilled_logic_library" not in set(inspector.get_table_names()):
        return

    dialect = bind.dialect.name
    existing_indexes = {ix["name"] for ix in inspector.get_indexes("distilled_logic_library")}
    if "uq_distilled_logic_week_region_pillar_active" in existing_indexes:
        return

    if dialect == "postgresql":
        # Partial unique constraint — only active rows must be unique.
        op.create_index(
            "uq_distilled_logic_week_region_pillar_active",
            "distilled_logic_library",
            ["distilled_at_week", "region", "pillar"],
            unique=True,
            postgresql_where=sa.text("retired_at IS NULL"),
        )
    else:
        # SQLite (dev): plain unique on the triple — same row can't repeat
        # even if retired. Acceptable: SQLite is dev-only.
        op.create_index(
            "uq_distilled_logic_week_region_pillar_active",
            "distilled_logic_library",
            ["distilled_at_week", "region", "pillar"],
            unique=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "distilled_logic_library" not in set(inspector.get_table_names()):
        return
    existing = {ix["name"] for ix in inspector.get_indexes("distilled_logic_library")}
    if "uq_distilled_logic_week_region_pillar_active" in existing:
        op.drop_index(
            "uq_distilled_logic_week_region_pillar_active",
            table_name="distilled_logic_library",
        )
