"""Integration: alpha_service PROD-corr 3rd gate + auto-revert safety net.

Plan §6.2 + §6.3. Validates the Consultant-only path through submit_alpha:
  - Consultant mode + check_correlation_with_poll returns OK with max=0.85
    → submit blocked with prod_corr_max in reason
  - Consultant mode + OK with max=0.3 → submit proceeds to BRAIN
  - Consultant mode + AUTH_DENIED → auto-revert flag (independent session)
    AND submit returned with retryable=False
  - Consultant mode + PENDING → blocked with retryable=True
  - User mode (flag=False) → check_correlation_with_poll never called
"""
from __future__ import annotations

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


def _make_alpha(alpha_id="brain-aid-1"):
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
def mock_db():
    alpha = _make_alpha()
    scalar_result = MagicMock()
    scalar_result.scalar_one_or_none = MagicMock(return_value=alpha)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=scalar_result)
    db.refresh = AsyncMock()
    db.commit = AsyncMock()
    return db, alpha


@pytest.fixture
def mock_brain_with_redis():
    """BrainAdapter mock — exposes _get_slot_redis + submit_alpha +
    check_correlation_with_poll. Test sets _prod_corr_response on the
    fixture to control PROD-corr behavior."""
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=True)
    redis.setex = AsyncMock(return_value=True)

    brain = AsyncMock()
    brain._get_slot_redis = AsyncMock(return_value=redis)
    brain.submit_alpha = AsyncMock(return_value={"success": True, "status_code": 200})
    # Test sets this; default = OK with safe max
    brain.check_correlation_with_poll = AsyncMock(
        return_value={"status": "OK", "data": {"max": 0.3}},
    )

    async def _aenter():
        return brain
    async def _aexit(*a):
        return None
    brain.__aenter__ = _aenter
    brain.__aexit__ = _aexit
    return brain


async def _run_submit(db, alpha_id, brain):
    """Helper: instantiate AlphaService + patch CorrelationService to PASS
    self_corr, then call submit_alpha."""
    from backend.services.alpha_service import AlphaService
    svc = AlphaService(db)
    svc.alpha_repo = MagicMock()
    with (
        patch(
            "backend.services.correlation_service.CorrelationService.get_with_fallback",
            new=AsyncMock(return_value=(0.3, CorrSource.LOCAL)),
        ),
        patch(
            "backend.agents.seed_pool.portfolio_skeletons.refresh_portfolio_from_db",
            new=AsyncMock(return_value=None),
        ),
    ):
        return await svc.submit_alpha(alpha_id, brain_adapter=brain)


# ---------------------------------------------------------------------------
# User mode: PROD-corr endpoint MUST NOT be called
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_user_mode_never_calls_prod_correlation(mock_db, mock_brain_with_redis):
    db, alpha = mock_db
    result = await _run_submit(db, alpha.id, mock_brain_with_redis)
    assert result["submitted"] is True
    # 0 PROD-corr calls in User mode (strict isolation §14)
    mock_brain_with_redis.check_correlation_with_poll.assert_not_called()


# ---------------------------------------------------------------------------
# Consultant mode: PROD-corr gate behavior
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_consultant_mode_passes_when_prod_corr_low(mock_db, mock_brain_with_redis):
    _flag_override_cache["ENABLE_BRAIN_CONSULTANT_MODE"] = True
    db, alpha = mock_db
    mock_brain_with_redis.check_correlation_with_poll = AsyncMock(
        return_value={"status": "OK", "data": {"max": 0.3}},
    )
    result = await _run_submit(db, alpha.id, mock_brain_with_redis)
    assert result["submitted"] is True
    mock_brain_with_redis.check_correlation_with_poll.assert_awaited_once()
    # submit_alpha was called (proceeded past gate)
    mock_brain_with_redis.submit_alpha.assert_awaited_once()


@pytest.mark.asyncio
async def test_consultant_mode_blocks_when_prod_corr_high(mock_db, mock_brain_with_redis):
    """max=0.85 ≥ 0.7 → blocked with prod_corr_max in reason; submit_alpha NOT called."""
    _flag_override_cache["ENABLE_BRAIN_CONSULTANT_MODE"] = True
    db, alpha = mock_db
    mock_brain_with_redis.check_correlation_with_poll = AsyncMock(
        return_value={"status": "OK", "data": {"max": 0.85}},
    )
    result = await _run_submit(db, alpha.id, mock_brain_with_redis)
    assert result["submitted"] is False
    assert "0.85" in result["reason"] or "prod_corr_max" in result["reason"]
    assert result.get("prod_corr_max") == 0.85
    mock_brain_with_redis.submit_alpha.assert_not_called()


@pytest.mark.asyncio
async def test_consultant_mode_pending_returns_retryable(mock_db, mock_brain_with_redis):
    """PENDING (BRAIN still computing) → blocked with retryable=True; submit NOT called."""
    _flag_override_cache["ENABLE_BRAIN_CONSULTANT_MODE"] = True
    db, alpha = mock_db
    mock_brain_with_redis.check_correlation_with_poll = AsyncMock(
        return_value={"status": "PENDING"},
    )
    result = await _run_submit(db, alpha.id, mock_brain_with_redis)
    assert result["submitted"] is False
    assert result["retryable"] is True
    mock_brain_with_redis.submit_alpha.assert_not_called()


@pytest.mark.asyncio
async def test_consultant_mode_missing_max_returns_retryable(mock_db, mock_brain_with_redis):
    """OK but data has no 'max' → fail-closed,retryable=True (don't fail-open)."""
    _flag_override_cache["ENABLE_BRAIN_CONSULTANT_MODE"] = True
    db, alpha = mock_db
    mock_brain_with_redis.check_correlation_with_poll = AsyncMock(
        return_value={"status": "OK", "data": {}},
    )
    result = await _run_submit(db, alpha.id, mock_brain_with_redis)
    assert result["submitted"] is False
    assert result["retryable"] is True
    mock_brain_with_redis.submit_alpha.assert_not_called()


# ---------------------------------------------------------------------------
# Auto-revert safety net: 403 → flip flag back to USER
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_consultant_mode_403_triggers_auto_revert(mock_db, mock_brain_with_redis):
    """AUTH_DENIED → _auto_revert_consultant_mode runs (independent session);
    submit returned with retryable=False (user must re-enable manually)."""
    _flag_override_cache["ENABLE_BRAIN_CONSULTANT_MODE"] = True
    db, alpha = mock_db
    mock_brain_with_redis.check_correlation_with_poll = AsyncMock(
        return_value={"status": "AUTH_DENIED"},
    )

    revert_calls = []

    async def _fake_session_ctx():
        """Mock async context manager mimicking AsyncSessionLocal()."""
        iso_db = AsyncMock()
        return iso_db

    class _FakeSessionLocal:
        def __init__(self):
            self.iso_db = None
        async def __aenter__(self):
            self.iso_db = AsyncMock()
            return self.iso_db
        async def __aexit__(self, *a):
            return None

    fake_flag_svc = AsyncMock()
    fake_flag_svc.clear_override = AsyncMock(side_effect=lambda *a, **kw: revert_calls.append((a, kw)))

    with (
        patch("backend.database.AsyncSessionLocal", new=_FakeSessionLocal),
        patch(
            "backend.services.feature_flag_service.FeatureFlagService",
            return_value=fake_flag_svc,
        ),
    ):
        result = await _run_submit(db, alpha.id, mock_brain_with_redis)

    assert result["submitted"] is False
    assert result.get("retryable") is False
    assert "回退" in result["reason"] or "USER" in result["reason"]
    # auto-revert was triggered via independent session
    assert len(revert_calls) == 1
    args, kwargs = revert_calls[0]
    assert args[0] == "ENABLE_BRAIN_CONSULTANT_MODE"
    assert kwargs["actor"] == "system_auto_revert"
    # submit_alpha NOT called
    mock_brain_with_redis.submit_alpha.assert_not_called()


@pytest.mark.asyncio
async def test_auto_revert_failure_does_not_break_submit_response(mock_db, mock_brain_with_redis):
    """If auto-revert itself fails (DB outage), submit still returns the
    AUTH_DENIED response — safety-net error is logged, not raised."""
    _flag_override_cache["ENABLE_BRAIN_CONSULTANT_MODE"] = True
    db, alpha = mock_db
    mock_brain_with_redis.check_correlation_with_poll = AsyncMock(
        return_value={"status": "AUTH_DENIED"},
    )

    class _FailingSessionLocal:
        async def __aenter__(self):
            raise ConnectionError("DB down")
        async def __aexit__(self, *a):
            return None

    with patch("backend.database.AsyncSessionLocal", new=_FailingSessionLocal):
        result = await _run_submit(db, alpha.id, mock_brain_with_redis)

    # Still got the proper rejection response
    assert result["submitted"] is False
    mock_brain_with_redis.submit_alpha.assert_not_called()
