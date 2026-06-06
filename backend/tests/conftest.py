"""
Pytest Configuration and Fixtures

This file provides:
- Async test configuration
- Database fixtures (in-memory SQLite for tests)
- Mock factory fixtures
- Common test utilities
"""

import asyncio
import sys
import os
from typing import AsyncGenerator, Generator
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.database import SQLAlchemyBase

# P2 review fix (2026-05-16): Warmup `backend.tasks` before any test imports
# `backend.agents`. Pairs with the root-cause fix at evaluation.py (removed
# top-level backend.tasks import). Belt-and-suspenders for future tests
# that may re-introduce the cycle. See 6+ existing integration test files
# using the same warmup pattern.
import backend.tasks  # noqa: F401


# JSONB-on-SQLite shim (2026-05-18): models use PostgreSQL JSONB / ARRAY
# for production. The in-memory SQLite fixture below can't compile those
# types, so 51 unit tests (test_services + test_repositories) ERRORed on
# metadata.create_all(). Three dispatch hooks cover the full surface:
#
#   1. JSONB → JSON column type fallback
#   2. ARRAY → JSON column type fallback
#   3. CreateTable DDL post-process: strip PG cast suffixes (``::jsonb`` /
#      ``::text``) from server_default literals — required because Phase
#      1.5-MF-V1.4 chose ``text("'X'::jsonb")`` server_defaults for
#      production atomicity, which SQLite parses as "unrecognized token".
#
# Round-trips correctly for tests that don't use JSONB-specific operators
# (``?``, ``@>``, ``->>``); those tests should carry
# ``@pytest.mark.requires_postgres`` anyway and are skipped here.
import re  # noqa: E402

from sqlalchemy.ext.compiler import compiles  # noqa: E402
from sqlalchemy.dialects.postgresql import ARRAY, JSONB  # noqa: E402
from sqlalchemy.schema import CreateTable  # noqa: E402
from sqlalchemy.sql import compiler as _sql_compiler  # noqa: E402


@compiles(JSONB, "sqlite")
def _jsonb_to_json_sqlite(type_, compiler, **kw):  # pragma: no cover - dispatch
    return "JSON"


@compiles(ARRAY, "sqlite")
def _array_to_json_sqlite(type_, compiler, **kw):  # pragma: no cover - dispatch
    return "JSON"


_PG_CAST_RE = re.compile(r"::\w+")


@compiles(CreateTable, "sqlite")
def _create_table_sqlite_strip_pg_casts(element, compiler, **kw):  # pragma: no cover
    """Render default DDL then strip PostgreSQL ``::cast`` suffixes so
    ``server_default=text("'[]'::jsonb")`` becomes the SQLite-valid
    ``DEFAULT '[]'``. SQLite's DDLCompiler doesn't override
    visit_create_table, so calling the base class method is equivalent
    to the default render path."""
    rendered = _sql_compiler.DDLCompiler.visit_create_table(compiler, element)
    return _PG_CAST_RE.sub("", rendered)


# =============================================================================
# Phase 1.5-A [V1.2-C3] requires_postgres mark (plan v1.3 §1.5.1)
# =============================================================================
# Register the mark and implement collection-modifying hook so tests carrying
# @pytest.mark.requires_postgres skip when PG_TEST_DSN env var is unset.
# Without this, the mark is silently ignored and migration tests run on
# aiosqlite where JSONB server_default behavior diverges.
#
# To exercise the migration tests locally:
#   $env:PG_TEST_DSN = "postgresql+asyncpg://aiac:aiac@localhost:5433/aiac_test"
#   pytest backend/tests/migration -v

def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "requires_postgres: mark test requiring real PostgreSQL "
        "(skipped if PG_TEST_DSN unset).",
    )


def pytest_collection_modifyitems(config, items):
    if not os.getenv("PG_TEST_DSN"):
        skip_pg = pytest.mark.skip(
            reason="requires PG_TEST_DSN env var to point at real PostgreSQL"
        )
        for item in items:
            if item.get_closest_marker("requires_postgres"):
                item.add_marker(skip_pg)


# =============================================================================
# Async Configuration
# =============================================================================

@pytest.fixture(scope="session")
def event_loop() -> Generator:
    """Create an instance of the default event loop for each test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


# =============================================================================
# Database Fixtures
# =============================================================================

@pytest_asyncio.fixture(scope="function")
async def async_engine():
    """Create an async in-memory SQLite engine for testing."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    
    async with engine.begin() as conn:
        await conn.run_sync(SQLAlchemyBase.metadata.create_all)
    
    yield engine
    
    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def db_session(async_engine) -> AsyncGenerator[AsyncSession, None]:
    """Create a new database session for each test."""
    async_session_maker = sessionmaker(
        async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    
    async with async_session_maker() as session:
        yield session
        await session.rollback()


# =============================================================================
# Mock Fixtures
# =============================================================================

@pytest.fixture
def mock_brain_adapter():
    """Get a mock BrainAdapter for testing."""
    from backend.tests.fixtures.mock_brain import MockBrainAdapter
    return MockBrainAdapter()


@pytest.fixture
def mock_llm_service():
    """Get a mock LLMService for testing."""
    from backend.tests.fixtures.mock_llm import MockLLMService
    return MockLLMService()


# =============================================================================
# Model Factory Fixtures
# =============================================================================

@pytest_asyncio.fixture
async def sample_task(db_session):
    """Create a sample mining task for testing."""
    from backend.models import MiningTask
    
    task = MiningTask(
        task_name="Test Task",
        region="USA",
        universe="TOP3000",
        dataset_strategy="AUTO",
        target_datasets=[],
        status="PENDING",
        daily_goal=4,
        config={},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return task


@pytest_asyncio.fixture
async def sample_alpha(db_session, sample_task):
    """Create a sample alpha for testing."""
    from backend.models import Alpha
    
    alpha = Alpha(
        alpha_id="test-alpha-001",
        task_id=sample_task.id,
        expression="rank(close)",
        expression_hash="abc123",
        region="USA",
        universe="TOP3000",
        status="created",
        quality_status="PENDING",
        human_feedback="NONE",
        is_sharpe=1.5,
        is_fitness=0.8,
        is_turnover=0.3,
    )
    db_session.add(alpha)
    await db_session.commit()
    await db_session.refresh(alpha)
    return alpha


@pytest_asyncio.fixture
async def sample_knowledge_entry(db_session):
    """Create a sample knowledge entry for testing."""
    from backend.models import KnowledgeEntry
    
    entry = KnowledgeEntry(
        entry_type="SUCCESS_PATTERN",
        pattern="rank(ts_mean(close, 5))",
        description="Simple momentum pattern",
        meta_data={"category": "momentum"},
        usage_count=10,
        is_active=True,
    )
    db_session.add(entry)
    await db_session.commit()
    await db_session.refresh(entry)
    return entry


# =============================================================================
# Service Fixtures
# =============================================================================

@pytest_asyncio.fixture
async def alpha_service(db_session):
    """Get an AlphaService instance for testing."""
    from backend.services import AlphaService
    return AlphaService(db_session)


@pytest_asyncio.fixture
async def dashboard_service(db_session):
    """Get a DashboardService instance for testing."""
    from backend.services import DashboardService
    return DashboardService(db_session)


# mining_service fixture retired in Phase 1d (MiningService deleted; unused by any test)


# =============================================================================
# Repository Fixtures
# =============================================================================

@pytest_asyncio.fixture
async def alpha_repository(db_session):
    """Get an AlphaRepository instance for testing."""
    from backend.repositories import AlphaRepository
    return AlphaRepository(db_session)


@pytest_asyncio.fixture
async def task_repository(db_session):
    """Get a TaskRepository instance for testing."""
    from backend.repositories import TaskRepository
    return TaskRepository(db_session)


@pytest_asyncio.fixture
async def knowledge_repository(db_session):
    """Get a KnowledgeRepository instance for testing."""
    from backend.repositories import KnowledgeRepository
    return KnowledgeRepository(db_session)
