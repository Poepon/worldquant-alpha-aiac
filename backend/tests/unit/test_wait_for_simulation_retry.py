"""_wait_for_simulation poll-loop retry tests (2026-05-21).

Regression guard for the cumulative-retry-budget bug: transient poll errors
(stale-keepalive TLS EOF etc.) spread across a long simulation must NOT
accumulate and falsely abort an otherwise-healthy sim. retry_count resets on
every successful poll → the budget measures CONSECUTIVE failures.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ConnectError


def _running():
    return SimpleNamespace(status_code=200, headers={"Retry-After": "1"}, json=lambda: {})


def _complete(alpha_id="A1"):
    return SimpleNamespace(
        status_code=200, headers={},
        json=lambda: {"status": "COMPLETE", "alpha": alpha_id},
    )


def _seq_request(seq):
    state = {"i": 0}

    async def _fake(method, url, **kw):
        item = seq[state["i"]]
        state["i"] += 1
        if isinstance(item, Exception):
            raise item
        return item

    return _fake


@pytest.mark.asyncio
async def test_isolated_blips_recover_via_retry_count_reset(monkeypatch):
    """4 transient TLS blips, each followed by a successful poll → the sim
    completes (each blip is 1 consecutive failure, reset by the next success).
    Pre-fix this would abort on the 4th cumulative blip."""
    from backend.adapters.brain_adapter import BrainAdapter

    b = BrainAdapter()
    seq = [
        ConnectError("TLS EOF"), _running(),
        ConnectError("TLS EOF"), _running(),
        ConnectError("TLS EOF"), _running(),
        ConnectError("TLS EOF"), _complete("WIN"),
    ]
    monkeypatch.setattr(b, "_request", _seq_request(seq))
    import asyncio
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    async def _fake_details(aid):
        return {"success": True, "alpha_id": aid}
    monkeypatch.setattr(b, "_get_completed_alpha_details", _fake_details)

    result = await b._wait_for_simulation("/simulations/X")
    assert result["success"] is True
    assert result["alpha_id"] == "WIN"


@pytest.mark.asyncio
async def test_consecutive_failures_abort(monkeypatch):
    """4 CONSECUTIVE blips (no success between) → abort with success=False."""
    from backend.adapters.brain_adapter import BrainAdapter

    b = BrainAdapter()
    seq = [ConnectError("TLS EOF")] * 4  # 4th trips retry_count > max_retries(3)
    monkeypatch.setattr(b, "_request", _seq_request(seq))
    import asyncio
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    result = await b._wait_for_simulation("/simulations/X")
    assert result["success"] is False
    assert "TLS EOF" in result["error"]
