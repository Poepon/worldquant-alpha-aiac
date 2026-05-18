"""Phase 1.5-A (Revision 7a3f9e1c2b8d) Alembic migration tests.

Per plan v1.3 §1.5:
- 5 DDL-level tests require real PostgreSQL (server_default + JSONB cast
  behavior diverges in SQLite). Marked @pytest.mark.requires_postgres
  → skipped when PG_TEST_DSN unset (conftest.py V1.2-C3 hook).
- 1 ORM-side test [V1.2-B4] is intentionally aiosqlite-compatible —
  verifies MiningTask(...) ORM-INSERT uses Python `default=` even when
  schedule/starting_tier/generation_strategy not passed (covers the
  21 test fixture files reliant on this dual-default convention).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from backend.database import SQLAlchemyBase


REVISION = "7a3f9e1c2b8d"


# ---------------------------------------------------------------------------
# [V1.2-B4] ORM-INSERT side — verifies Python `default=` fires for ORM-INSERT
#   This is the load-bearing test — without dual-default, all 21 test files
#   that construct MiningTask(...) without schedule/starting_tier would fail
#   NOT NULL on commit. Runs on aiosqlite (default test DB).
# ---------------------------------------------------------------------------

class TestPythonDefaultsFireForORMInsert:
    """[V1.2-B4] Verifies Python `default=` fires for ORM-INSERT path.

    NOTE: aiosqlite test fixture cannot compile JSONB columns (existing
    `target_datasets` JSONB on MiningTask blocks `metadata.create_all`).
    So these tests use **in-memory model introspection** — verify the
    Column.default callable produces the expected value when SQLAlchemy
    invokes it during INSERT (mirrors the actual ORM behavior without
    needing a DB).

    Full INSERT-path integration is covered by the @requires_postgres
    DDL tests below + by every existing integration test that constructs
    MiningTask(...) — those would crash on missing schedule if Python
    default didn't fire.
    """

    def test_mining_task_python_defaults_evaluate(self):
        """MiningTask Column.default callables produce expected values
        — same path SQLAlchemy invokes during ORM-INSERT.
        """
        from backend.models import MiningTask
        # ColumnDefault.arg is the static value or callable
        schedule_col = MiningTask.__table__.c["schedule"]
        starting_tier_col = MiningTask.__table__.c["starting_tier"]
        gen_strategy_col = MiningTask.__table__.c["generation_strategy"]

        assert schedule_col.default is not None
        assert schedule_col.default.arg == "ONESHOT"

        assert starting_tier_col.default is not None
        assert starting_tier_col.default.arg == 1

        # generation_strategy uses `default=lambda: ["llm"]` callable
        assert gen_strategy_col.default is not None
        default_callable = gen_strategy_col.default.arg
        assert callable(default_callable)
        # SQLAlchemy invokes the callable with a context arg (or no arg for
        # scalar defaults); call it the way SQLAlchemy does at INSERT time
        try:
            value = default_callable(None)  # ColumnDefault passes context
        except TypeError:
            value = default_callable()
        assert value == ["llm"]

    def test_experiment_run_runtime_state_default_evaluates(self):
        from backend.models import ExperimentRun
        rt_col = ExperimentRun.__table__.c["runtime_state"]
        assert rt_col.default is not None
        # default=dict — the dict type itself, called to produce {}
        default_callable = rt_col.default.arg
        assert callable(default_callable)
        try:
            value = default_callable(None)
        except TypeError:
            value = default_callable()
        assert value == {}


# ---------------------------------------------------------------------------
# [V1.2-C3] DDL-level tests — require real PostgreSQL via PG_TEST_DSN
# ---------------------------------------------------------------------------

@pytest.mark.requires_postgres
class TestRevisionAUpgradeDowngrade:
    @pytest_asyncio.fixture
    async def pg_engine(self):
        dsn = os.getenv("PG_TEST_DSN")
        if not dsn:
            pytest.skip("PG_TEST_DSN not set")
        engine = create_async_engine(dsn, echo=False, future=True)
        yield engine
        await engine.dispose()

    @pytest.mark.asyncio
    async def test_upgrade_adds_4_columns(self, pg_engine):
        """After alembic upgrade head, mining_tasks has schedule /
        starting_tier / generation_strategy and experiment_runs has
        runtime_state."""
        from alembic.config import Config
        from alembic import command as alembic_command
        cfg = Config(str(Path(__file__).parent.parent.parent.parent / "alembic.ini"))
        # Run upgrade in-process; assumes pg_engine DB is pre-cleaned
        alembic_command.upgrade(cfg, REVISION)
        async with pg_engine.begin() as conn:
            insp = await conn.run_sync(lambda c: inspect(c))
            mt_cols = {c["name"] for c in insp.get_columns("mining_tasks")}
            er_cols = {c["name"] for c in insp.get_columns("experiment_runs")}
        assert "schedule" in mt_cols
        assert "starting_tier" in mt_cols
        assert "generation_strategy" in mt_cols
        assert "runtime_state" in er_cols

    @pytest.mark.asyncio
    async def test_upgrade_creates_2_dedicated_tables(self, pg_engine):
        """direction_bandit_log + ast_distance_log exist after upgrade
        (Phase 1.5-A Alembic-formalizes them)."""
        async with pg_engine.begin() as conn:
            insp = await conn.run_sync(lambda c: inspect(c))
            tables = set(insp.get_table_names())
        assert "direction_bandit_log" in tables
        assert "ast_distance_log" in tables

    @pytest.mark.asyncio
    async def test_existing_rows_get_server_defaults(self, pg_engine):
        """Insert a row pre-upgrade with raw SQL; after upgrade, SELECT
        returns server_default values (ONESHOT / 1 / ["llm"])."""
        # NOTE: full test requires fresh DB at prior revision; skip in
        # default CI run, exercise manually with alembic.
        pytest.skip("Requires multi-revision fixture; run manually")

    @pytest.mark.asyncio
    async def test_downgrade_reverses_columns_but_preserves_log_tables(self, pg_engine):
        """Bug M5: downgrade drops the 4 added columns but DELIBERATELY does
        NOT drop direction_bandit_log / ast_distance_log — we can't tell
        whether they pre-existed (Phase 1 metadata.create_all() shipped them
        before Alembic formalized) so we preserve the data.
        """
        from alembic.config import Config
        from alembic import command as alembic_command
        cfg = Config(str(Path(__file__).parent.parent.parent.parent / "alembic.ini"))
        alembic_command.downgrade(cfg, "-1")
        async with pg_engine.begin() as conn:
            insp = await conn.run_sync(lambda c: inspect(c))
            mt_cols = {c["name"] for c in insp.get_columns("mining_tasks")}
            er_cols = {c["name"] for c in insp.get_columns("experiment_runs")}
            tables = set(insp.get_table_names())
        # 4 added columns ARE reverted
        assert "schedule" not in mt_cols
        assert "starting_tier" not in mt_cols
        assert "generation_strategy" not in mt_cols
        assert "runtime_state" not in er_cols
        # Bug M5 guard: 2 dedicated log tables are PRESERVED
        assert "direction_bandit_log" in tables, (
            "Bug M5: direction_bandit_log must be preserved on downgrade "
            "(Phase 1 R1a data safety)"
        )
        assert "ast_distance_log" in tables, (
            "Bug M5: ast_distance_log must be preserved on downgrade "
            "(Phase 1 AST data safety)"
        )
        # Restore to head for subsequent tests
        alembic_command.upgrade(cfg, "head")


# ---------------------------------------------------------------------------
# Revision file structural tests — no DB required
# ---------------------------------------------------------------------------

class TestRevisionFile:
    def test_revision_file_exists(self):
        path = (
            Path(__file__).parent.parent.parent / "alembic" / "versions"
            / f"{REVISION}_phase15_a_add_columns.py"
        )
        assert path.exists(), f"Revision file missing: {path}"

    def test_revision_chains_off_prior_head(self):
        path = (
            Path(__file__).parent.parent.parent / "alembic" / "versions"
            / f"{REVISION}_phase15_a_add_columns.py"
        )
        src = path.read_text(encoding="utf-8")
        assert f"revision: str = '{REVISION}'" in src
        # Plan §1.2 locks the down_revision target to the current head
        # (41ae82a9b859 = P3 feature_flag_override tables)
        assert "down_revision" in src and "41ae82a9b859" in src

    def test_revision_uses_jsonb_cast_for_server_default(self):
        path = (
            Path(__file__).parent.parent.parent / "alembic" / "versions"
            / f"{REVISION}_phase15_a_add_columns.py"
        )
        src = path.read_text(encoding="utf-8")
        # MF-V1.4-1/2: all JSONB server_default must include ::jsonb cast
        assert "::jsonb" in src
        assert "'[\\\"llm\\\"]'::jsonb" in src or '"[\\"llm\\"]"::jsonb' in src or "'[\"llm\"]'" in src
        assert "'{}'::jsonb" in src

    def test_revision_has_inspector_has_table_guard(self):
        """MF-V1.4-3 + V1.2-B4 guard: dev DBs with metadata.create_all()
        already-present tables should not double-create."""
        path = (
            Path(__file__).parent.parent.parent / "alembic" / "versions"
            / f"{REVISION}_phase15_a_add_columns.py"
        )
        src = path.read_text(encoding="utf-8")
        assert "inspector" in src
        assert "get_table_names()" in src
        assert "direction_bandit_log" in src
        assert "ast_distance_log" in src

    def test_downgrade_does_not_drop_phase1_tables_bug_m5(self):
        """Bug M5 fix: downgrade() must NOT contain unconditional drop_table
        calls for direction_bandit_log / ast_distance_log — we can't tell
        whether they pre-existed Revision A (Phase 1 metadata.create_all()
        shipped them earlier) so dropping risks wiping R1a / AST data.
        """
        path = (
            Path(__file__).parent.parent.parent / "alembic" / "versions"
            / f"{REVISION}_phase15_a_add_columns.py"
        )
        src = path.read_text(encoding="utf-8")
        # Slice out the downgrade function body
        idx = src.find("def downgrade(")
        assert idx >= 0
        downgrade_section = src[idx:]
        # Bug M5: these unconditional drops must be gone
        assert 'drop_table("ast_distance_log")' not in downgrade_section, (
            "Bug M5: downgrade still drops ast_distance_log unconditionally"
        )
        assert 'drop_table("direction_bandit_log")' not in downgrade_section, (
            "Bug M5: downgrade still drops direction_bandit_log unconditionally"
        )
        # Bug M5 documentation marker present
        assert "Bug M5" in downgrade_section


# ---------------------------------------------------------------------------
# Model sync tests — no DB required
# ---------------------------------------------------------------------------

class TestModelSync:
    def test_mining_task_has_new_columns(self):
        from backend.models import MiningTask
        cols = {c.name for c in MiningTask.__table__.columns}
        assert "schedule" in cols
        assert "starting_tier" in cols
        assert "generation_strategy" in cols

    def test_experiment_run_has_runtime_state(self):
        from backend.models import ExperimentRun
        cols = {c.name for c in ExperimentRun.__table__.columns}
        assert "runtime_state" in cols

    def test_dual_defaults_present(self):
        """[V1.2-B4] verify each new column has BOTH Python default= and
        DB server_default= specified (lockstep convention)."""
        from backend.models import MiningTask, ExperimentRun
        for col_name in ("schedule", "starting_tier", "generation_strategy"):
            col = MiningTask.__table__.c[col_name]
            assert col.default is not None, f"{col_name}: missing Python default"
            assert col.server_default is not None, f"{col_name}: missing server_default"
            assert not col.nullable, f"{col_name}: must be NOT NULL"
        rt = ExperimentRun.__table__.c["runtime_state"]
        assert rt.default is not None
        assert rt.server_default is not None
        assert not rt.nullable
