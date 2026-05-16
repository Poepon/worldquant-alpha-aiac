"""P1-C (2026-05-15) — Hypothesis structured triggers + LLM thesis scoring

来源: docs/alphagbm_skills_research_2026-05-15.md skill `investment-thesis`.

Adds 9 columns to ``hypotheses`` table for:
- Soft-flag trigger state (is_triggered / triggered_at) — orthogonal to
  ``status``: a triggered hypothesis is still ACTIVE/PROMOTED and continues
  to be sampled (sticky soft-warning, not a stop-sampling switch).
- ``trigger_detail`` JSONB list — append-only history of (type, window,
  severity, reason, hit_at). Deduplicated 24h within (type, window).
- ``baseline_metrics`` JSONB — frozen at first PROMOTED snapshot for T1
  ``dropped_sharpe`` comparison (current AVG vs baseline AVG). Includes
  ``n_alphas`` so T1 can guard against small-sample false positives.
- ``thesis_score`` + ``ai_feedback`` + ``thesis_score_history`` — LLM
  self-grading on trigger hit / first PROMOTED. ``last_thesis_score_status``
  distinguishes ok / fallback_failed / fallback_schema_invalid so the
  daily rerun can use a 4h backoff for failures vs 24h for ok runs
  (SFX-13 — status field is more reliable than parsing ai_feedback prefix).

Adds new audit table ``hypothesis_status_transitions`` mirroring
``alpha_status_transitions`` but covering only the ``is_triggered``
False→True edge (SFX-10: ``mark_abandoned`` / ``mark_promoted`` audit
coverage is deferred to a follow-up; this table stays narrow).

Pure additive — no data backfill. Partial index on
``(region, triggered_at)`` keeps the active-trigger frontend list fast
while ignoring the bulk of healthy rows.

Revision ID: b2c5d8e1a9f4
Revises: 8100862bcef9
Create Date: 2026-05-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "b2c5d8e1a9f4"
down_revision: Union[str, Sequence[str], None] = "8100862bcef9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # hypotheses table: 9 new columns
    # ------------------------------------------------------------------
    op.add_column(
        "hypotheses",
        sa.Column(
            "is_triggered",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "hypotheses",
        sa.Column(
            "triggered_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "hypotheses",
        sa.Column(
            "trigger_detail",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "hypotheses",
        sa.Column(
            "baseline_metrics",
            JSONB(),
            nullable=True,
        ),
    )
    op.add_column(
        "hypotheses",
        sa.Column(
            "thesis_score",
            sa.Float(),
            nullable=True,
        ),
    )
    op.add_column(
        "hypotheses",
        sa.Column(
            "last_thesis_score_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "hypotheses",
        sa.Column(
            "last_thesis_score_status",
            sa.String(length=30),
            nullable=True,
        ),
    )
    op.add_column(
        "hypotheses",
        sa.Column(
            "ai_feedback",
            sa.Text(),
            nullable=True,
        ),
    )
    op.add_column(
        "hypotheses",
        sa.Column(
            "thesis_score_history",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )

    # Partial index — only rows currently flagged + in scope. Mirrors the
    # existing partial-index style of ``ix_hypotheses_region_active``.
    op.create_index(
        "ix_hypotheses_triggered",
        "hypotheses",
        ["region", "triggered_at"],
        postgresql_where=sa.text(
            "is_triggered IS TRUE AND status IN ('ACTIVE','PROMOTED')"
        ),
    )

    # ------------------------------------------------------------------
    # hypothesis_status_transitions: new audit table
    # ------------------------------------------------------------------
    # SFX-10: only covers is_triggered edges. ``old_status`` / ``new_status``
    # intentionally OMITTED — adding them would force ``mark_abandoned`` /
    # ``mark_promoted`` to start writing transitions or the audit becomes
    # asymmetric. That coverage is deferred to a follow-up.
    op.create_table(
        "hypothesis_status_transitions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "hypothesis_id",
            sa.Integer(),
            sa.ForeignKey("hypotheses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("old_is_triggered", sa.Boolean(), nullable=True),
        sa.Column("new_is_triggered", sa.Boolean(), nullable=False),
        sa.Column("sharpe_at_transition", sa.Float(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=True),
        sa.Column(
            "transitioned_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_hyp_status_trans_hid",
        "hypothesis_status_transitions",
        ["hypothesis_id", "transitioned_at"],
    )
    op.create_index(
        "ix_hyp_status_trans_time",
        "hypothesis_status_transitions",
        ["transitioned_at"],
    )
    op.create_index(
        "ix_hyp_status_trans_triggered",
        "hypothesis_status_transitions",
        ["transitioned_at"],
        postgresql_where=sa.text("new_is_triggered IS TRUE"),
    )


def downgrade() -> None:
    # MFX-7 (P1-C Part 1 lesson): drop_index MUST precede drop_table /
    # drop_column on the same target so PG's CASCADE doesn't fight the
    # partial-where clause re-creation on a subsequent upgrade.
    op.drop_index(
        "ix_hyp_status_trans_triggered",
        table_name="hypothesis_status_transitions",
    )
    op.drop_index(
        "ix_hyp_status_trans_time",
        table_name="hypothesis_status_transitions",
    )
    op.drop_index(
        "ix_hyp_status_trans_hid",
        table_name="hypothesis_status_transitions",
    )
    op.drop_table("hypothesis_status_transitions")

    op.drop_index("ix_hypotheses_triggered", table_name="hypotheses")
    for col in (
        "thesis_score_history",
        "ai_feedback",
        "last_thesis_score_status",
        "last_thesis_score_at",
        "thesis_score",
        "baseline_metrics",
        "trigger_detail",
        "triggered_at",
        "is_triggered",
    ):
        op.drop_column("hypotheses", col)
