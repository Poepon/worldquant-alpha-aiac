"""Phase 3 Q10 PR2e: Alembic migration smoke tests (2026-05-18).

Plan: ~/.claude/plans/phase3-q10-pyqlib-prescreen-2026-05-18.md v1.3 §7.6.

Two test layers:
  - Module-level smoke (no DB) — verifies the migration file imports,
    declares the correct revision + down_revision, and references the
    expected table + indexes. Catches typos / wrong revision chaining
    even in CI without Postgres.
  - DDL-level requires-postgres tests — exercise actual upgrade /
    downgrade against a live PG via PG_TEST_DSN. Skipped when env unset
    (conftest.py [V1.2-C3] hook).
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine


REVISION = "c5d9e1f3a7b8"
DOWN_REVISION = "b3c8d9e2f4a1"  # R8 GIN
TABLE_NAME = "qlib_prescreen_log"
EXPECTED_INDEXES = {
    "ix_q10_task_id",
    "ix_q10_created_at",
    "ix_q10_verdict",
    "ix_q10_expr_hash",
}


# ---------------------------------------------------------------------------
# Module-level smoke (no DB required)
# ---------------------------------------------------------------------------

def _load_migration_module():
    """Import the migration file directly (Alembic version files aren't
    auto-importable as backend.alembic.versions.* in tests)."""
    root = Path(__file__).resolve().parent.parent.parent.parent
    path = (
        root / "backend" / "alembic" / "versions"
        / f"{REVISION}_phase3_q10_qlib_prescreen_log.py"
    )
    assert path.exists(), f"Q10 migration file missing: {path}"
    spec = importlib.util.spec_from_file_location(
        f"alembic_versions_{REVISION}", path,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_file_imports_cleanly():
    """Smoke — migration file is valid Python and importable."""
    mod = _load_migration_module()
    assert hasattr(mod, "upgrade")
    assert hasattr(mod, "downgrade")
    assert callable(mod.upgrade)
    assert callable(mod.downgrade)


def test_migration_revision_id_and_chain():
    """Revision IDs match the plan + chain to the R8 GIN head."""
    mod = _load_migration_module()
    assert mod.revision == REVISION
    assert mod.down_revision == DOWN_REVISION
    assert mod.branch_labels is None


def test_migration_references_expected_table_and_indexes():
    """upgrade() source mentions the qlib_prescreen_log table + 4 indexes."""
    import inspect as _inspect
    mod = _load_migration_module()
    src = _inspect.getsource(mod.upgrade)
    assert TABLE_NAME in src
    for ix in EXPECTED_INDEXES:
        assert ix in src, f"upgrade() missing index {ix!r}"
    # Downgrade also drops the indexes + table
    down_src = _inspect.getsource(mod.downgrade)
    assert TABLE_NAME in down_src
    for ix in EXPECTED_INDEXES:
        assert ix in down_src, f"downgrade() missing index {ix!r}"


def test_alembic_head_resolves_to_q10_revision():
    """The current Alembic head IS the Q10 revision."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    root = Path(__file__).resolve().parent.parent.parent.parent
    cfg = Config(str(root / "backend" / "alembic.ini"))
    sd = ScriptDirectory.from_config(cfg)
    heads = sd.get_heads()
    assert REVISION in heads, (
        f"Q10 revision {REVISION} not in Alembic heads {heads} — "
        "did a later migration get added that didn't chain?"
    )


# ---------------------------------------------------------------------------
# DDL-level requires-postgres tests
# ---------------------------------------------------------------------------

@pytest.mark.requires_postgres
class TestQ10UpgradeDowngrade:
    @pytest_asyncio.fixture
    async def pg_engine(self):
        dsn = os.getenv("PG_TEST_DSN")
        if not dsn:
            pytest.skip("PG_TEST_DSN not set")
        engine = create_async_engine(dsn, echo=False, future=True)
        yield engine
        await engine.dispose()

    @pytest.mark.asyncio
    async def test_upgrade_creates_table_and_indexes(self, pg_engine):
        """alembic upgrade head → qlib_prescreen_log table + 4 indexes exist."""
        from alembic.config import Config
        from alembic import command as alembic_command
        root = Path(__file__).resolve().parent.parent.parent.parent
        cfg = Config(str(root / "backend" / "alembic.ini"))
        alembic_command.upgrade(cfg, REVISION)
        async with pg_engine.begin() as conn:
            insp = await conn.run_sync(lambda c: inspect(c))
            tables = set(insp.get_table_names())
            indexes = set()
            if TABLE_NAME in tables:
                indexes = {ix["name"] for ix in insp.get_indexes(TABLE_NAME)}
        assert TABLE_NAME in tables
        for ix in EXPECTED_INDEXES:
            assert ix in indexes, f"index {ix} missing post-upgrade"

    @pytest.mark.asyncio
    async def test_downgrade_drops_table_and_indexes(self, pg_engine):
        """alembic downgrade -1 → qlib_prescreen_log gone (no orphan indexes)."""
        from alembic.config import Config
        from alembic import command as alembic_command
        root = Path(__file__).resolve().parent.parent.parent.parent
        cfg = Config(str(root / "backend" / "alembic.ini"))
        alembic_command.downgrade(cfg, "-1")
        async with pg_engine.begin() as conn:
            insp = await conn.run_sync(lambda c: inspect(c))
            tables = set(insp.get_table_names())
        assert TABLE_NAME not in tables, (
            "qlib_prescreen_log should be dropped after downgrade"
        )
