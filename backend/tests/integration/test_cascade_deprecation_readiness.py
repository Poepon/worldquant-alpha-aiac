"""Integration: GET /ops/cascade-deprecation/readiness (flat-F4 prep, 2026-05-18).

Verifies the readiness verdict + next_action recommendation under each
adoption scenario:
  - mixed cascade RUNNING + flat RUNNING + flag OFF → ready_to_delete=False,
    advise to drain cascade
  - cascade PAUSED only → ready_to_delete=False, advise to finalize
  - 0 cascade + flat RUNNING + default flag ON → ready_to_delete=True
  - 0 cascade + default flag OFF → ready_to_delete=False, advise flag flip
  - 0 cascade + default flag ON + 0 flat RUNNING → ready_to_delete=False,
    advise to start a flat session first
  - auth 401 without X-Ops-Token when OPS_API_TOKEN env set

Mocks the DB layer (AsyncSession.execute) so the route stays a pure
unit-of-routing test.
"""
from __future__ import annotations

import os
from typing import AsyncGenerator, List, Tuple
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.database import get_db
from backend.routers.ops import router as ops_router


@pytest.fixture(autouse=True)
def _isolate_ops_token():
    prev = os.environ.pop("OPS_API_TOKEN", None)
    yield
    if prev is not None:
        os.environ["OPS_API_TOKEN"] = prev
    else:
        os.environ.pop("OPS_API_TOKEN", None)


def _mock_db_with_rows(rows: List[Tuple[str, str, str, int]]):
    """Build an AsyncSession-like mock returning ``rows`` from execute().all().

    Each tuple is (mining_mode, status, region, n) — matches the SELECT in
    the endpoint.
    """
    result = MagicMock()
    result.all = MagicMock(return_value=rows)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    return db


@pytest_asyncio.fixture
async def client_factory():
    """Return a builder that wires the ops router with a db override."""

    async def _build(rows: List[Tuple[str, str, str, int]], settings_overrides=None):
        app = FastAPI()
        app.include_router(ops_router, prefix="/api/v1")
        app.dependency_overrides[get_db] = lambda: _mock_db_with_rows(rows)
        if settings_overrides:
            from backend.config import settings as _stg
            for k, v in settings_overrides.items():
                setattr(_stg, k, v)
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test")

    return _build


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_readiness_requires_ops_token_when_set(client_factory):
    os.environ["OPS_API_TOKEN"] = "secret-token"
    client = await client_factory([])
    async with client as ac:
        r = await ac.get("/api/v1/ops/cascade-deprecation/readiness")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_readiness_cascade_running_advises_drain(client_factory):
    rows = [
        ("CONTINUOUS_CASCADE", "RUNNING", "USA", 1),
        ("CONTINUOUS_CASCADE", "RUNNING", "CHN", 2),
        ("FLAT_CONTINUOUS", "RUNNING", "USA", 1),
    ]
    client = await client_factory(rows, settings_overrides={
        "ENABLE_DEFAULT_FLAT_SESSION": False,
        "ENABLE_FLAT_CONTINUOUS": True,
    })
    async with client as ac:
        r = await ac.get("/api/v1/ops/cascade-deprecation/readiness")
    assert r.status_code == 200
    body = r.json()
    assert body["cascade_running_count"] == 3
    assert sorted(body["cascade_running_regions"]) == ["CHN", "USA"]
    assert body["flat_running_count"] == 1
    assert body["ready_to_delete"] is False
    assert "Drain" in body["next_action"]


@pytest.mark.asyncio
async def test_readiness_cascade_only_paused_advises_drain_endpoint(client_factory):
    """phase15-D PR2 (2026-05-18): wording changed from "finalize" to point
    at the new POST /ops/cascade-deprecation/drain endpoint."""
    rows = [("CONTINUOUS_CASCADE", "PAUSED", "USA", 2)]
    client = await client_factory(rows, settings_overrides={
        "ENABLE_DEFAULT_FLAT_SESSION": False,
        "ENABLE_FLAT_CONTINUOUS": False,
    })
    async with client as ac:
        r = await ac.get("/api/v1/ops/cascade-deprecation/readiness")
    body = r.json()
    assert body["cascade_running_count"] == 0
    assert body["cascade_paused_count"] == 2
    assert body["ready_to_delete"] is False
    assert "drain" in body["next_action"].lower()


@pytest.mark.asyncio
async def test_readiness_drained_default_flat_kill_switch_retired_ready(client_factory):
    """phase15-D PR3c (2026-05-18): ENABLE_CASCADE_LEGACY retired —
    cascade always-refused. Drained + flat active → ready_to_delete=True
    + next_action describes PR3d/PR4b remaining cleanup (no operator
    action needed since kill-switch is gone)."""
    rows = [("FLAT_CONTINUOUS", "RUNNING", "USA", 3)]
    client = await client_factory(rows, settings_overrides={
        "ENABLE_DEFAULT_FLAT_SESSION": True,
        "ENABLE_FLAT_CONTINUOUS": True,
    })
    async with client as ac:
        r = await ac.get("/api/v1/ops/cascade-deprecation/readiness")
    body = r.json()
    assert body["cascade_running_count"] == 0
    assert body["cascade_paused_count"] == 0
    assert body["flat_running_count"] == 3
    assert body["ready_to_delete"] is True
    assert body["cascade_legacy_flag_on"] is False


@pytest.mark.asyncio
async def test_readiness_all_green_post_pr3c(client_factory):
    """phase15-D PR3c (2026-05-18): kill-switch retired → only gates
    are drained + flat active + default_flat ON."""
    rows = [("FLAT_CONTINUOUS", "RUNNING", "USA", 3)]
    client = await client_factory(rows, settings_overrides={
        "ENABLE_DEFAULT_FLAT_SESSION": True,
        "ENABLE_FLAT_CONTINUOUS": True,
    })
    async with client as ac:
        r = await ac.get("/api/v1/ops/cascade-deprecation/readiness")
    body = r.json()
    assert body["ready_to_delete"] is True
    assert "PR3d" in body["next_action"] or "PR4b" in body["next_action"]


@pytest.mark.asyncio
async def test_readiness_drained_but_default_flag_off_advises_flip(client_factory):
    rows: List[Tuple[str, str, str, int]] = []
    client = await client_factory(rows, settings_overrides={
        "ENABLE_DEFAULT_FLAT_SESSION": False,
        "ENABLE_FLAT_CONTINUOUS": True,
    })
    async with client as ac:
        r = await ac.get("/api/v1/ops/cascade-deprecation/readiness")
    body = r.json()
    assert body["cascade_running_count"] == 0
    assert body["ready_to_delete"] is False
    assert "ENABLE_DEFAULT_FLAT_SESSION" in body["next_action"]


@pytest.mark.asyncio
async def test_readiness_no_flat_running_advises_start(client_factory):
    rows: List[Tuple[str, str, str, int]] = []
    client = await client_factory(rows, settings_overrides={
        "ENABLE_DEFAULT_FLAT_SESSION": True,
        "ENABLE_FLAT_CONTINUOUS": True,
    })
    async with client as ac:
        r = await ac.get("/api/v1/ops/cascade-deprecation/readiness")
    body = r.json()
    assert body["flat_running_count"] == 0
    assert body["ready_to_delete"] is False
    assert "start-flat-session" in body["next_action"]
