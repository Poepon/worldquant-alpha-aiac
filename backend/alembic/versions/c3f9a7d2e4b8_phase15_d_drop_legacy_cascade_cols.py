"""phase15-D: drop legacy cascade columns (cascade_phase / cascade_round_idx)

Revision ID: c3f9a7d2e4b8
Revises: b2e5c9f1d847
Create Date: 2026-05-18

Phase 15-D PR3 (2026-05-18) per master plan v1.75+. The MIGRATION FILE
is shipped so operators can apply at their pace; the ORM model still
declares the columns + readers in 8 production files still reference
them. The "apply" step is intentionally separate from "ship" — once
the operator runs ``alembic upgrade head``, PR3b follow-up MUST drop
the ORM columns + update the 8 reader sites to read
``run.runtime_state["current_tier"]`` / ``["round_idx"]`` instead.

**DO NOT apply this migration to a deployment whose code has not been
updated for PR3b column-reader removal.** SELECT * FROM mining_tasks
will fail when the ORM model declares columns that no longer exist.

NOT dropped this PR (intentionally scoped tighter):
  * mining_mode — still used by FLAT_CONTINUOUS dispatch in
    run_mining_task L444 + flat-F1 main loop. Dropping requires a
    larger refactor moving FLAT detection to schedule/config-based
    plumbing. Deferred to phase15-D PR3c.
  * uq_active_cascade_per_region — same partial index dependency on
    mining_mode; defer with the column.

Operational pre-conditions for apply:
  * PR3b shipped (ORM columns removed + 8 readers migrated)
  * `/api/v1/ops/cascade-deprecation/drain` ran — all PAUSED cascade
    rows STOPPED with audit
  * `ENABLE_CASCADE_LEGACY` flag OFF in production ≥7d
  * `/api/v1/ops/cascade-deprecation/readiness` returns
    `ready_to_delete=True`
  * Dev DB cascade rows drained 2026-05-18 (manual ops_drain test)

Downgrade re-adds columns as NULLABLE with defaults; original values
NOT restored. IF EXISTS guards make this idempotent + replay-safe.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c3f9a7d2e4b8"
down_revision: Union[str, Sequence[str], None] = "b2e5c9f1d847"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF EXISTS guards make this idempotent. NB: we deliberately do NOT
    # drop mining_mode or uq_active_cascade_per_region — those depend
    # on the FLAT path migration (PR3b).
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'mining_tasks' AND column_name = 'cascade_phase'
          ) THEN
            ALTER TABLE mining_tasks DROP COLUMN cascade_phase;
          END IF;
          IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'mining_tasks' AND column_name = 'cascade_round_idx'
          ) THEN
            ALTER TABLE mining_tasks DROP COLUMN cascade_round_idx;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    # Re-add columns as nullable with defaults; original values NOT
    # restored — operators must consult config["cascade_drained"] audit
    # trail for forensic recovery. Idempotent via IF NOT EXISTS.
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'mining_tasks' AND column_name = 'cascade_phase'
          ) THEN
            ALTER TABLE mining_tasks
              ADD COLUMN cascade_phase VARCHAR(10) NULL;
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'mining_tasks' AND column_name = 'cascade_round_idx'
          ) THEN
            ALTER TABLE mining_tasks
              ADD COLUMN cascade_round_idx INTEGER NOT NULL DEFAULT 0;
          END IF;
        END $$;
        """
    )
