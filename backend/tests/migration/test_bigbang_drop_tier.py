"""Static-source verification for the Tier System Removal big-bang migration.

The PG-only DO $$ blocks can't be replayed against sqlite, so this is a
file-contents check — same pattern as test_phase15_d_drop_cols.py.

Verifies the file as shipped in Ship #7 (revision e1f3b9c2a4d8) matches
the scope locked in master plan v1.8 §5:
  * 6 columns dropped (factor_tier × 2 tables + target_tier +
    starting_tier + agent_mode + mining_mode)
  * 4 indexes dropped + 1 new index created (ix_alphas_can_submit)
  * 3 feature_flag_override rows deleted
  * IF EXISTS / IF NOT EXISTS guards everywhere (idempotent)
  * SET lock_timeout='30s' header
  * down_revision points at c3f9a7d2e4b8 (phase15-D PR3)
"""
from __future__ import annotations

import inspect


import backend.alembic.versions.e1f3b9c2a4d8_bigbang_drop_tier_system as mig


SRC = inspect.getsource(mig)


def test_revision_and_chain():
    """Revision id matches filename + down_revision links to phase15-D PR3."""
    assert mig.revision == "e1f3b9c2a4d8"
    assert mig.down_revision == "c3f9a7d2e4b8"


def test_drops_alphas_factor_tier_plus_indexes():
    """alphas.factor_tier + partial index + composite index all in upgrade()."""
    assert "DROP INDEX IF EXISTS ix_alphas_factor_tier" in SRC
    assert "DROP INDEX IF EXISTS ix_alphas_tier_can_submit" in SRC
    assert "ALTER TABLE alphas DROP COLUMN factor_tier" in SRC


def test_creates_replacement_can_submit_index():
    """ix_alphas_can_submit replaces ix_alphas_tier_can_submit so the
    refresh-can-submit batch endpoint still has a selective index for
    the can_submit IS NOT NULL filter."""
    assert "ix_alphas_can_submit" in SRC
    assert "WHERE can_submit IS NOT NULL" in SRC


def test_drops_knowledge_entries_factor_tier():
    assert "DROP INDEX IF EXISTS ix_kb_factor_tier" in SRC
    assert "ALTER TABLE knowledge_entries DROP COLUMN factor_tier" in SRC


def test_drops_hypotheses_target_tier():
    assert "DROP INDEX IF EXISTS ix_hypotheses_target_tier" in SRC
    assert "ALTER TABLE hypotheses DROP COLUMN target_tier" in SRC


def test_drops_mining_tasks_three_columns():
    for col in ("starting_tier", "agent_mode", "mining_mode"):
        assert f"ALTER TABLE mining_tasks DROP COLUMN {col}" in SRC


def test_deletes_retired_feature_flag_overrides():
    """3 flag names cleaned to drop orphan rows surfaced as /ops/flags noise."""
    assert "ENABLE_FACTOR_TIERING" in SRC
    assert "ENABLE_T2_SELF_CORR_CHECK" in SRC
    assert "TIER_SEED_LOAD_REFRESH_VIA_BRAIN" in SRC
    assert "DELETE FROM feature_flag_overrides" in SRC


def test_has_lock_timeout_guard():
    """30s lock_timeout protects the ~100k-row alphas DROP COLUMN."""
    assert "SET lock_timeout='30s'" in SRC


def test_uses_idempotency_guards():
    """All DDL wrapped in DO $$ + IF EXISTS / IF NOT EXISTS so a partial
    mid-flight failure can be safely replayed."""
    # Upgrade DROPs use IF EXISTS for both indexes and (inside DO $$) cols
    assert SRC.count("IF EXISTS") >= 8     # 5 cols + indexes
    # Downgrade ADDs use IF NOT EXISTS
    assert SRC.count("IF NOT EXISTS") >= 6  # 5 cols re-add + 1 index


def test_downgrade_restores_columns_nullable():
    """Downgrade re-adds columns NULLABLE (original values not restored —
    operator must reload from CSV per plan §10 rollback playbook)."""
    assert "ADD COLUMN factor_tier SMALLINT NULL" in SRC
    assert "ADD COLUMN target_tier INTEGER NULL" in SRC
    assert "ADD COLUMN starting_tier" in SRC
    assert "ADD COLUMN agent_mode VARCHAR(50) NULL" in SRC
    assert "ADD COLUMN mining_mode" in SRC


def test_orm_models_have_dropped_columns_removed():
    """Lockstep check: ORM model definitions in backend/models must match
    the post-migration schema. If a column survives in the ORM but the
    migration drops it, SELECT * will fail on first query."""
    from backend.models import Alpha, KnowledgeEntry, Hypothesis, MiningTask

    assert "factor_tier" not in {c.name for c in Alpha.__table__.columns}
    assert "factor_tier" not in {c.name for c in KnowledgeEntry.__table__.columns}
    assert "target_tier" not in {c.name for c in Hypothesis.__table__.columns}

    mt_cols = {c.name for c in MiningTask.__table__.columns}
    assert "starting_tier" not in mt_cols
    assert "agent_mode" not in mt_cols
    assert "mining_mode" not in mt_cols
