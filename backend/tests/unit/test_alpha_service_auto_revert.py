"""B2: AlphaService._auto_revert_consultant_mode safety-net test.

P3-Brain plan §6.3. When ``brain_adapter.check_correlation_with_poll``
returns AUTH_DENIED (BRAIN 403 on PROD-corr), ``submit_alpha`` calls
``_auto_revert_consultant_mode`` which:

* opens an isolated ``AsyncSessionLocal`` (avoids @transactional nesting
  inside the in-flight submit transaction — would raise
  ``InvalidRequestError "A transaction is already begun"``)
* clears the ENABLE_BRAIN_CONSULTANT_MODE override
* writes an audit row with ``actor='system_auto_revert'``

Coverage gap before this test:
``test_check_correlation_with_poll.py`` only verified the adapter layer
translates 403 → AUTH_DENIED. The integration ``submit_alpha → _auto_revert``
chain was untested even though CLAUDE.md promises it as the only safety
net for accidental Consultant flag flips.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import MetaData, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.models.config import FeatureFlagAudit, FeatureFlagOverride
from backend.services.feature_flag_service import (
    FeatureFlagService,
    _flag_override_cache,
)


# ---------------------------------------------------------------------------
# Fixtures — isolated aiosqlite engine with just the feature-flag tables
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def ff_engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    isolated = MetaData()
    FeatureFlagOverride.__table__.to_metadata(isolated)
    FeatureFlagAudit.__table__.to_metadata(isolated)
    async with engine.begin() as conn:
        await conn.run_sync(isolated.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session_maker(ff_engine):
    return sessionmaker(ff_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _clear_cache():
    _flag_override_cache.clear()
    yield
    _flag_override_cache.clear()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_auto_revert_clears_override_and_writes_audit(session_maker):
    """_auto_revert: clear override row + audit with actor=system_auto_revert."""
    from backend.services.alpha_service import AlphaService

    # Seed: ENABLE_BRAIN_CONSULTANT_MODE=True via the ops console path
    async with session_maker() as setup_db:
        await FeatureFlagService(setup_db).set(
            "ENABLE_BRAIN_CONSULTANT_MODE",
            True,
            actor="ops_console",
            note="initial activation",
        )
        # transactional decorator commits — no explicit commit needed

    # Sanity: override row exists, cache primed
    async with session_maker() as check_db:
        rows = (await check_db.execute(
            select(FeatureFlagOverride).where(
                FeatureFlagOverride.flag_name == "ENABLE_BRAIN_CONSULTANT_MODE"
            )
        )).scalars().all()
        assert len(rows) == 1

    # Patch AsyncSessionLocal so _auto_revert's "isolated session" lands
    # on our test engine (not the prod one which isn't initialized).
    with patch("backend.database.AsyncSessionLocal", session_maker):
        svc = AlphaService(MagicMock())  # outer db unused by _auto_revert
        await svc._auto_revert_consultant_mode(
            "BRAIN PROD-corr 返回 403 — 账号未实际授权 Consultant",
        )

    # 1. Override row deleted
    async with session_maker() as verify_db:
        rows = (await verify_db.execute(
            select(FeatureFlagOverride).where(
                FeatureFlagOverride.flag_name == "ENABLE_BRAIN_CONSULTANT_MODE"
            )
        )).scalars().all()
        assert rows == [], "auto-revert must delete the override row"

        # 2. Audit row recorded with the system actor
        audits = (await verify_db.execute(
            select(FeatureFlagAudit).where(
                FeatureFlagAudit.flag_name == "ENABLE_BRAIN_CONSULTANT_MODE"
            )
        )).scalars().all()
        clear_audits = [a for a in audits if a.action == "clear"]
        assert len(clear_audits) == 1
        assert clear_audits[0].actor == "system_auto_revert"
        assert "PROD-corr" in (clear_audits[0].note or "")


@pytest.mark.asyncio
async def test_auto_revert_swallows_failure_silently(session_maker):
    """Safety-net must NEVER raise — would break the parent submit path."""
    from backend.services.alpha_service import AlphaService

    # AsyncSessionLocal that raises on entry
    def _bad_session_factory():
        raise ConnectionError("DB connection refused")

    with patch("backend.database.AsyncSessionLocal", _bad_session_factory):
        svc = AlphaService(MagicMock())
        # Must not raise
        await svc._auto_revert_consultant_mode("trigger reason")


@pytest.mark.asyncio
async def test_auto_revert_uses_isolated_session_not_submit_session(session_maker):
    """_auto_revert opens its own session — verifies via different session
    identity than the one passed to AlphaService."""
    from backend.services.alpha_service import AlphaService

    # Seed override
    async with session_maker() as setup_db:
        await FeatureFlagService(setup_db).set(
            "ENABLE_BRAIN_CONSULTANT_MODE", True, actor="ops", note="x"
        )

    sessions_opened = []
    original = session_maker

    def _tracking_factory(*args, **kwargs):
        s = original(*args, **kwargs)
        sessions_opened.append(s)
        return s

    fake_outer_session = MagicMock(spec=AsyncSession)

    with patch("backend.database.AsyncSessionLocal", _tracking_factory):
        svc = AlphaService(fake_outer_session)
        await svc._auto_revert_consultant_mode("test")

    # _auto_revert opened a session, NOT reusing svc.db
    assert len(sessions_opened) >= 1
    # And svc.db was untouched (no executions on the outer session)
    fake_outer_session.execute.assert_not_called()
