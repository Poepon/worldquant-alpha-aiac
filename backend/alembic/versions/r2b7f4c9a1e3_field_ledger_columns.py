"""Field-coverage exploration ledger: +6 columns on datafield_cell_stats

Adds the per-(field, universe, delay) mining-ledger columns for the
orthogonal-breadth field-exploration loop (PR-A, 2026-06-09). All additive +
ONLINE-SAFE (nullable or NOT NULL with server_default → PG 11+ metadata-only
add, no table rewrite, no worker stop). The running OLD code never SELECTs them
→ INERT until the new code / ENABLE_FIELD_SCREENING flag deploy.

  - times_mined     INT  DEFAULT 0  — distinct alphas whose expr uses this field
  - distinct_alphas INT  DEFAULT 0
  - signal_p90      FLOAT NULL       — p90 IS Sharpe of this field's alphas
  - band_pass_count INT  DEFAULT 0   — # clearing the eval band
  - orthogonality   FLOAT NULL       — informational (1 - mean self_corr); NOT reward
  - last_mined      TIMESTAMP NULL   — most recent alpha using this field

Populated by run_field_ledger_refresh (gated). Idempotent (IF NOT EXISTS).

Revision ID: r2b7f4c9a1e3
Revises: t1a9c3e5b7d2
Create Date: 2026-06-09
"""
from alembic import op


revision = "r2b7f4c9a1e3"
down_revision = "t1a9c3e5b7d2"
branch_labels = None
depends_on = None


_COLS = [
    "times_mined INTEGER NOT NULL DEFAULT 0",
    "distinct_alphas INTEGER NOT NULL DEFAULT 0",
    "signal_p90 DOUBLE PRECISION",
    "band_pass_count INTEGER NOT NULL DEFAULT 0",
    "orthogonality DOUBLE PRECISION",
    "last_mined TIMESTAMP",
]


def upgrade() -> None:
    for col in _COLS:
        op.execute(f"ALTER TABLE datafield_cell_stats ADD COLUMN IF NOT EXISTS {col}")


def downgrade() -> None:
    for name in ("times_mined", "distinct_alphas", "signal_p90",
                 "band_pass_count", "orthogonality", "last_mined"):
        op.execute(f"ALTER TABLE datafield_cell_stats DROP COLUMN IF EXISTS {name}")
