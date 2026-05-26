"""Zombie-simulation reclaim unit tests (2026-05-24).

brain_adapter._wait_for_simulation / _wait_for_multisim used to declare a
`max_wait` parameter that the poll loop NEVER compared against elapsed time.
A "zombie" sim — one that keeps returning HTTP 200 + a valid Retry-After
header indefinitely (rooted in a stale/thrashing BRAIN session; observed as
task 3329 RUNNING ~11h with 0 alphas) — therefore polled forever.

These tests verify the Zombie Protocol now enforced in the poll loop:
  STEP 1: on exceeding max_wait, re-auth once (_coalesced_reauth)
  STEP 2: recheck after a short grace
  STEP 3/4: if STILL in_progress, classify as zombie and abandon with
            {success: False, retryable: True, error_kind: 'sim_zombie_timeout'}
            so node_simulate (V-27.61) holds the alpha at PENDING and the next
            round re-tries — rather than polling a dead handle forever.

A genuinely-finishing sim that completes during the grace recheck must NOT be
mis-classified as a zombie (no false reclaim of a slow-but-live sim).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_response(status_code: int, headers: dict, body: dict):
    r = MagicMock()
    r.status_code = status_code
    r.headers = headers
    r.json = MagicMock(return_value=body)
    return r


def _bare_adapter():
    """BrainAdapter with __init__ bypassed so we isolate the poll loop."""
    from backend.adapters.brain_adapter import BrainAdapter

    ba = BrainAdapter.__new__(BrainAdapter)
    # Short grace so the recheck round resolves fast in tests.
    ba._ZOMBIE_RECHECK_SLEEP = 0.01
    return ba


# Always "in progress": 200 + a small Retry-After, never an alpha id.
_IN_PROGRESS = _make_response(200, {"Retry-After": "0.02"}, {"status": "RUNNING"})


@pytest.mark.asyncio
async def test_wait_for_simulation_zombie_returns_retryable():
    """A sim stuck at 200+Retry-After past max_wait → re-auth once, recheck,
    still stuck → retryable zombie signal (not an indefinite poll)."""
    ba = _bare_adapter()
    ba._request = AsyncMock(return_value=_IN_PROGRESS)
    ba._coalesced_reauth = AsyncMock(return_value=True)

    result = await ba._wait_for_simulation("http://brain/sim/1", max_wait=0.2)

    assert result["success"] is False
    assert result["retryable"] is True
    assert result["error_kind"] == "sim_zombie_timeout"
    assert result["retry_after_sec"] == 30
    assert "zombie timeout" in result["error"]
    # Zombie Protocol STEP 1 fires exactly once (not on every poll iteration).
    ba._coalesced_reauth.assert_awaited_once()


@pytest.mark.asyncio
async def test_wait_for_simulation_recovers_after_reauth_no_false_zombie():
    """If the sim FINISHES during the post-reauth grace recheck, it must be
    returned normally — never mis-classified as a zombie."""
    ba = _bare_adapter()
    state = {"reauthed": False}

    async def _reauth():
        state["reauthed"] = True
        return True

    completed = _make_response(200, {}, {"status": "COMPLETE", "alpha": "AX1"})

    def _request_side_effect(method, url, **kwargs):
        # Stuck until the grace re-auth happens, then completes on recheck.
        return completed if state["reauthed"] else _IN_PROGRESS

    ba._coalesced_reauth = AsyncMock(side_effect=_reauth)
    ba._request = AsyncMock(side_effect=_request_side_effect)
    ba._get_completed_alpha_details = AsyncMock(
        return_value={"success": True, "alpha_id": "AX1"}
    )

    result = await ba._wait_for_simulation("http://brain/sim/2", max_wait=0.2)

    assert result == {"success": True, "alpha_id": "AX1"}
    ba._coalesced_reauth.assert_awaited_once()
    ba._get_completed_alpha_details.assert_awaited_once_with("AX1")


@pytest.mark.asyncio
async def test_wait_for_multisim_zombie_returns_retryable():
    """Multi-sim poll has the same dead-max_wait bug → same zombie reclaim."""
    ba = _bare_adapter()
    ba._request = AsyncMock(return_value=_IN_PROGRESS)
    ba._coalesced_reauth = AsyncMock(return_value=True)

    result = await ba._wait_for_multisim("http://brain/multisim/1", max_wait=0.2)

    assert result["success"] is False
    assert result["retryable"] is True
    assert result["error_kind"] == "sim_zombie_timeout"
    assert "multi-simulation zombie timeout" in result["error"]
    ba._coalesced_reauth.assert_awaited_once()


@pytest.mark.asyncio
async def test_simulate_batch_propagates_parent_retryable_to_children():
    """A parent-level retryable failure (e.g. multi-sim zombie) must propagate
    to EVERY child result so node_simulate holds each alpha at PENDING instead
    of writing them all to alpha_failures."""
    ba = _bare_adapter()
    ba._wait_for_multisim = AsyncMock(
        return_value={
            "success": False,
            "error": "BRAIN multi-simulation zombie timeout after 3600s",
            "retryable": True,
            "retry_after_sec": 30,
            "error_kind": "sim_zombie_timeout",
        }
    )
    # POST returns 201 with a Location so simulate_batch reaches _wait_for_multisim.
    post_resp = MagicMock()
    post_resp.status_code = 201
    post_resp.headers = {"Location": "/simulations/parent-1"}
    post_resp.text = ""
    ba._request = AsyncMock(return_value=post_resp)
    # Force the multi-sim probe path (skip Redis latch / consultant gate).
    ba._get_slot_redis = AsyncMock(side_effect=Exception("no redis in test"))

    from backend.config import settings

    _orig = settings.ENABLE_BRAIN_CONSULTANT_MODE
    object.__setattr__(settings, "ENABLE_BRAIN_CONSULTANT_MODE", True)
    try:
        results = await ba.simulate_batch(["expr_a", "expr_b", "expr_c"])
    finally:
        object.__setattr__(settings, "ENABLE_BRAIN_CONSULTANT_MODE", _orig)

    assert len(results) == 3
    for r in results:
        assert r["success"] is False
        assert r["retryable"] is True
        assert r["error_kind"] == "sim_zombie_timeout"
        assert r["retry_after_sec"] == 30
