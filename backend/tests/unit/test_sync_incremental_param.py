"""sync incremental date-filter param tests (2026-05-20).

Regression guard: get_user_alphas must send the working BRAIN range param
'dateCreated>' (not the silently-ignored 'startDate'), so the MAX(date_created)
incremental anchor actually limits the fetch instead of re-pulling all ~9700
alphas every run.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_get_user_alphas_uses_dateCreated_gt_param():
    from backend.adapters.brain_adapter import BrainAdapter

    captured = {}

    async def _fake_call(method, endpoint, params=None, **kw):
        captured["params"] = params
        return SimpleNamespace(status_code=200, json=lambda: {"results": [], "count": 0})

    b = BrainAdapter()
    with patch.object(b, "_safe_api_call", new=AsyncMock(side_effect=_fake_call)):
        await b.get_user_alphas(
            limit=100, offset=0, stage="IS",
            start_date="2026-05-11T11:18:00+08:00",
        )

    p = captured["params"]
    # The working server-side filter
    assert p.get("dateCreated>") == "2026-05-11T11:18:00+08:00"
    # The broken param must NOT be sent
    assert "startDate" not in p


@pytest.mark.asyncio
async def test_get_user_alphas_no_filter_when_start_date_none():
    from backend.adapters.brain_adapter import BrainAdapter

    captured = {}

    async def _fake_call(method, endpoint, params=None, **kw):
        captured["params"] = params
        return SimpleNamespace(status_code=200, json=lambda: {"results": [], "count": 0})

    b = BrainAdapter()
    with patch.object(b, "_safe_api_call", new=AsyncMock(side_effect=_fake_call)):
        await b.get_user_alphas(limit=100, offset=0, stage="IS")

    assert "dateCreated>" not in captured["params"]
    assert "startDate" not in captured["params"]
