"""Phase 15-D PR3: migration file sanity tests (2026-05-18).

Static-source verification — the migration file is shipped READY but
NOT applied automatically. Tests check the file contents to catch
copy-paste / scope-creep mistakes before operator runs alembic upgrade.
"""
from __future__ import annotations

import inspect


def test_migration_file_only_drops_two_cascade_cols():
    """PR3 explicitly scoped to drop ONLY cascade_phase + cascade_round_idx.
    mining_mode + uq_active_cascade_per_region deferred to PR3c."""
    import backend.alembic.versions.c3f9a7d2e4b8_phase15_d_drop_legacy_cascade_cols as mig
    src = inspect.getsource(mig)
    # The two cols MUST be present
    assert "DROP COLUMN cascade_phase" in src
    assert "DROP COLUMN cascade_round_idx" in src
    # mining_mode MUST NOT be dropped this PR
    assert "DROP COLUMN mining_mode" not in src
    # Partial index MUST NOT be dropped this PR
    assert "DROP INDEX IF EXISTS uq_active_cascade_per_region" not in src
    # IF EXISTS guards for idempotency
    assert "IF EXISTS" in src
    # Downgrade restores both
    assert "ADD COLUMN cascade_phase" in src
    assert "ADD COLUMN cascade_round_idx" in src


def test_migration_revises_r8_query_log():
    """Chain check: revises b2e5c9f1d847 (R8 query log)."""
    import backend.alembic.versions.c3f9a7d2e4b8_phase15_d_drop_legacy_cascade_cols as mig
    assert mig.revision == "c3f9a7d2e4b8"
    assert mig.down_revision == "b2e5c9f1d847"


def test_migration_docstring_warns_against_premature_apply():
    """Docstring must warn that ORM still declares the columns +
    PR3b is required before apply."""
    import backend.alembic.versions.c3f9a7d2e4b8_phase15_d_drop_legacy_cascade_cols as mig
    doc = mig.__doc__ or ""
    assert "DO NOT apply" in doc or "PR3b" in doc
    assert "8 production files" in doc or "8 readers" in doc or "8 reader" in doc


def test_orm_columns_dropped_in_pr3b():
    """PR3b (2026-05-18): cascade_phase + cascade_round_idx removed from ORM
    in lockstep with migration apply. mining_mode was also dropped in the
    later tier-system removal big-bang (Ship #5, revision e1f3b9c2a4d8)."""
    from backend.models import MiningTask
    cols = {c.name for c in MiningTask.__table__.columns}
    assert "cascade_phase" not in cols
    assert "cascade_round_idx" not in cols
    # mining_mode dropped post tier-system removal (Ship #5 + #7).
    assert "mining_mode" not in cols
