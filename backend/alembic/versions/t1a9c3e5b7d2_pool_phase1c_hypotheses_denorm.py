"""Pool Phase 2 (1c): hypotheses cognitive-reconcile denorm + pool dedup key

Adds four columns to ``hypotheses`` — all additive + ONLINE-SAFE (nullable, or
NOT NULL with a server_default → PG 11+ metadata-only add, no table rewrite, no
worker stop required; the running OLD code never SELECTs them so they are inert
until the new code deploys):

  - hyp_intent_id    INT NULL    — pool 1a lease-recycle dedup key (indexed)
  - can_submit_count INT NOT NULL DEFAULT 0 — 1c PROMOTE gate (NOT pass_count,
                                   which counts PASS_PROVISIONAL — guard #5)
  - submitted_count  INT NOT NULL DEFAULT 0 — 1c realized-submission rollup
  - attribution      VARCHAR(20) NULL — 1c heuristic attribution stamp

Read by node_hypothesis (dedup) + the run_pool_cognitive_reconcile beat.
Idempotent (IF NOT EXISTS) so a re-run / partial-apply is safe.

Revision ID: t1a9c3e5b7d2
Revises: s4f1c9e7b2a6
Create Date: 2026-06-07
"""
from alembic import op


revision = "t1a9c3e5b7d2"
down_revision = "s4f1c9e7b2a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE hypotheses ADD COLUMN IF NOT EXISTS hyp_intent_id INTEGER")
    op.execute(
        "ALTER TABLE hypotheses ADD COLUMN IF NOT EXISTS "
        "can_submit_count INTEGER NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE hypotheses ADD COLUMN IF NOT EXISTS "
        "submitted_count INTEGER NOT NULL DEFAULT 0"
    )
    op.execute("ALTER TABLE hypotheses ADD COLUMN IF NOT EXISTS attribution VARCHAR(20)")
    # Matches the model's ``index=True`` (plain b-tree, name ix_hypotheses_<col>)
    # — the dedup lookup find_open_by_intent filters on hyp_intent_id.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_hypotheses_hyp_intent_id "
        "ON hypotheses (hyp_intent_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_hypotheses_hyp_intent_id")
    op.execute("ALTER TABLE hypotheses DROP COLUMN IF EXISTS attribution")
    op.execute("ALTER TABLE hypotheses DROP COLUMN IF EXISTS submitted_count")
    op.execute("ALTER TABLE hypotheses DROP COLUMN IF EXISTS can_submit_count")
    op.execute("ALTER TABLE hypotheses DROP COLUMN IF EXISTS hyp_intent_id")
