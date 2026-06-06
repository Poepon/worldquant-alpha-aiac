"""Phase 1d-2: drop dead MiningTask columns

After the pool cutover, four MiningTask columns are dead (never written by the
pool, only legacy ONESHOT/FLAT code touched them; that code was removed in
Phase 1c-delete / 1d):

  - progress_current   (no writer; dashboard now counts real alphas)
  - current_iteration  (writer increment_iteration retired)
  - max_iterations     (writer create_task retired)
  - generation_strategy (0 readers/writers)

Phase 1d-2 removed them from the ORM model + the serializers (task_service
TaskSummary/TaskDetail now emit 0 for the kept-shape fields; dashboard_service
rewired to count alphas) + retired the orphaned repo methods + MiningSessionInfo.

KEPT (NOT dropped — the live pool writes them):
  - schedule              (hydrate.py writes 'POOL' on the resident task)
  - last_alpha_persisted_at (persistence.py heartbeat on every persist)

RUN ORDER (operator, maintenance window — same as Phase 1d):
  1. Deploy this code + restart (uvicorn/celery/pool) so the ORM no longer maps
     the columns (it tolerates the still-present DB columns until this runs).
  2. pg_dump backup (mining_tasks).
  3. alembic upgrade head   (this migration).
  4. Restart + verify pool produces + /tasks 200 + dashboard.

Revision ID: s4f1c9e7b2a6
Revises: r3d8c1f5a9b2
Create Date: 2026-06-06
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "s4f1c9e7b2a6"
down_revision = "r3d8c1f5a9b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE mining_tasks DROP COLUMN IF EXISTS progress_current")
    op.execute("ALTER TABLE mining_tasks DROP COLUMN IF EXISTS current_iteration")
    op.execute("ALTER TABLE mining_tasks DROP COLUMN IF EXISTS max_iterations")
    op.execute("ALTER TABLE mining_tasks DROP COLUMN IF EXISTS generation_strategy")


def downgrade() -> None:
    # Best-effort structural restore (values not recovered — restore from backup).
    op.add_column("mining_tasks", sa.Column("progress_current", sa.Integer(), server_default="0"))
    op.add_column("mining_tasks", sa.Column("current_iteration", sa.Integer(), server_default="0"))
    op.add_column("mining_tasks", sa.Column("max_iterations", sa.Integer(), server_default="10"))
    op.add_column(
        "mining_tasks",
        sa.Column("generation_strategy", JSONB(), server_default=sa.text("'[\"llm\"]'::jsonb"), nullable=False),
    )
