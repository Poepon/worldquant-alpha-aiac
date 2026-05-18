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

    Bug M6 fix (review 2026-05-18):
      The blanket UPDATE that resets schedule / starting_tier / runtime_state
      back to defaults is dangerous while Phase 1.5-C is live. With
      ENABLE_TASK_SCHEMA_V2=ON, the cascade orchestrator reads cursor +
      tier state from runtime_state['current_tier'] / ['round_idx'] /
      ['progress'] — wiping those mid-flight silently regresses an
      in-cascade T2/T3 task back to T1 and the worker re-mines T1 from
      scratch, defeating the entire cascade.

      Two guards before the blanket UPDATE runs:
        1. Refuse if ENABLE_TASK_SCHEMA_V2 is ON. Check the runtime
           feature_flag_overrides DB row first (production source of truth)
           and fall back to settings.ENABLE_TASK_SCHEMA_V2 (env default).
           Operator must flip the flag OFF first.
        2. Refuse if any CONTINUOUS_CASCADE task is currently RUNNING.
           Even with the flag OFF, a live cascade still relies on
           runtime_state for its mining_tasks columns derived state.

      A logger.warning at downgrade start tells the operator how many rows
      will be affected.

    Old legacy columns (mining_mode / agent_mode / cascade_phase) were
    never touched, so legacy-mode read paths are still authoritative.
    """
    import json
    import logging
    from sqlalchemy import text

    logger = logging.getLogger("alembic.runtime.migration")
    bind = op.get_bind()

    # --- Bug M6 guard 1: refuse if ENABLE_TASK_SCHEMA_V2 is ON ---
    flag_on = False
    try:
        row = bind.execute(text(
            "SELECT flag_value FROM feature_flag_overrides "
            "WHERE flag_name = 'ENABLE_TASK_SCHEMA_V2'"
        )).fetchone()
        if row is not None and row[0] is not None:
            # flag_value is JSON-encoded text (per FeatureFlagOverride model)
            try:
                decoded = json.loads(row[0])
                flag_on = bool(decoded)
            except (ValueError, TypeError):
                # Fallback to truthy string check
                flag_on = str(row[0]).lower() in ("true", "1", "yes", "on")
    except Exception as ex:
        # feature_flag_overrides table may not exist on very old DBs
        logger.warning(
            "[phase15-B downgrade / Bug M6] could not read "
            "feature_flag_overrides: %s — falling back to env default", ex
        )

    if not flag_on:
        # Fall back to env-default from settings
        try:
            from backend.config import settings
            flag_on = bool(getattr(settings, "ENABLE_TASK_SCHEMA_V2", False))
        except Exception as ex:
            logger.warning(
                "[phase15-B downgrade / Bug M6] could not import settings: "
                "%s — assuming flag OFF", ex
            )

    if flag_on:
        raise Exception(
            "refusing downgrade: ENABLE_TASK_SCHEMA_V2 is ON; flip flag OFF "
            "first (POST /api/v1/ops/feature-flags with "
            "{name='ENABLE_TASK_SCHEMA_V2', value=false}) then retry. "
            "Downgrading while v2 reads are live would wipe in-flight "
            "runtime_state cursors and silently regress T2/T3 tasks to T1."
        )

    # --- Bug M6 guard 2: refuse if any live CONTINUOUS_CASCADE running ---
    running_count_row = bind.execute(text(
        "SELECT COUNT(*) FROM mining_tasks "
        "WHERE status = 'RUNNING' AND mining_mode = 'CONTINUOUS_CASCADE'"
    )).fetchone()
    running_count = int(running_count_row[0]) if running_count_row else 0
    if running_count > 0:
        raise Exception(
            f"refusing downgrade: {running_count} CONTINUOUS_CASCADE task(s) "
            "currently RUNNING. Stop / pause them via "
            "POST /api/v1/mining-session/stop first — blanket UPDATE would "
            "wipe their in-flight cascade cursors and force re-mine from T1."
        )

    # --- Pre-flight notice: how many rows will be reset ---
    mt_count_row = bind.execute(text(
        "SELECT COUNT(*) FROM mining_tasks"
    )).fetchone()
    er_count_row = bind.execute(text(
        "SELECT COUNT(*) FROM experiment_runs"
    )).fetchone()
    logger.warning(
        "[phase15-B downgrade] resetting schedule/starting_tier on %d "
        "mining_tasks rows and runtime_state on %d experiment_runs rows. "
        "Legacy mining_mode / agent_mode / cascade_phase columns untouched.",
        int(mt_count_row[0]) if mt_count_row else 0,
        int(er_count_row[0]) if er_count_row else 0,
    )

    op.execute("UPDATE mining_tasks SET schedule = 'ONESHOT', starting_tier = 1;")
    op.execute("UPDATE experiment_runs SET runtime_state = '{}'::jsonb;")
