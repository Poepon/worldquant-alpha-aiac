"""Phase 1d: drop run_id columns + experiment_runs table

The HG/S/E pool has no per-run concept (lineage anchors on hypotheses.id /
candidate_queue). The legacy FLAT/ONESHOT ExperimentRun ("run") is retired:
the pool persister already wrote run_id=None, and Phase 1d removed the run_id
columns from the ORM models + the persist-chain constructors + runs.py.

This migration drops the now-unmapped DB columns + the experiment_runs table.
Dropping a column in Postgres auto-drops the FK constraint on it, so the three
run_id FK constraints (→ experiment_runs.id) are removed implicitly before the
table is dropped.

RUN ORDER (operator, in a maintenance window):
  1. Deploy the Phase 1d code (models without run_id / ExperimentRun) + restart
     uvicorn + celery + the pool supervisor so the running ORM no longer maps
     these columns (it tolerates the still-present DB columns until this runs).
  2. pg_dump backup (at least alphas / alpha_failures / trace_steps / experiment_runs).
  3. alembic upgrade head   (this migration).
  4. Restart + verify pool produces + /tasks + dashboard.

NOTE: MiningTask.schedule / last_alpha_persisted_at are NOT touched here (the
live pool writes them). The dead MiningTask columns (generation_strategy /
current_iteration / max_iterations / progress_current) are deferred to a
separate Phase 1d-2 that first rewires dashboard_service + the task serializers.

Revision ID: r3d8c1f5a9b2
Revises: q2e8b4d6f1a3
Create Date: 2026-06-06
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "r3d8c1f5a9b2"
down_revision = "q2e8b4d6f1a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) Drop the three run_id columns. In Postgres DROP COLUMN cascades to the
    #    FK constraint defined on that column, so the experiment_runs FKs go too.
    #    IF EXISTS keeps this idempotent if a column was already removed.
    op.execute("ALTER TABLE alphas DROP COLUMN IF EXISTS run_id")
    op.execute("ALTER TABLE alpha_failures DROP COLUMN IF EXISTS run_id")
    op.execute("ALTER TABLE trace_steps DROP COLUMN IF EXISTS run_id")

    # 2) Drop the experiment_runs table (no remaining referencers after step 1).
    op.execute("DROP TABLE IF EXISTS experiment_runs CASCADE")


def downgrade() -> None:
    # Best-effort structural restore (row data is NOT recovered — restore from
    # the pre-upgrade pg_dump backup if the legacy rows are needed).
    op.create_table(
        "experiment_runs",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("mining_tasks.id"), nullable=False),
        sa.Column("status", sa.String(length=50), server_default="RUNNING"),
        sa.Column("trigger_source", sa.String(length=50), server_default="API"),
        sa.Column("celery_task_id", sa.String(length=100)),
        sa.Column("config_snapshot", JSONB(), server_default=sa.text("'{}'::jsonb")),
        sa.Column("prompt_version", sa.String(length=100)),
        sa.Column("thresholds_version", sa.String(length=100)),
        sa.Column("strategy_snapshot", JSONB(), server_default=sa.text("'{}'::jsonb")),
        sa.Column("runtime_state", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("started_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column("error_message", sa.Text()),
    )
    op.add_column("alphas", sa.Column("run_id", sa.Integer(), sa.ForeignKey("experiment_runs.id"), nullable=True))
    op.add_column("alpha_failures", sa.Column("run_id", sa.Integer(), sa.ForeignKey("experiment_runs.id"), nullable=True))
    op.add_column("trace_steps", sa.Column("run_id", sa.Integer(), sa.ForeignKey("experiment_runs.id"), nullable=True))
