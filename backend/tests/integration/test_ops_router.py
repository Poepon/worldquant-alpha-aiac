"""Integration tests for /api/v1/ops/* router (Phase 1 endpoints).

来源: docs/alphagbm_skills_research_2026-05-15.md ops dashboard plan §1.2.

Mounts ``backend.routers.ops.router`` on an isolated FastAPI app so we
don't pull in the project-wide lifespan + JSONB-bearing tables. The DB
``Depends(get_db)`` is overridden to yield a session bound to the same
JSONB-free aiosqlite engine used in test_feature_flag_service.py.

Coverage:
* GET /flags returns every supported flag (env defaults)
* PATCH /flags/{name} flips a flag, GET reflects override
* DELETE /flags/{name}/override clears it
* GET /flags/audit shows the trail
* POST /tasks/trigger calls celery_app.send_task with the right name
* Throttle errors map to 409 / 429
* Unknown task → 400; missing token → 401 (when OPS_API_TOKEN is set)
"""
from __future__ import annotations

import os
from typing import AsyncGenerator
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import get_db
from backend.models.config import FeatureFlagAudit, FeatureFlagOverride
from backend.routers.ops import router as ops_router
from backend.services.feature_flag_service import _flag_override_cache


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def ff_engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    isolated = MetaData()
    FeatureFlagOverride.__table__.to_metadata(isolated)
    FeatureFlagAudit.__table__.to_metadata(isolated)
    async with engine.begin() as conn:
        await conn.run_sync(isolated.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session_maker(ff_engine):
    return sessionmaker(ff_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _clear_cache_and_env():
    _flag_override_cache.clear()
    # Ensure auth is disabled by default for these tests
    prev = os.environ.pop("OPS_API_TOKEN", None)
    yield
    _flag_override_cache.clear()
    if prev is not None:
        os.environ["OPS_API_TOKEN"] = prev


@pytest_asyncio.fixture
async def client(session_maker) -> AsyncGenerator[AsyncClient, None]:
    """ASGI client with /api/v1/ops/* mounted + get_db overridden."""
    app = FastAPI()
    app.include_router(ops_router, prefix="/api/v1")

    async def _override_get_db():
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# /ops/flags
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_flags_returns_all_supported(client):
    r = await client.get("/api/v1/ops/flags")
    assert r.status_code == 200
    body = r.json()
    names = [f["name"] for f in body]
    # Every whitelisted flag is present
    assert "ENABLE_PILLAR_AWARE_SELECTION" in names
    assert "ENABLE_REGIME_INFERENCE" in names
    # All start as env defaults (no overrides yet)
    for f in body:
        assert f["source"] != "runtime-override"
        assert f["override_value"] is None


@pytest.mark.asyncio
async def test_patch_flag_round_trip(client):
    r = await client.patch(
        "/api/v1/ops/flags/ENABLE_PILLAR_AWARE_SELECTION",
        json={"value": True, "note": "A/B for region USA"},
    )
    assert r.status_code == 200
    state = r.json()
    assert state["effective_value"] is True
    assert state["source"] == "runtime-override"
    assert state["note"] == "A/B for region USA"

    # GET reflects the override
    r2 = await client.get("/api/v1/ops/flags")
    pillar = next(f for f in r2.json() if f["name"] == "ENABLE_PILLAR_AWARE_SELECTION")
    assert pillar["effective_value"] is True
    assert pillar["override_value"] is True


@pytest.mark.asyncio
async def test_patch_unknown_flag_returns_404(client):
    r = await client.patch(
        "/api/v1/ops/flags/ENABLE_DOES_NOT_EXIST",
        json={"value": True},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_patch_wrong_type_returns_400(client):
    r = await client.patch(
        "/api/v1/ops/flags/ENABLE_PILLAR_AWARE_SELECTION",
        json={"value": "not-a-bool"},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_delete_flag_override_resets(client):
    await client.patch(
        "/api/v1/ops/flags/ENABLE_PILLAR_AWARE_SELECTION",
        json={"value": True},
    )
    r = await client.delete("/api/v1/ops/flags/ENABLE_PILLAR_AWARE_SELECTION/override")
    assert r.status_code == 200
    state = r.json()
    assert state["override_value"] is None
    assert state["source"] in ("env", "default")


@pytest.mark.asyncio
async def test_audit_trail(client):
    await client.patch("/api/v1/ops/flags/ENABLE_PILLAR_AWARE_SELECTION",
                       json={"value": True, "note": "first"})
    await client.patch("/api/v1/ops/flags/ENABLE_PILLAR_AWARE_SELECTION",
                       json={"value": False, "note": "rollback"})
    await client.delete("/api/v1/ops/flags/ENABLE_PILLAR_AWARE_SELECTION/override")

    r = await client.get("/api/v1/ops/flags/audit?limit=10")
    assert r.status_code == 200
    audits = r.json()
    assert len(audits) == 3
    # Newest first
    assert audits[0]["action"] == "clear"
    assert audits[1]["action"] == "set"
    assert audits[2]["action"] == "set"


@pytest.mark.asyncio
async def test_refresh_all_loads_overrides(client):
    """After PATCH the cache is already updated by write-through. The
    refresh-all endpoint exists so an operator can FORCE a reload from DB,
    which should be idempotent — it returns the count of currently-loaded
    flags."""
    await client.patch("/api/v1/ops/flags/ENABLE_PILLAR_AWARE_SELECTION",
                       json={"value": True})
    r = await client.post("/api/v1/ops/flags/refresh-all")
    assert r.status_code == 200
    body = r.json()
    assert body["refreshed"] == 1
    assert body["flags"] == ["ENABLE_PILLAR_AWARE_SELECTION"]


# ---------------------------------------------------------------------------
# /ops/tasks/trigger
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_trigger_unknown_task_400(client):
    r = await client.post(
        "/api/v1/ops/tasks/trigger",
        json={"name": "backend.tasks.evil_secret_op"},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_trigger_valid_task_calls_send_task(client):
    """End-to-end: POST → OpsService.trigger_task → celery_app.send_task."""
    sent = []

    def _send(name, kwargs=None, **rest):
        sent.append((name, kwargs))
        m = MagicMock()
        m.id = "fake-task-id-001"
        return m

    with patch("backend.celery_app.celery_app") as mock_app, \
         patch("backend.services.ops_service.OpsService._redis",
               side_effect=ConnectionError("redis off — fail-open path")):
        mock_app.send_task.side_effect = _send

        r = await client.post(
            "/api/v1/ops/tasks/trigger",
            json={"name": "backend.tasks.run_pillar_balance_check"},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["task_id"] == "fake-task-id-001"
    assert body["name"] == "backend.tasks.run_pillar_balance_check"
    assert sent == [("backend.tasks.run_pillar_balance_check", {})]


@pytest.mark.asyncio
async def test_trigger_per_task_throttle_409(client):
    """Two triggers of the same task within 60s → 409 on the second."""
    fake_redis = _MiniRedis()

    def _send(name, kwargs=None, **rest):
        m = MagicMock()
        m.id = "ok"
        return m

    with patch("backend.celery_app.celery_app") as mock_app, \
         patch("backend.services.ops_service.OpsService._redis",
               return_value=fake_redis):
        mock_app.send_task.side_effect = _send
        r1 = await client.post(
            "/api/v1/ops/tasks/trigger",
            json={"name": "backend.tasks.run_pillar_balance_check"},
        )
        r2 = await client.post(
            "/api/v1/ops/tasks/trigger",
            json={"name": "backend.tasks.run_pillar_balance_check"},
        )

    assert r1.status_code == 200
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_trigger_global_throttle_429(client):
    """Once GLOBAL_THROTTLE_LIMIT is reached the 11th trigger gets 429."""
    from backend.services.ops_service import GLOBAL_THROTTLE_LIMIT, OpsService

    def _send(name, kwargs=None, **rest):
        m = MagicMock()
        m.id = "ok"
        return m

    fake_redis = _MiniRedis()
    with patch("backend.celery_app.celery_app") as mock_app, \
         patch("backend.services.ops_service.OpsService._redis",
               return_value=fake_redis), \
         patch.object(OpsService, "_check_per_task_throttle", return_value=0):
        mock_app.send_task.side_effect = _send

        for _ in range(GLOBAL_THROTTLE_LIMIT):
            r = await client.post(
                "/api/v1/ops/tasks/trigger",
                json={"name": "backend.tasks.run_pillar_balance_check"},
            )
            assert r.status_code == 200

        r = await client.post(
            "/api/v1/ops/tasks/trigger",
            json={"name": "backend.tasks.run_pillar_balance_check"},
        )
        assert r.status_code == 429


# ---------------------------------------------------------------------------
# /ops/tasks/recent-runs
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recent_runs_redis_outage_returns_empty(client):
    with patch("backend.services.ops_service.OpsService._redis",
               side_effect=ConnectionError("redis off")):
        r = await client.get("/api/v1/ops/tasks/recent-runs?limit=5")
    assert r.status_code == 200
    assert r.json() == []


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_missing_token_401_when_env_var_set(client, monkeypatch):
    monkeypatch.setenv("OPS_API_TOKEN", "super-secret")
    r = await client.get("/api/v1/ops/flags")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_correct_token_200(client, monkeypatch):
    monkeypatch.setenv("OPS_API_TOKEN", "super-secret")
    r = await client.get(
        "/api/v1/ops/flags",
        headers={"X-Ops-Token": "super-secret"},
    )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_wrong_token_401(client, monkeypatch):
    """Token mismatch → 401, not 200 (regression: review found this untested)."""
    monkeypatch.setenv("OPS_API_TOKEN", "super-secret")
    r = await client.get(
        "/api/v1/ops/flags",
        headers={"X-Ops-Token": "wrong-secret"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_empty_token_header_401(client, monkeypatch):
    """X-Ops-Token: '' (empty string sent) must NOT auth-bypass — only an
    unset OPS_API_TOKEN env var disables auth."""
    monkeypatch.setenv("OPS_API_TOKEN", "super-secret")
    r = await client.get(
        "/api/v1/ops/flags",
        headers={"X-Ops-Token": ""},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_per_task_lock_does_not_block_other_tasks(client):
    """Per-task SETNX is keyed by task name — distinct tasks don't collide."""
    fake_redis = _MiniRedis()

    def _send(name, kwargs=None, **rest):
        m = MagicMock()
        m.id = "ok"
        return m

    with patch("backend.celery_app.celery_app") as mock_app, \
         patch("backend.services.ops_service.OpsService._redis",
               return_value=fake_redis):
        mock_app.send_task.side_effect = _send

        # Two distinct whitelisted tasks back-to-back — both must succeed
        r1 = await client.post(
            "/api/v1/ops/tasks/trigger",
            json={"name": "backend.tasks.run_pillar_balance_check"},
        )
        r2 = await client.post(
            "/api/v1/ops/tasks/trigger",
            json={"name": "backend.tasks.run_alpha_health_check"},
        )

    assert r1.status_code == 200
    assert r2.status_code == 200


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _MiniRedis:
    """Minimum subset of redis used by OpsService throttle checks."""

    def __init__(self):
        self.kv = {}
        self.ttls = {}
        self.deleted = []

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    def ttl(self, key):
        return self.ttls.get(key, -2)

    def delete(self, key):
        self.deleted.append(key)
        self.kv.pop(key, None)
        return 1

    def incr(self, key):
        cur = int(self.kv.get(key, 0)) + 1
        self.kv[key] = cur
        return cur

    def expire(self, key, sec):
        self.ttls[key] = sec
        return True

    def keys(self, pattern):
        return []

    def get(self, key):
        return self.kv.get(key)
