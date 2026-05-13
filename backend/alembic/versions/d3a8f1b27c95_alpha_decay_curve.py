"""alphas.decay_curve JSONB for OS-metric decay tracking

Per the TODO #1 plan (cost-near-zero schema + collection now; analysis later):
adds a JSONB list on each alpha row that the daily Celery beat
(`refresh_os_alpha_metrics`) appends to weekly. Each entry is a snapshot of
the alpha's OS metrics at a point in time:

    {
      "snapshot_date": "2026-05-14",
      "days_since_submit": 30,
      "sharpe": 1.42,
      "fitness": 1.08,
      "turnover": 0.45,
      "returns": 0.0021,
      "drawdown": 0.08,
      "margin": 0.00015
    }

Storage: ~150 bytes per entry * weekly cadence * 1 year = ~7.5 KB per alpha.
At 500 OS alphas that's <4 MB total — well under any reason to defer.

NOT NULL DEFAULT '[]' so backfill is trivial; no analysis code needs to
handle the legacy NULL case.

Revision ID: d3a8f1b27c95
Revises: f1c4e83d72a6
Create Date: 2026-05-14
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "d3a8f1b27c95"
down_revision: Union[str, Sequence[str], None] = "f1c4e83d72a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "alphas",
        sa.Column(
            "decay_curve",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("alphas", "decay_curve")
