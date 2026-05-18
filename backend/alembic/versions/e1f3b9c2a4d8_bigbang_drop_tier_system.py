"""bigbang drop tier system: factor_tier + starting_tier + agent_mode + mining_mode + target_tier

Revision ID: e1f3b9c2a4d8
Revises: c3f9a7d2e4b8
Create Date: 2026-05-18

Tier System Removal big-bang per master plan v1.8 §5 (Ship #7). Drops all
remaining tier-related schema after Ships #1-#6 closed every code-side
caller:

  * alphas.factor_tier            (SMALLINT, partial index + composite
                                   index ix_alphas_tier_can_submit)
  * knowledge_entries.factor_tier (SMALLINT, partial index)
  * hypotheses.target_tier        (Integer, index)
  * mining_tasks.starting_tier    (Integer)
  * mining_tasks.agent_mode       (String)
  * mining_tasks.mining_mode      (String)

Plus:
  * Replace ix_alphas_tier_can_submit (factor_tier, can_submit) with a
    single-column ix_alphas_can_submit (can_submit WHERE can_submit IS NOT
    NULL) so the /alphas refresh-can-submit batch endpoint still has a
    selective index for the can_submit IS NOT NULL filter.
  * DELETE 3 retired feature_flag_override rows: ENABLE_FACTOR_TIERING /
    ENABLE_T2_SELF_CORR_CHECK / TIER_SEED_LOAD_REFRESH_VIA_BRAIN.

Operational pre-conditions for apply:
  * Ships #1-#6 deployed (backend + frontend code-tree tier-free; AST +
    importlib PASS verified per §18 ship log).
  * KB pillar backfill verified ≥80% non-other via
    ``scripts/backfill_kb_hypothesis_pillar.py`` (plan §6 step 6-1).
  * Slim CSV backup taken for rollback per plan §6 step 6-2:
      psql \\copy alphas (id, factor_tier) TO '/backups/alphas_tier_<date>.csv' CSV
      psql \\copy mining_tasks (id, starting_tier, agent_mode, mining_mode) TO '/backups/mt_tier_<date>.csv' CSV
      psql \\copy knowledge_entries (id, factor_tier) TO '/backups/kb_tier_<date>.csv' CSV
      psql \\copy hypotheses (id, target_tier) TO '/backups/hyp_tier_<date>.csv' CSV
  * Services stopped: celery shutdown + UPDATE mining_tasks SET status='STOPPED'
    WHERE status IN ('RUNNING','PAUSED') (plan §6 step 6c).
  * SET lock_timeout='30s' guards the ~100k-row alphas DROP COLUMN which
    takes ACCESS EXCLUSIVE.

Downgrade re-adds columns NULLABLE; original values NOT restored — must
restore from the §6 step 6-2 CSV backup per plan §10 rollback playbook.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "e1f3b9c2a4d8"
down_revision: Union[str, Sequence[str], None] = "c3f9a7d2e4b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Plan §5: 30s lock_timeout protects the alphas DROP COLUMN (~100k
    # rows takes ACCESS EXCLUSIVE). With celery stopped + all RUNNING
    # tasks STOPPED per §6 step 6b/6c, lock should be free immediately.
    op.execute("SET lock_timeout='30s'")

    # 1. alphas: factor_tier column + 2 indexes (partial single-column +
    # composite). Replace composite with single-column can_submit index so
    # the refresh-can-submit batch endpoint still has a selective lookup.
    op.execute("DROP INDEX IF EXISTS ix_alphas_factor_tier")
    op.execute("DROP INDEX IF EXISTS ix_alphas_tier_can_submit")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_alphas_can_submit "
        "ON alphas (can_submit) WHERE can_submit IS NOT NULL"
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'alphas' AND column_name = 'factor_tier'
          ) THEN
            ALTER TABLE alphas DROP COLUMN factor_tier;
          END IF;
        END $$;
        """
    )

    # 2. knowledge_entries.factor_tier (top-level column + partial index)
    op.execute("DROP INDEX IF EXISTS ix_kb_factor_tier")
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'knowledge_entries' AND column_name = 'factor_tier'
          ) THEN
            ALTER TABLE knowledge_entries DROP COLUMN factor_tier;
          END IF;
        END $$;
        """
    )

    # 3. hypotheses.target_tier (integer + plain index)
    op.execute("DROP INDEX IF EXISTS ix_hypotheses_target_tier")
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'hypotheses' AND column_name = 'target_tier'
          ) THEN
            ALTER TABLE hypotheses DROP COLUMN target_tier;
          END IF;
        END $$;
        """
    )

    # 4. mining_tasks: starting_tier + agent_mode + mining_mode. The
    # mining_mode partial index uq_active_cascade_per_region was already
    # dropped in d8a2f15b9c63 / phase15-D PR3b vintage migrations; if any
    # straggler indexes survive, IF EXISTS makes them idempotent.
    op.execute("DROP INDEX IF EXISTS uq_active_cascade_per_region")
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'mining_tasks' AND column_name = 'starting_tier'
          ) THEN
            ALTER TABLE mining_tasks DROP COLUMN starting_tier;
          END IF;
          IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'mining_tasks' AND column_name = 'agent_mode'
          ) THEN
            ALTER TABLE mining_tasks DROP COLUMN agent_mode;
          END IF;
          IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'mining_tasks' AND column_name = 'mining_mode'
          ) THEN
            ALTER TABLE mining_tasks DROP COLUMN mining_mode;
          END IF;
        END $$;
        """
    )

    # 5. Clean retired feature_flag_override rows. The flags themselves
    # were already deleted from config.py + SUPPORTED_FLAGS in Ship Phase
    # 1.3, so any surviving DB row is orphan and would surface as warning
    # noise in /ops/flags. IF EXISTS via DELETE is naturally idempotent.
    op.execute(
        """
        DELETE FROM feature_flag_overrides
        WHERE flag_name IN (
          'ENABLE_FACTOR_TIERING',
          'ENABLE_T2_SELF_CORR_CHECK',
          'TIER_SEED_LOAD_REFRESH_VIA_BRAIN'
        )
        """
    )


def downgrade() -> None:
    # Re-add columns as nullable with safe defaults. Original values are
    # NOT restored — operators must reload from the §6 step 6-2 CSV backup
    # per plan §10 rollback playbook. IF NOT EXISTS keeps this idempotent.
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'alphas' AND column_name = 'factor_tier'
          ) THEN
            ALTER TABLE alphas ADD COLUMN factor_tier SMALLINT NULL;
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'knowledge_entries' AND column_name = 'factor_tier'
          ) THEN
            ALTER TABLE knowledge_entries ADD COLUMN factor_tier SMALLINT NULL;
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'hypotheses' AND column_name = 'target_tier'
          ) THEN
            ALTER TABLE hypotheses ADD COLUMN target_tier INTEGER NULL DEFAULT 1;
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'mining_tasks' AND column_name = 'starting_tier'
          ) THEN
            ALTER TABLE mining_tasks
              ADD COLUMN starting_tier INTEGER NOT NULL DEFAULT 1;
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'mining_tasks' AND column_name = 'agent_mode'
          ) THEN
            ALTER TABLE mining_tasks
              ADD COLUMN agent_mode VARCHAR(50) NULL DEFAULT 'AUTONOMOUS';
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'mining_tasks' AND column_name = 'mining_mode'
          ) THEN
            ALTER TABLE mining_tasks
              ADD COLUMN mining_mode VARCHAR(30) NOT NULL DEFAULT 'DISCRETE';
          END IF;
        END $$;
        """
    )

    # Re-create the indexes the upgrade dropped.
    op.execute("DROP INDEX IF EXISTS ix_alphas_can_submit")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_alphas_factor_tier "
        "ON alphas (factor_tier) WHERE factor_tier IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_alphas_tier_can_submit "
        "ON alphas (factor_tier, can_submit)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_kb_factor_tier "
        "ON knowledge_entries (factor_tier) WHERE factor_tier IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_hypotheses_target_tier "
        "ON hypotheses (target_tier)"
    )

    # Feature flag rows are NOT restored — operator must re-insert via
    # /ops/flags PATCH if the rollback is permanent.
