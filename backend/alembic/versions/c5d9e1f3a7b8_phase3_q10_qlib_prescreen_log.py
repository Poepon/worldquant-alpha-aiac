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
    op.create_index("ix_q10_task_id", "qlib_prescreen_log", ["task_id"])
    op.create_index("ix_q10_created_at", "qlib_prescreen_log", ["created_at"])
    op.create_index("ix_q10_verdict", "qlib_prescreen_log", ["verdict"])
    op.create_index("ix_q10_expr_hash", "qlib_prescreen_log", ["expression_hash"])


def downgrade() -> None:
    op.drop_index("ix_q10_expr_hash", table_name="qlib_prescreen_log")
    op.drop_index("ix_q10_verdict", table_name="qlib_prescreen_log")
    op.drop_index("ix_q10_created_at", table_name="qlib_prescreen_log")
    op.drop_index("ix_q10_task_id", table_name="qlib_prescreen_log")
    op.drop_table("qlib_prescreen_log")
