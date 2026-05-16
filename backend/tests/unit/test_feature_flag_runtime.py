"""B3: feature_flag_runtime async refresher coverage.

Source: docs/alphagbm_skills_research_2026-05-15.md ops dashboard plan §1.4.

Before this test, ``backend/feature_flag_runtime.py`` was entirely
untested — yet it's the only mechanism by which a flag flipped via
/ops/feature-flags propagates to a Celery worker process (whose
``settings.ENABLE_X`` cache is per-process).

We do NOT test the long-running loop (asyncio.sleep would slow CI) —
just ``_async_refresh_once`` which the loop calls each tick. Smoke tests:

1. happy path: refresher pulls DB rows into ``_flag_override_cache``
2. fail-open: DB exception is swallowed (the loop must never die)
3. start_async_refresher is idempotent (returns existing task on 2nd call)
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.models.config import FeatureFlagAudit, FeatureFlagOverride
from backend.services.feature_flag_service import (
    FeatureFlagService,
    _flag_override_cache,
)


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
def _clear_cache_and_task():
    _flag_override_cache.clear()
    # Also reset module-level _async_task so idempotency test starts clean
    import backend.feature_flag_runtime as ffrt
    ffrt._async_task = None
    yield
    _flag_override_cache.clear()
    ffrt._async_task = None


# ---------------------------------------------------------------------------
# _async_refresh_once
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_async_refresh_once_pulls_db_overrides_into_cache(session_maker):
    """Seed an override in DB → refresher → cache reflects it."""
    from backend.feature_flag_runtime import _async_refresh_once

    # Seed via FeatureFlagService.set (writes through to cache too — clear)
    async with session_maker() as db:
        await FeatureFlagService(db).set(
            "ENABLE_PILLAR_AWARE_SELECTION", True, actor="seed"
        )
    _flag_override_cache.clear()

    # Cold cache → refresher should re-populate from DB
    assert "ENABLE_PILLAR_AWARE_SELECTION" not in _flag_override_cache

    with patch("backend.database.AsyncSessionLocal", session_maker):
        await _async_refresh_once()

    assert _flag_override_cache.get("ENABLE_PILLAR_AWARE_SELECTION") is True


@pytest.mark.asyncio
async def test_async_refresh_once_swallows_db_failure():
    """Refresher must NEVER raise — long-running loop would die otherwise."""
    from backend.feature_flag_runtime import _async_refresh_once

    def _broken_factory(*args, **kwargs):
        raise ConnectionError("DB unreachable")

    with patch("backend.database.AsyncSessionLocal", _broken_factory):
        # Returns None — exceptions are swallowed
        result = await _async_refresh_once()
        assert result is None


@pytest.mark.asyncio
async def test_async_refresh_once_preserves_cache_on_db_failure(session_maker):
    """Pre-existing cache entries survive a DB blip — fail-open semantics."""
    from backend.feature_flag_runtime import _async_refresh_once

    # Prime cache with a value we expect to survive
    _flag_override_cache["ENABLE_PILLAR_AWARE_SELECTION"] = True

    def _broken_factory(*args, **kwargs):
        raise ConnectionError("DB unreachable")

    with patch("backend.database.AsyncSessionLocal", _broken_factory):
        await _async_refresh_once()

    # Cache untouched
    assert _flag_override_cache.get("ENABLE_PILLAR_AWARE_SELECTION") is True


# ---------------------------------------------------------------------------
# start_async_refresher — idempotency + uses get_running_loop (no deprecation)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_async_refresher_is_idempotent():
    """Second call returns the same task — no double-loop."""
    from backend.feature_flag_runtime import (
        start_async_refresher,
        stop_async_refresher,
    )

    t1 = start_async_refresher()
    t2 = start_async_refresher()
    assert t1 is t2
    await stop_async_refresher()


@pytest.mark.asyncio
async def test_start_async_refresher_uses_get_running_loop(monkeypatch):
    """Regression: was using deprecated asyncio.get_event_loop (3.14 removes
    the no-running-loop branch). Must use get_running_loop now."""
    import asyncio

    from backend.feature_flag_runtime import (
        start_async_refresher,
        stop_async_refresher,
    )

    calls = {"event_loop": 0, "running_loop": 0}
    orig_get_event_loop = asyncio.get_event_loop
    orig_get_running_loop = asyncio.get_running_loop

    def _spy_event(*a, **kw):
        calls["event_loop"] += 1
        return orig_get_event_loop(*a, **kw)

    def _spy_running(*a, **kw):
        calls["running_loop"] += 1
        return orig_get_running_loop(*a, **kw)

    monkeypatch.setattr(asyncio, "get_event_loop", _spy_event)
    monkeypatch.setattr(asyncio, "get_running_loop", _spy_running)

    start_async_refresher()
    await stop_async_refresher()

    assert calls["running_loop"] >= 1, "must use get_running_loop"
    assert calls["event_loop"] == 0, "must NOT use deprecated get_event_loop"
