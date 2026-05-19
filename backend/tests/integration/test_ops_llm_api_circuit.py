"""Phase 4 Sprint 0 PR0 — /ops/llm/api-circuit-{status,clear} integration
tests (2026-05-19, F-T2 post-S0-B review).

Mirrors test_ops_brain_auth_circuit.py for the LLM provider circuit. The
two circuits are independent — LLM can be OPEN while BRAIN is CLOSED and
vice versa.
"""
from __future__ import annotations

import os
import time
from unittest.mock import MagicMock, patch

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


@pytest.fixture
def _fake_redis():
    class _R:
        def __init__(self):
            self.store: dict = {}
            self.ttls: dict = {}
            self.set_at: dict = {}

        def get(self, key):
            v = self.store.get(key)
            return v.encode("utf-8") if isinstance(v, str) else v

        def set(self, key, value, ex=None):
            self.store[key] = value
            if ex is not None:
                self.ttls[key] = int(ex)
                self.set_at[key] = time.time()
            return True

        def delete(self, key):
            self.store.pop(key, None)
            self.ttls.pop(key, None)
            self.set_at.pop(key, None)
            return 1

    return _R()


@pytest_asyncio.fixture
async def client_factory(_fake_redis):
    async def _build():
        app = FastAPI()
        app.include_router(ops_router, prefix="/api/v1")
        app.dependency_overrides[get_db] = lambda: None
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test")
    yield _build


@pytest_asyncio.fixture(autouse=True)
async def _reset_llm_circuit(_fake_redis):
    """Ensure each test starts with a fresh CLOSED circuit."""
    from backend.agents.services.llm_service import LLM_API_CIRCUIT
    with patch(
        "backend.tasks.redis_pool.get_redis_client",
        return_value=_fake_redis,
    ):
        LLM_API_CIRCUIT.clear(reason="test_setup")
    yield


@pytest.mark.asyncio
async def test_llm_circuit_status_closed_by_default(client_factory, _fake_redis):
    """Fresh circuit → state=closed, trip_count=0."""
    with patch(
        "backend.tasks.redis_pool.get_redis_client",
        return_value=_fake_redis,
    ):
        client = await client_factory()
        async with client as ac:
            r = await ac.get("/api/v1/ops/llm/api-circuit-status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "closed"
    assert body["trip_count"] == 0
    assert body["until_ts"] is None
    assert body["last_failure_reason"] is None
    assert body["seconds_until_half_open"] == 0


@pytest.mark.asyncio
async def test_llm_circuit_status_open_after_trip(client_factory, _fake_redis):
    """Trip the live LLM_API_CIRCUIT → status shows OPEN + reason."""
    from backend.agents.services.llm_service import LLM_API_CIRCUIT
    with patch(
        "backend.tasks.redis_pool.get_redis_client",
        return_value=_fake_redis,
    ):
        LLM_API_CIRCUIT.trip(reason="deepseek_5xx_storm", ttl_sec=60)
        client = await client_factory()
        async with client as ac:
            r = await ac.get("/api/v1/ops/llm/api-circuit-status")
    body = r.json()
    assert body["state"] == "open"
    assert body["last_failure_reason"] == "deepseek_5xx_storm"
    assert body["trip_count"] == 1
    assert body["until_ts"] is not None
    assert 50 <= body["seconds_until_half_open"] <= 61


@pytest.mark.asyncio
async def test_llm_circuit_clear_via_endpoint(client_factory, _fake_redis):
    """POST /clear → trip cleared, state back to CLOSED."""
    from backend.agents.services.llm_service import LLM_API_CIRCUIT
    with patch(
        "backend.tasks.redis_pool.get_redis_client",
        return_value=_fake_redis,
    ):
        LLM_API_CIRCUIT.trip(reason="x", ttl_sec=60)
        assert LLM_API_CIRCUIT.is_open() is True

        client = await client_factory()
        async with client as ac:
            r = await ac.post(
                "/api/v1/ops/llm/api-circuit-clear",
                headers={"X-Ops-Actor": "ops_console_test"},
            )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cleared"] is True
    assert body["actor"] == "ops_console_test"

    # Confirm status now CLOSED
    with patch(
        "backend.tasks.redis_pool.get_redis_client",
        return_value=_fake_redis,
    ):
        client2 = await client_factory()
        async with client2 as ac:
            r2 = await ac.get("/api/v1/ops/llm/api-circuit-status")
    assert r2.json()["state"] == "closed"


@pytest.mark.asyncio
async def test_llm_circuit_endpoints_require_ops_token(client_factory, _fake_redis):
    """OPS_API_TOKEN set → 401 without X-Ops-Token header."""
    os.environ["OPS_API_TOKEN"] = "secret_llm_circuit"
    try:
        with patch(
            "backend.tasks.redis_pool.get_redis_client",
            return_value=_fake_redis,
        ):
            client = await client_factory()
            async with client as ac:
                r1 = await ac.get("/api/v1/ops/llm/api-circuit-status")
                assert r1.status_code == 401
                r2 = await ac.post("/api/v1/ops/llm/api-circuit-clear")
                assert r2.status_code == 401
    finally:
        os.environ.pop("OPS_API_TOKEN", None)


@pytest.mark.asyncio
async def test_llm_and_brain_circuits_independent(client_factory, _fake_redis):
    """Tripping BRAIN auth circuit MUST NOT affect LLM circuit (and v.v.) —
    the two are independent providers."""
    from backend.adapters.brain_adapter import BRAIN_AUTH_CIRCUIT
    from backend.agents.services.llm_service import LLM_API_CIRCUIT

    with patch(
        "backend.tasks.redis_pool.get_redis_client",
        return_value=_fake_redis,
    ):
        # Trip BRAIN only
        BRAIN_AUTH_CIRCUIT.trip(reason="brain_401", ttl_sec=60)
        assert BRAIN_AUTH_CIRCUIT.is_open() is True
        assert LLM_API_CIRCUIT.is_open() is False  # independent

        client = await client_factory()
        async with client as ac:
            r_llm = await ac.get("/api/v1/ops/llm/api-circuit-status")
            r_brain = await ac.get("/api/v1/ops/brain/auth-circuit-status")
        assert r_llm.json()["state"] == "closed"
        assert r_brain.json()["state"] == "open"

        # Cleanup
        BRAIN_AUTH_CIRCUIT.clear(reason="test_cleanup")
