"""Integration: self_corr Redis cache (P3-Brain plan §6.4).

Verifies the two-state cache (submit:self_corr_passed:{id} = "1") in
alpha_service.submit_alpha:
  - cache miss → CorrelationService called → cache written on PASS
  - cache hit → CorrelationService skipped (no BRAIN /correlations/SELF)
  - Redis exception → silent fallback (CorrelationService still runs)
  - self_corr FAIL → cache NOT written (no false-shortcut on retry)

Uses AsyncMock for db + redis to avoid SQLite JSONB compile errors
(MiningTask/Alpha/Hypothesis models pull in JSONB + PG ARRAY + interval
types not natively supported by SQLite). Focus is on control-flow,
not SQL — the JSONB-aware path is exercised by Postgres CI.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.config import _flag_override_cache
from backend.services.correlation_service import CorrSource


@pytest.fixture(autouse=True)
def _clear_flag_cache():
    _flag_override_cache.clear()
    yield
    _flag_override_cache.clear()


@pytest.fixture
def fake_redis_store():
    """In-memory dict shared by the fake_redis_mock — for assertion."""
    return {}


@pytest.fixture
def fake_redis_mock(fake_redis_store):
    """AsyncMock simulating the tiny redis surface submit_alpha uses."""
    redis = AsyncMock()

    async def _get(key):
        return fake_redis_store.get(key)

    async def _set(key, value, **kw):
        fake_redis_store[key] = value
        return True

    async def _setex(key, ttl, value):
        fake_redis_store[key] = value
        return True

    redis.get = AsyncMock(side_effect=_get)
    redis.set = AsyncMock(side_effect=_set)
    redis.setex = AsyncMock(side_effect=_setex)
    redis.eval = AsyncMock(return_value=1)
    return redis


def _make_alpha_row(alpha_id="brain-aid-1"):
    """Build a SimpleNamespace mimicking a SQLAlchemy Alpha row."""
    return SimpleNamespace(
        id=42,
        alpha_id=alpha_id,
        region="USA",
        universe="TOP3000",
        date_submitted=None,
        can_submit=True,
        metrics={},
    )


@pytest.fixture
def mock_brain_adapter(fake_redis_mock):
    """BrainAdapter mock with _get_slot_redis + submit_alpha success."""
    brain = AsyncMock()
    brain._get_slot_redis = AsyncMock(return_value=fake_redis_mock)
    brain.submit_alpha = AsyncMock(return_value={"success": True, "status_code": 200})

    async def _aenter():
        return brain
    async def _aexit(*a):
        return None
    brain.__aenter__ = _aenter
    brain.__aexit__ = _aexit
    return brain


@pytest.fixture
def mock_db_with_alpha():
    """AsyncSession mock returning a submittable alpha row."""
    alpha = _make_alpha_row()
    scalar_result = MagicMock()
    scalar_result.scalar_one_or_none = MagicMock(return_value=alpha)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=scalar_result)
    db.refresh = AsyncMock()
    db.commit = AsyncMock()
    return db, alpha


@pytest.mark.asyncio
async def test_cache_miss_calls_correlation_service_and_writes_cache(
    mock_db_with_alpha, mock_brain_adapter, fake_redis_store,
):
    """First submit: cache empty → CorrelationService called → cache written on PASS."""
    db, alpha = mock_db_with_alpha
    from backend.services.alpha_service import AlphaService
    svc = AlphaService(db)
    # alpha_repo also queried during portfolio refresh — stub on the service
    svc.alpha_repo = MagicMock()

    mock_get_with_fallback = AsyncMock(return_value=(0.3, CorrSource.LOCAL))
    with (
        patch(
            "backend.services.correlation_service.CorrelationService.get_with_fallback",
            new=mock_get_with_fallback,
        ),
        patch(
            "backend.agents.seed_pool.portfolio_skeletons.refresh_portfolio_from_db",
            new=AsyncMock(return_value=None),
        ),
    ):
        result = await svc.submit_alpha(alpha.id, brain_adapter=mock_brain_adapter)

    assert result["submitted"] is True
    mock_get_with_fallback.assert_awaited_once()
    # cache written on PASS
    assert fake_redis_store.get("submit:self_corr_passed:brain-aid-1") == "1"


@pytest.mark.asyncio
async def test_cache_hit_skips_correlation_service(
    mock_db_with_alpha, mock_brain_adapter, fake_redis_store,
):
    """Second submit (cache=='1'): CorrelationService NOT called."""
    db, alpha = mock_db_with_alpha
    fake_redis_store["submit:self_corr_passed:brain-aid-1"] = "1"

    from backend.services.alpha_service import AlphaService
    svc = AlphaService(db)
    svc.alpha_repo = MagicMock()

    mock_get_with_fallback = AsyncMock(return_value=(0.3, CorrSource.LOCAL))
    with (
        patch(
            "backend.services.correlation_service.CorrelationService.get_with_fallback",
            new=mock_get_with_fallback,
        ),
        patch(
            "backend.agents.seed_pool.portfolio_skeletons.refresh_portfolio_from_db",
            new=AsyncMock(return_value=None),
        ),
    ):
        result = await svc.submit_alpha(alpha.id, brain_adapter=mock_brain_adapter)

    assert result["submitted"] is True
    # ZERO CorrelationService calls — cache shortcut took it
    mock_get_with_fallback.assert_not_called()


@pytest.mark.asyncio
async def test_redis_exception_falls_back_to_correlation_service(
    mock_db_with_alpha, mock_brain_adapter, fake_redis_mock,
):
    """Redis errors are non-fatal — submit proceeds without cache shortcut."""
    db, alpha = mock_db_with_alpha
    # Make redis.get raise — cache check should swallow it and fall through
    fake_redis_mock.get = AsyncMock(side_effect=ConnectionError("redis down"))

    from backend.services.alpha_service import AlphaService
    svc = AlphaService(db)
    svc.alpha_repo = MagicMock()

    mock_get_with_fallback = AsyncMock(return_value=(0.3, CorrSource.LOCAL))
    with (
        patch(
            "backend.services.correlation_service.CorrelationService.get_with_fallback",
            new=mock_get_with_fallback,
        ),
        patch(
            "backend.agents.seed_pool.portfolio_skeletons.refresh_portfolio_from_db",
            new=AsyncMock(return_value=None),
        ),
    ):
        result = await svc.submit_alpha(alpha.id, brain_adapter=mock_brain_adapter)

    assert result["submitted"] is True
    # CorrelationService still called (cache miss path on exception)
    mock_get_with_fallback.assert_awaited_once()


@pytest.mark.asyncio
async def test_cache_not_written_when_self_corr_fails(
    mock_db_with_alpha, mock_brain_adapter, fake_redis_store,
):
    """self_corr 0.85 → submit blocked → cache MUST NOT be written
    (would falsely shortcut future retries to "passed")."""
    db, alpha = mock_db_with_alpha

    from backend.services.alpha_service import AlphaService
    svc = AlphaService(db)
    svc.alpha_repo = MagicMock()

    mock_get_with_fallback = AsyncMock(return_value=(0.85, CorrSource.LOCAL))
    with patch(
        "backend.services.correlation_service.CorrelationService.get_with_fallback",
        new=mock_get_with_fallback,
    ):
        result = await svc.submit_alpha(alpha.id, brain_adapter=mock_brain_adapter)

    assert result["submitted"] is False
    assert "self_corr" in result["reason"]
    # cache absent (write only happens on PASS)
    assert "submit:self_corr_passed:brain-aid-1" not in fake_redis_store
