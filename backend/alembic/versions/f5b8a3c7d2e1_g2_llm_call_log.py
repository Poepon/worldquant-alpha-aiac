"""g2-phase-a: llm_call_log per-call cost telemetry table

Revision ID: f5b8a3c7d2e1
Revises: e1f3b9c2a4d8
Create Date: 2026-05-19

G2 Phase A (light wiring) per master plan + memory
[[feedback_light_wiring_deferred_gate]] / [[feedback_r1a_dedicated_log_table]]:

NEW table ``llm_call_log`` — one row per LLMService.call invocation,
captured via cost_tracker contextvar batched flush at round boundary.
Coverage:普通 round (hypothesis / code_gen / self_correct / distill /
mutate) + R1b retry/mutate path + macro narrative LLM batch + any future
LLM caller. R1b path keeps writing r1b_retry_log for outcome
reconciliation (per-attempt result fields); llm_call_log is the pure
cost/token aggregation source feeding /ops/cost/telemetry.

Zero-risk additive:
  - Brand-new table → DROP TABLE on downgrade
  - inspector.has_table() guard for dev DBs that ran
    metadata.create_all() startup fallback (same lesson as Q10 PR1b /
    R1b PR1)
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "f5b8a3c7d2e1"
down_revision: Union[str, Sequence[str], None] = "e1f3b9c2a4d8"  # bigbang tier drop head
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    existing_indexes = (
        set(ix["name"] for ix in inspector.get_indexes("llm_call_log"))
        if "llm_call_log" in existing_tables
        else set()
    )

    if "llm_call_log" not in existing_tables:
        op.create_table(
            "llm_call_log",
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("task_id", sa.Integer(), nullable=True),
            sa.Column("run_id", sa.Integer(), nullable=True),
            sa.Column("round_idx", sa.Integer(), nullable=True),
            sa.Column("dataset_id", sa.String(64), nullable=True),
            sa.Column("pillar", sa.String(20), nullable=True),
            sa.Column("node_key", sa.String(40), nullable=True),
            sa.Column("model", sa.String(60), nullable=False),
            sa.Column("provider", sa.String(20), nullable=True),
            sa.Column("effort", sa.String(20), nullable=True),
            sa.Column("prompt_tokens", sa.Integer(), nullable=True),
            sa.Column("completion_tokens", sa.Integer(), nullable=True),
            sa.Column("tokens_total", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("cost_usd", sa.Float(), nullable=True),
            sa.Column("latency_ms", sa.Integer(), nullable=True),
            sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("error_kind", sa.String(40), nullable=True),
            sa.Column("call_id", sa.String(20), nullable=True),
            sa.Column("write_error", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )

    for ix_name, ix_cols in (
        ("ix_llmcl_task_id", ["task_id"]),
        ("ix_llmcl_run_id", ["run_id"]),
        ("ix_llmcl_created_at", ["created_at"]),
        ("ix_llmcl_node_key", ["node_key"]),
        ("ix_llmcl_model", ["model"]),
    ):
        if ix_name not in existing_indexes:
            op.create_index(ix_name, "llm_call_log", ix_cols)


def downgrade() -> None:
    for ix_name in (
        "ix_llmcl_model",
        "ix_llmcl_node_key",
        "ix_llmcl_created_at",
        "ix_llmcl_run_id",
        "ix_llmcl_task_id",
    ):
        op.execute(f'DROP INDEX IF EXISTS {ix_name}')
    op.drop_table("llm_call_log")
