"""Phase 4 Sprint 1 A1.2 — feature_flag_audit sentinel columns

Revision ID: k6f8a3d2c1b9
Revises: j5b1a7e3c2f4
Create Date: 2026-05-20

Extends feature_flag_audit with 3 columns needed for R12 LLM_MODE
sentinel guard + restore:
  - sentinel_trigger_for VARCHAR(64) NULL — non-null marks the row as
    a sentinel-cascade audit, value identifies the parent flag that
    triggered the cascade (e.g. 'ENABLE_LLM_ASSISTANT_MODE'). The
    list_audit default filter excludes these rows so the ops Timeline
    isn't flooded by the 6-row sentinel burst on every R12 flip.
  - restored_at TIMESTAMP NULL — stamped when restore_sentinel()
    reverts the override; lets restore_sentinel idempotently skip
    already-restored rows on re-run.
  - restored_by VARCHAR(64) NULL — actor who triggered the restore
    (operator console actor / system).

Zero-risk additive:
  - ALTER TABLE ADD COLUMN with NULL default — existing rows get
    NULL for all 3 columns (semantically: "regular audit row, never
    restored").
  - inspector.has_column guard for idempotent re-run.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "k6f8a3d2c1b9"
down_revision: Union[str, Sequence[str], None] = "j5b1a7e3c2f4"
branch_labels = None
depends_on = None


_TABLE = "feature_flag_audit"
_NEW_COLUMNS = (
    ("sentinel_trigger_for", sa.String(length=64), True),
    ("restored_at", sa.DateTime(timezone=True), True),
    ("restored_by", sa.String(length=64), True),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {col["name"] for col in inspector.get_columns(_TABLE)}
    for col_name, col_type, nullable in _NEW_COLUMNS:
        if col_name in existing:
            continue
        op.add_column(_TABLE, sa.Column(col_name, col_type, nullable=nullable))

    # Partial index — most audit rows are NOT sentinel-triggered, so a
    # WHERE predicate keeps the index tiny + fast for the common
    # list_audit (sentinel_trigger_for IS NULL) query.
    if "ix_ffa_sentinel_trigger" not in {ix["name"] for ix in inspector.get_indexes(_TABLE)}:
        op.create_index(
            "ix_ffa_sentinel_trigger",
            _TABLE,
            ["sentinel_trigger_for"],
            postgresql_where=sa.text("sentinel_trigger_for IS NOT NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "ix_ffa_sentinel_trigger" in {ix["name"] for ix in inspector.get_indexes(_TABLE)}:
        op.drop_index("ix_ffa_sentinel_trigger", _TABLE)
    existing = {col["name"] for col in inspector.get_columns(_TABLE)}
    for col_name, _, _ in reversed(_NEW_COLUMNS):
        if col_name in existing:
            op.drop_column(_TABLE, col_name)
