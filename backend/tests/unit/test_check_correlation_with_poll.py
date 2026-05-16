"""Unit tests for BrainAdapter.check_correlation_with_poll status classification.

Plan §6.1. Validates four response paths:
  - 200 + max present → status="OK"
  - 200 + empty payload (still computing) → retries → status="PENDING"
  - 403 → status="AUTH_DENIED" (triggers auto-revert safety net)
  - Exception/network error → eventually status="PENDING" (retryable)
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from backend.adapters.brain_adapter import BrainAdapter


@pytest.fixture
def adapter():
    return BrainAdapter()


@pytest.mark.asyncio
async def test_status_ok_when_200_with_max(adapter):
    """200 + payload with 'max' → OK."""
    mock_response = type("R", (), {"status_code": 200, "json": lambda self=None: {"max": 0.5, "min": -0.2}})()
    with patch.object(adapter, "_request", new=AsyncMock(return_value=mock_response)):
        result = await adapter.check_correlation_with_poll("alpha-1", max_polls=1, poll_interval=0)
    assert result["status"] == "OK"
    assert result["data"]["max"] == 0.5


@pytest.mark.asyncio
async def test_status_auth_denied_on_403(adapter):
    """403 → AUTH_DENIED (triggers auto-revert in alpha_service)."""
    mock_response = type("R", (), {"status_code": 403, "json": lambda self=None: {}})()
    with patch.object(adapter, "_request", new=AsyncMock(return_value=mock_response)):
        result = await adapter.check_correlation_with_poll("alpha-1", max_polls=1, poll_interval=0)
    assert result["status"] == "AUTH_DENIED"


@pytest.mark.asyncio
async def test_status_pending_when_empty_payload_persists(adapter):
    """200 + payload without 'max' (BRAIN still computing) → retries exhaust → PENDING."""
    mock_response = type("R", (), {"status_code": 200, "json": lambda self=None: {}})()
    with patch.object(adapter, "_request", new=AsyncMock(return_value=mock_response)):
        result = await adapter.check_correlation_with_poll(
            "alpha-1", max_polls=2, poll_interval=0,
        )
    assert result["status"] == "PENDING"


@pytest.mark.asyncio
async def test_status_pending_on_network_exception(adapter):
    """Exception → check_correlation returns status_code=0 → retries exhaust → PENDING."""
    with patch.object(adapter, "_request", new=AsyncMock(side_effect=ConnectionError("boom"))):
        result = await adapter.check_correlation_with_poll(
            "alpha-1", max_polls=2, poll_interval=0,
        )
    assert result["status"] == "PENDING"


@pytest.mark.asyncio
async def test_check_correlation_wraps_status_code(adapter):
    """Bare check_correlation returns {status_code, data} shape (not bare dict)."""
    mock_response = type("R", (), {"status_code": 200, "json": lambda self=None: {"max": 0.3}})()
    with patch.object(adapter, "_request", new=AsyncMock(return_value=mock_response)):
        result = await adapter.check_correlation("alpha-1", "PROD")
    assert result == {"status_code": 200, "data": {"max": 0.3}}


@pytest.mark.asyncio
async def test_check_correlation_403_preserves_status_code(adapter):
    """403 → status_code=403 + empty data (caller distinguishes from 200+empty)."""
    mock_response = type("R", (), {"status_code": 403, "json": lambda self=None: {}})()
    with patch.object(adapter, "_request", new=AsyncMock(return_value=mock_response)):
        result = await adapter.check_correlation("alpha-1", "PROD")
    assert result == {"status_code": 403, "data": {}}
