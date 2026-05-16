"""Integration: /ops/brain/* router endpoints (P3-Brain plan §10).

Verifies:
  - GET /ops/brain/role-state: 401 without X-Ops-Token; OK shape with token
  - POST activate-consultant: flips flag, kicks off sync_datasets.delay,
    response includes mode+sync_enqueued+actor
  - POST deactivate-consultant: clears flag, no Redis/sync side effects
  - get_state response: mode + effective_* + running_tasks_count +
    last_switched_at (UTC marker)

Uses dependency_overrides to inject a mock BrainRoleSwitchService — we
skip the JSONB-heavy FeatureFlagOverride DB layer at this level (covered
by test_brain_role_switch_service.py at the unit level).
"""
from __future__ import annotations

import os
from typing import AsyncGenerator
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.routers.ops import (
    get_brain_role_switch_service,
    router as ops_router,
)


@pytest.fixture(autouse=True)
def _isolate_ops_token():
    """Each test starts with OPS_API_TOKEN unset; test sets/unsets explicitly."""
    prev = os.environ.pop("OPS_API_TOKEN", None)
    yield
    if prev is not None:
        os.environ["OPS_API_TOKEN"] = prev
    else:
        os.environ.pop("OPS_API_TOKEN", None)


@pytest.fixture
def mock_switch_service():
    """AsyncMock standing in for BrainRoleSwitchService."""
    svc = AsyncMock()
    svc.get_state = AsyncMock(return_value={
        "mode": "USER",
        "effective_default_test_period": "P2Y0M",
        "effective_sharpe_submit_min": 1.5,
        "effective_region_universes": {"USA": "TOP3000"},
        "running_tasks_count": 0,
        "last_switched_at": None,
        "last_switched_by": None,
    })
    svc.activate_consultant_mode = AsyncMock(return_value={
        "mode": "CONSULTANT",
        "sync_enqueued": True,
        "note": "test",
        "actor": "test_user",
    })
    svc.deactivate_consultant_mode = AsyncMock(return_value={
        "mode": "USER",
        "note": "test revert",
        "actor": "test_user",
    })
    return svc


@pytest_asyncio.fixture
async def client(mock_switch_service) -> AsyncGenerator[AsyncClient, None]:
    """FastAPI test client with the BrainRoleSwitchService dependency
    overridden so we don't need DB/redis/celery wiring."""
    app = FastAPI()
    app.include_router(ops_router, prefix="/api/v1")
    app.dependency_overrides[get_brain_role_switch_service] = lambda: mock_switch_service
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# GET /ops/brain/role-state
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_role_state_returns_user_mode_shape(client, mock_switch_service):
    r = await client.get("/api/v1/ops/brain/role-state")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "USER"
    assert body["effective_default_test_period"] == "P2Y0M"
    assert body["effective_sharpe_submit_min"] == 1.5
    assert body["effective_region_universes"] == {"USA": "TOP3000"}
    assert body["running_tasks_count"] == 0
    assert body["last_switched_at"] is None
    mock_switch_service.get_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_role_state_with_utc_timestamp(client, mock_switch_service):
    mock_switch_service.get_state = AsyncMock(return_value={
        "mode": "CONSULTANT",
        "effective_default_test_period": "P0Y",
        "effective_sharpe_submit_min": 1.58,
        "effective_region_universes": {"USA": "TOP3000", "CHN": "TOP2000A"},
        "running_tasks_count": 3,
        "last_switched_at": "2026-05-16T03:14:15Z",
        "last_switched_by": "ops_console",
    })
    r = await client.get("/api/v1/ops/brain/role-state")
    body = r.json()
    assert body["mode"] == "CONSULTANT"
    assert body["last_switched_at"].endswith("Z")
    assert body["running_tasks_count"] == 3


# ---------------------------------------------------------------------------
# Auth — X-Ops-Token
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_role_state_401_without_token(client):
    """When OPS_API_TOKEN env is set, requests without X-Ops-Token must 401."""
    os.environ["OPS_API_TOKEN"] = "secret-token-for-test"
    r = await client.get("/api/v1/ops/brain/role-state")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_get_role_state_401_with_wrong_token(client):
    os.environ["OPS_API_TOKEN"] = "secret-token-for-test"
    r = await client.get(
        "/api/v1/ops/brain/role-state",
        headers={"X-Ops-Token": "wrong"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_get_role_state_200_with_correct_token(client):
    os.environ["OPS_API_TOKEN"] = "secret-token-for-test"
    r = await client.get(
        "/api/v1/ops/brain/role-state",
        headers={"X-Ops-Token": "secret-token-for-test"},
    )
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# POST /ops/brain/activate-consultant
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_activate_consultant_uses_actor_header(client, mock_switch_service):
    r = await client.post(
        "/api/v1/ops/brain/activate-consultant",
        headers={"X-Ops-Actor": "alice"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "CONSULTANT"
    assert body["sync_enqueued"] is True
    mock_switch_service.activate_consultant_mode.assert_awaited_once_with(actor="alice")


@pytest.mark.asyncio
async def test_activate_consultant_falls_back_to_default_actor(client, mock_switch_service):
    r = await client.post("/api/v1/ops/brain/activate-consultant")
    assert r.status_code == 200
    mock_switch_service.activate_consultant_mode.assert_awaited_once_with(actor="ops_console")


# ---------------------------------------------------------------------------
# POST /ops/brain/deactivate-consultant
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_deactivate_consultant_returns_user_mode(client, mock_switch_service):
    r = await client.post(
        "/api/v1/ops/brain/deactivate-consultant",
        headers={"X-Ops-Actor": "bob"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "USER"
    mock_switch_service.deactivate_consultant_mode.assert_awaited_once_with(actor="bob")
