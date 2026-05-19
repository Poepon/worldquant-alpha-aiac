"""2026-05-19 silent-burn fix unit tests.

Verifies BrainAdapter.simulate_alpha's post-reauth-retry failure path:
when _request's coalesced reauth + 2x retry can't recover (BRAIN account
locked / cred actually invalid / sustained 401), the response shows the
"Incorrect authentication credentials" body marker → simulate_alpha now
returns retryable=True instead of ordinary failure, so node_simulate
(V-27.61) holds the alpha at PENDING (not alpha_failures) and the
round can soft-abort instead of burning the rest of the LLM pipeline.

Pre-fix: 24h had 121× alpha_failures rows with "Incorrect authentication
credentials" while ROUND_SUMMARY / EVALUATE / HYPOTHESIS_FEEDBACK trace
steps reported SUCCESS — workflow burned LLM cost on alphas it couldn't
simulate.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_response(status_code: int, body: str):
    r = MagicMock()
    r.status_code = status_code
    r.text = body
    r.content = body.encode("utf-8")
    r.headers = {}
    return r


def _make_adapter_with_response(response):
    """Build a BrainAdapter mock where _request returns the given response.

    Bypasses _acquire_sim_slot / _release_sim_slot / authenticate() etc.
    so we can isolate the simulate_alpha branch.
    """
    from backend.adapters.brain_adapter import BrainAdapter

    ba = BrainAdapter.__new__(BrainAdapter)
    ba._request = AsyncMock(return_value=response)
    # _is_auth_error is method on the real class — use it unbound
    ba._is_auth_error = BrainAdapter._is_auth_error.__get__(ba, BrainAdapter)
    ba._AUTH_ERROR_BODY_MARKER = "Incorrect authentication credentials"
    return ba


@pytest.mark.asyncio
async def test_simulate_401_with_body_marker_returns_retryable():
    """The exact production failure pattern: status=401 + auth-error body."""
    response = _make_response(
        401,
        '{"detail":"Incorrect authentication credentials."}',
    )
    ba = _make_adapter_with_response(response)
    with patch(
        "backend.adapters.brain_adapter.BrainAdapter._acquire_sim_slot",
        new=AsyncMock(return_value=True),
    ), patch(
        "backend.adapters.brain_adapter.BrainAdapter._release_sim_slot",
        new=AsyncMock(return_value=None),
    ):
        result = await ba.simulate_alpha("ts_rank(returns, 20)")

    assert result["success"] is False
    assert result["retryable"] is True
    assert result["retry_after_sec"] == 300
    assert result["error_kind"] == "brain_auth_failure"
    assert "BRAIN auth failure" in result["error"]


@pytest.mark.asyncio
async def test_simulate_non_401_with_body_marker_also_retryable():
    """V-22.7 case: BRAIN returns non-401 status with the auth-error body
    (observed on task 530 spike). _is_auth_error should catch it too."""
    response = _make_response(
        500,
        '{"detail":"Incorrect authentication credentials."}',
    )
    ba = _make_adapter_with_response(response)
    with patch(
        "backend.adapters.brain_adapter.BrainAdapter._acquire_sim_slot",
        new=AsyncMock(return_value=True),
    ), patch(
        "backend.adapters.brain_adapter.BrainAdapter._release_sim_slot",
        new=AsyncMock(return_value=None),
    ):
        result = await ba.simulate_alpha("ts_rank(returns, 20)")

    assert result["retryable"] is True
    assert result["error_kind"] == "brain_auth_failure"


@pytest.mark.asyncio
async def test_simulate_non_auth_failure_stays_ordinary():
    """Non-auth 4xx (e.g. 400 malformed payload) MUST stay as ordinary
    failure (no retryable key) so node_simulate writes the alpha to
    alpha_failures normally."""
    response = _make_response(
        400,
        '{"detail":"Invalid expression syntax"}',
    )
    ba = _make_adapter_with_response(response)
    with patch(
        "backend.adapters.brain_adapter.BrainAdapter._acquire_sim_slot",
        new=AsyncMock(return_value=True),
    ), patch(
        "backend.adapters.brain_adapter.BrainAdapter._release_sim_slot",
        new=AsyncMock(return_value=None),
    ):
        result = await ba.simulate_alpha("malformed")

    assert result["success"] is False
    assert "Creation failed" in result["error"]
    # Ordinary failure path — caller should write to alpha_failures
    assert "retryable" not in result or result.get("retryable") is False
    assert result.get("error_kind") is None


@pytest.mark.asyncio
async def test_simulate_403_stays_ordinary():
    """403 (e.g. CONSULTANT-only endpoint) is NOT an auth-token issue —
    it's a permission issue. Don't conflate it with retryable auth."""
    response = _make_response(
        403,
        '{"detail":"You do not have permission to perform this action."}',
    )
    ba = _make_adapter_with_response(response)
    with patch(
        "backend.adapters.brain_adapter.BrainAdapter._acquire_sim_slot",
        new=AsyncMock(return_value=True),
    ), patch(
        "backend.adapters.brain_adapter.BrainAdapter._release_sim_slot",
        new=AsyncMock(return_value=None),
    ):
        result = await ba.simulate_alpha("expr")

    assert result["success"] is False
    assert "Creation failed" in result["error"]
    assert "retryable" not in result or result.get("retryable") is False
