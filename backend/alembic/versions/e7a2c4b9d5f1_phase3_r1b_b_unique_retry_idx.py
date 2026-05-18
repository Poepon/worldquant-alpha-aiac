"""phase3-r1b-b: UNIQUE constraint on r1b_retry_log dedupe tuple

Revision ID: e7a2c4b9d5f1
Revises: d6f8a3b1e9c4
Create Date: 2026-05-18

R1b.1 review LOW (2026-05-18). Defensive UNIQUE on
``(task_id, round_idx, original_expression_hash, attempt_type)`` to prevent
concurrent dup rows when the same FAIL alpha enters ``node_code_gen_retry``
twice (workflow restart on stuck cycle, OR future multi-worker LangGraph
mode). Single-worker production rarely hits this today, but the constraint
is cheap insurance.

Tuple rationale:
  - ``task_id`` + ``round_idx`` scope to one round of one task
  - ``original_expression_hash`` (SHA256, always populated) identifies the
    failing alpha — preferred over ``original_alpha_id_brain`` which is
    often NULL for pre-sim FAIL rows
  - ``attempt_type`` is included so ``retry_impl`` and ``mutate_hyp`` rows
    can legitimately coexist on the same alpha+round (BOTH attribution
    triggers both nodes per plan §3 + §4)

Postgres UNIQUE treats NULLs as distinct, so direct-invoke unit tests that
omit ``task_id`` / ``round_idx`` continue to insert without conflict.

WARNING: this constraint assumes no pre-existing duplicate rows. R1b shipped
in d6f8a3b1e9c4 on 2026-05-18 (same day), so production volume is minimal
and dup rows are unlikely. If upgrade fails with a uniqueness violation,
manually dedupe first via:

  DELETE FROM r1b_retry_log a USING r1b_retry_log b
   WHERE a.id < b.id
     AND COALESCE(a.task_id, -1) = COALESCE(b.task_id, -1)
     AND COALESCE(a.round_idx, -1) = COALESCE(b.round_idx, -1)
     AND a.original_expression_hash = b.original_expression_hash
     AND a.attempt_type = b.attempt_type;
"""
from typing import Sequence, Union

from alembic import op


revision: str = "e7a2c4b9d5f1"
down_revision: Union[str, Sequence[str], None] = "d6f8a3b1e9c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_r1b_retry_log_task_alpha_attempt_type",
        "r1b_retry_log",
        ["task_id", "round_idx", "original_expression_hash", "attempt_type"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_r1b_retry_log_task_alpha_attempt_type",
        "r1b_retry_log",
        type_="unique",
    )
