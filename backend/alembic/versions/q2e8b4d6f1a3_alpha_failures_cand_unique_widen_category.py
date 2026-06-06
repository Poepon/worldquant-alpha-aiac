"""Pool schema hardening — alpha_failures.candidate_queue_id partial-unique + widen candidate_queue.dataset_category

Revision ID: q2e8b4d6f1a3
Revises: p1d3f5a7c9e2
Create Date: 2026-06-06

Two pool-only schema hardenings (the metrics-wiring follow-up is deferred — that
column already exists from p1d3f5a7c9e2 and needs no migration):

  1. alpha_failures.candidate_queue_id (new INTEGER, nullable, NO ForeignKey) +
     a PARTIAL UNIQUE index ``WHERE candidate_queue_id IS NOT NULL``. This is the
     DB backstop behind the best-effort Redis persist-marker: it makes the E-pool
     FAIL-row write idempotent per candidate (closes the B2 crash-window double-
     write on the load-bearing alpha_failures denominator). NULLs are distinct, so
     FLAT / legacy rows (candidate_queue_id NULL) stay unconstrained. NO FK on the
     column: candidate_queue rows are purged but failure rows are a permanent audit
     log — an ON DELETE SET NULL would silently un-dedup on purge, a NOT NULL FK
     would block purges.
  2. candidate_queue.dataset_category String(80) → String(200): inferred values are
     <20 chars today but BRAIN category strings can reach ~203 — headroom so a long
     category can't error on INSERT. Zero FLAT impact (FLAT never writes it).

Zero behaviour change while ENABLE_POOL_PIPELINE is OFF (alpha_failures.
candidate_queue_id stays NULL on the live FLAT path; the widened varchar is a
superset). Idempotent: inspector guards on the column + index; the varchar widen
is PostgreSQL-only (SQLite does not enforce VARCHAR length, and create_all already
builds the model's String(200)). Forward-compatible with dev DBs that ran
SQLAlchemyBase.metadata.create_all().
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "q2e8b4d6f1a3"
down_revision: Union[str, Sequence[str], None] = "p1d3f5a7c9e2"
branch_labels = None
depends_on = None

_IDX = "uq_alpha_failures_candidate_queue_id"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    dialect = bind.dialect.name
    existing = set(inspector.get_table_names())

    # ---- 1. alpha_failures.candidate_queue_id + partial-unique index ----
    if "alpha_failures" in existing:
        cols = {c["name"] for c in inspector.get_columns("alpha_failures")}
        if "candidate_queue_id" not in cols:
            op.add_column(
                "alpha_failures",
                sa.Column("candidate_queue_id", sa.Integer(), nullable=True),
            )
        idx_names = {ix["name"] for ix in inspector.get_indexes("alpha_failures")}
        if _IDX not in idx_names:
            if dialect == "postgresql":
                op.create_index(
                    _IDX, "alpha_failures", ["candidate_queue_id"], unique=True,
                    postgresql_where=sa.text("candidate_queue_id IS NOT NULL"),
                )
            else:
                # SQLite: plain UNIQUE index (NULLs are distinct → many NULLs OK).
                op.create_index(_IDX, "alpha_failures", ["candidate_queue_id"], unique=True)

    # ---- 2. widen candidate_queue.dataset_category 80 -> 200 (PG only) ----
    if dialect == "postgresql" and "candidate_queue" in existing:
        op.alter_column(
            "candidate_queue", "dataset_category",
            existing_type=sa.String(length=80),
            type_=sa.String(length=200),
            existing_nullable=True,
        )
    # SQLite: no-op — VARCHAR length is not enforced; create_all builds String(200).


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    dialect = bind.dialect.name
    existing = set(inspector.get_table_names())

    if dialect == "postgresql" and "candidate_queue" in existing:
        op.alter_column(
            "candidate_queue", "dataset_category",
            existing_type=sa.String(length=200),
            type_=sa.String(length=80),
            existing_nullable=True,
        )

    if "alpha_failures" in existing:
        idx_names = {ix["name"] for ix in inspector.get_indexes("alpha_failures")}
        if _IDX in idx_names:
            op.drop_index(_IDX, table_name="alpha_failures")
        cols = {c["name"] for c in inspector.get_columns("alpha_failures")}
        if "candidate_queue_id" in cols:
            op.drop_column("alpha_failures", "candidate_queue_id")
