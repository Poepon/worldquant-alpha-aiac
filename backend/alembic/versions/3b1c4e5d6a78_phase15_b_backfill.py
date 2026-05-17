"""phase15_b_backfill — derive schedule/starting_tier from legacy columns

Revision ID: 3b1c4e5d6a78
Revises: 7a3f9e1c2b8d
Create Date: 2026-05-17

Phase 1.5-B (plan v1.3 §2). Data-only migration — no DDL changes. Backfills
the columns added in Revision A from the legacy mining_mode / agent_mode /
cascade_phase fields so the new columns become semantically meaningful for
historical rows BEFORE Phase 1.5-C cut-over reads from them.

Backfill rules (plan §2.2):
  schedule:
    'CASCADE'  when mining_mode = 'CONTINUOUS_CASCADE'
    'ONESHOT'  otherwise
  starting_tier:
    1  when mining_mode = 'CONTINUOUS_CASCADE'
    2  when agent_mode = 'AUTONOMOUS_TIER2'
    3  when agent_mode = 'AUTONOMOUS_TIER3'
    1  otherwise (AUTONOMOUS / AUTONOMOUS_TIER1 / INTERACTIVE / NULL)
  runtime_state (latest ExperimentRun per task only — older runs keep
                 default '{}' from Revision A server_default):
    current_tier: 1/2/3 from cascade_phase 'T1'/'T2'/'T3', else starting_tier
    round_idx:    COALESCE(mt.cascade_round_idx, 0)
    progress:     COALESCE(mt.progress_current, 0)
    iteration:    COALESCE(mt.current_iteration, 0)
    last_persisted_at: ISO string of mt.last_alpha_persisted_at when not NULL

Pre-flight requirement (plan v1.3 §2.4 SF-V1.4-E + V1.2-B4):
  SELECT COUNT(*) FROM mining_tasks WHERE agent_mode='INTERACTIVE'
  must equal 0. If non-zero, STOP and ping user — backfill default treats
  INTERACTIVE rows as starting_tier=1 but that's a soft assumption.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '3b1c4e5d6a78'
down_revision: Union[str, Sequence[str], None] = '7a3f9e1c2b8d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Backfill new columns from legacy. Data-only; no DDL."""
    # === Part 1: mining_tasks.schedule + starting_tier ===
    op.execute("""
        UPDATE mining_tasks SET
            schedule = CASE
                WHEN mining_mode = 'CONTINUOUS_CASCADE' THEN 'CASCADE'
                ELSE 'ONESHOT'
            END,
            starting_tier = CASE
                WHEN mining_mode = 'CONTINUOUS_CASCADE' THEN 1
                WHEN agent_mode = 'AUTONOMOUS_TIER2' THEN 2
                WHEN agent_mode = 'AUTONOMOUS_TIER3' THEN 3
                ELSE 1
            END;
    """)
    # generation_strategy already filled by server_default '["llm"]'::jsonb
    # in Revision A — no backfill needed.

    # === Part 2: experiment_runs.runtime_state for latest run per task ===
    # Older runs keep '{}' default — only the latest run gets snapshot of
    # current cascade state. Phase 1.5-C cut-over reads from this.
    op.execute("""
        WITH latest_runs AS (
            SELECT DISTINCT ON (task_id) id, task_id
            FROM experiment_runs
            ORDER BY task_id, id DESC
        )
        UPDATE experiment_runs er
        SET runtime_state = jsonb_build_object(
            'current_tier', CASE mt.cascade_phase
                WHEN 'T1' THEN 1
                WHEN 'T2' THEN 2
                WHEN 'T3' THEN 3
                ELSE mt.starting_tier
            END,
            'round_idx', COALESCE(mt.cascade_round_idx, 0),
            'progress', COALESCE(mt.progress_current, 0),
            'iteration', COALESCE(mt.current_iteration, 0),
            'last_persisted_at',
                CASE
                    WHEN mt.last_alpha_persisted_at IS NULL THEN NULL
                    ELSE to_jsonb(mt.last_alpha_persisted_at::text)
                END
        )
        FROM mining_tasks mt
        WHERE er.id IN (SELECT id FROM latest_runs)
          AND er.task_id = mt.id;
    """)


def downgrade() -> None:
    """Reset backfilled values to server_defaults.

    Old legacy columns (mining_mode / agent_mode / cascade_phase) were
    never touched, so no data loss. Read paths still authoritative.
    """
    op.execute("UPDATE mining_tasks SET schedule = 'ONESHOT', starting_tier = 1;")
    op.execute("UPDATE experiment_runs SET runtime_state = '{}'::jsonb;")
