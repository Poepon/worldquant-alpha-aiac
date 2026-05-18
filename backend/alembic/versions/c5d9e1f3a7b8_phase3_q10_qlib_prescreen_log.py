"""phase3-q10: qlib_prescreen_log table for Q10 pyqlib pre-screen telemetry

Revision ID: c5d9e1f3a7b8
Revises: b3c8d9e2f4a1
Create Date: 2026-05-18

Per master plan §4.7 Q10 + plan v1.3 §6 (~/.claude/plans/
phase3-q10-pyqlib-prescreen-2026-05-18.md):

Dedicated telemetry table for the local pyqlib pre-screen layer that
sits in front of BRAIN simulate. One row per ``prescreen_alpha()`` call
captures verdict (pass / reject / skip), local Sharpe/IC, the engine
tier used, plus brain_followup_* columns filled by a separate post-
BRAIN update task (NULL in hard mode where BRAIN was never called).

Per ``[[feedback_r1a_dedicated_log_table]]``: dedicated table not
piggyback on alpha.metrics — Q10 fires per simulate attempt (incl.
skipped/untranslatable), not just per PASS/PROV alpha. 50x throughput.

Zero-risk additive:
  - New table only, no existing-table mutation
  - 4 indexes (task_id / created_at / verdict / expression_hash) for the
    cross-tab calibration queries in scripts/calibrate_qlib_prescreen.py
  - Downgrade is a clean DROP TABLE (rows are append-only analytics)
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c5d9e1f3a7b8"
down_revision: Union[str, Sequence[str], None] = "b3c8d9e2f4a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # inspector.has_table() guard — dev DBs created via
    # database.init_db()'s metadata.create_all() fallback already have the
    # table; without this guard `alembic upgrade head` raises DuplicateTable.
    # Surfaced by test_alembic_chain_pg.py XFAIL tests (eae52fa). Pattern
    # mirrors 7a3f9e1c2b8d phase15-A part 2.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    existing_indexes = (
        set(ix["name"] for ix in inspector.get_indexes("qlib_prescreen_log"))
        if "qlib_prescreen_log" in existing_tables
        else set()
    )

    if "qlib_prescreen_log" not in existing_tables:
        op.create_table(
            "qlib_prescreen_log",
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("task_id", sa.Integer(), nullable=True),
            sa.Column("alpha_candidate_idx", sa.Integer(), nullable=True),
            sa.Column("brain_expression", sa.Text(), nullable=False),
            sa.Column("expression_hash", sa.String(64), nullable=False),
            sa.Column("qlib_expression", sa.Text(), nullable=True),
            sa.Column("region", sa.String(20), nullable=False),
            sa.Column("universe", sa.String(50), nullable=False),
            sa.Column("verdict", sa.String(20), nullable=False),
            sa.Column("reject_reason", sa.Text(), nullable=True),
            sa.Column("skip_reason", sa.String(80), nullable=True),
            sa.Column("translation_error", sa.Text(), nullable=True),
            sa.Column("local_sharpe", sa.Float(), nullable=True),
            sa.Column("local_ic", sa.Float(), nullable=True),
            sa.Column("engine_kind", sa.String(32), nullable=False),
            sa.Column("elapsed_ms", sa.Integer(), nullable=False),
            sa.Column("mode_at_call", sa.String(8), nullable=False),
            sa.Column("brain_followup_status", sa.String(20), nullable=True),
            sa.Column("brain_followup_sharpe", sa.Float(), nullable=True),
            sa.Column("brain_disagreement", sa.String(8), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )

    for ix_name, ix_cols in (
        ("ix_q10_task_id", ["task_id"]),
        ("ix_q10_created_at", ["created_at"]),
        ("ix_q10_verdict", ["verdict"]),
        ("ix_q10_expr_hash", ["expression_hash"]),
    ):
        if ix_name not in existing_indexes:
            op.create_index(ix_name, "qlib_prescreen_log", ix_cols)


def downgrade() -> None:
    # Symmetric guard — drop only if present (mirrors upgrade's guard so
    # downgrade is safe on partially-applied DBs).
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "qlib_prescreen_log" not in set(inspector.get_table_names()):
        return
    existing_indexes = set(
        ix["name"] for ix in inspector.get_indexes("qlib_prescreen_log")
    )
    for ix_name in (
        "ix_q10_expr_hash",
        "ix_q10_verdict",
        "ix_q10_created_at",
        "ix_q10_task_id",
    ):
        if ix_name in existing_indexes:
            op.drop_index(ix_name, table_name="qlib_prescreen_log")
    op.drop_table("qlib_prescreen_log")
