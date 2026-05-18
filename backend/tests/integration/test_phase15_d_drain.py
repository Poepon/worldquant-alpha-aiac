"""Phase 15-D PR2: operator drain endpoint + readiness upgrade tests.

POST /api/v1/ops/cascade-deprecation/drain — idempotent + audit-trail
conversion of PAUSED CONTINUOUS_CASCADE rows to STOPPED.

Readiness endpoint upgrade verifies new branch + cascade_legacy_flag_on
field per phase15-D PR2.
"""
from __future__ import annotations

import os
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


@pytest.fixture(autouse=True)
def _isolate_default_flat_flag():
    """phase15-D PR3c (2026-05-18): ENABLE_CASCADE_LEGACY retired (cascade
    always-refused). Only ENABLE_DEFAULT_FLAT_SESSION still needs isolation."""
    from backend.config import settings as _stg
    saved = getattr(_stg, "ENABLE_DEFAULT_FLAT_SESSION", False)
    yield
    setattr(_stg, "ENABLE_DEFAULT_FLAT_SESSION", saved)


# ---------------------------------------------------------------------------
# Drain endpoint mocks
# ---------------------------------------------------------------------------

def _mk_drain_db(running_ids, paused_rows):
    """Mock db.execute returning running query then paused query then
    accepting per-row UPDATE statements + commit."""
    running_r = MagicMock(); running_r.all = MagicMock(return_value=[(i,) for i in running_ids])
    paused_r = MagicMock(); paused_r.all = MagicMock(return_value=paused_rows)
    # UPDATEs return a result-like; we don't care about the contents
    update_r = MagicMock()
    db = AsyncMock()
    # First 2 calls: SELECT running then SELECT paused; then any UPDATEs
    db.execute = AsyncMock(side_effect=lambda *a, **kw: (
        running_r if len(db.execute.await_args_list) <= 1
        else (paused_r if len(db.execute.await_args_list) == 2 else update_r)
    ))
    db.commit = AsyncMock(return_value=None)
    return db


@pytest_asyncio.fixture
async def client():
    async def _factory(running_ids, paused_rows):
        app = FastAPI()
        app.include_router(ops_router, prefix="/api/v1")
        app.dependency_overrides[get_db] = lambda: _mk_drain_db(running_ids, paused_rows)
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test")
    return _factory


# ---------------------------------------------------------------------------
# Drain endpoint behavior
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_drain_paused_rows_to_stopped(client):
    """2 PAUSED rows + 0 RUNNING → both drained, paused_after=0."""
    # (id, config jsonb, status)
    paused_rows = [(101, {}, "PAUSED"), (102, {}, "PAUSED")]
    c = await client([], paused_rows)
    async with c as ac:
        r = await ac.post("/api/v1/ops/cascade-deprecation/drain")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_paused_before"] == 2
    assert body["total_paused_after"] == 0
    assert len(body["drained"]) == 2
    assert body["skipped_running"] == []
    assert body["already_drained"] == []
    assert {d["task_id"] for d in body["drained"]} == {101, 102}


@pytest.mark.asyncio
async def test_drain_skips_already_drained(client):
    """Idempotent: rows with cascade_drained audit already set are skipped."""
    paused_rows = [
        (200, {"cascade_drained": {"at": "2026-05-18", "by": "ops_drain"}}, "PAUSED"),
        (201, {}, "PAUSED"),
    ]
    c = await client([], paused_rows)
    async with c as ac:
        r = await ac.post("/api/v1/ops/cascade-deprecation/drain")
    body = r.json()
    assert body["already_drained"] == [200]
    assert len(body["drained"]) == 1
    assert body["drained"][0]["task_id"] == 201


@pytest.mark.asyncio
async def test_drain_refuses_when_cascade_running(client):
    """RUNNING cascade rows surfaced as skipped_running — drain proceeds on
    PAUSED but operator sees the warning to stop RUNNING first."""
    c = await client([300, 301], [(302, {}, "PAUSED")])
    async with c as ac:
        r = await ac.post("/api/v1/ops/cascade-deprecation/drain")
    body = r.json()
    assert body["skipped_running"] == [300, 301]
    # PAUSED row still drained
    assert len(body["drained"]) == 1


@pytest.mark.asyncio
async def test_drain_empty_db_returns_empty_arrays(client):
    c = await client([], [])
    async with c as ac:
        r = await ac.post("/api/v1/ops/cascade-deprecation/drain")
    body = r.json()
    assert body["drained"] == []
    assert body["skipped_running"] == []
    assert body["already_drained"] == []
    assert body["total_paused_before"] == 0
    assert body["total_paused_after"] == 0


@pytest.mark.asyncio
async def test_drain_requires_ops_token_when_env_set(client):
    os.environ["OPS_API_TOKEN"] = "abc123"
    c = await client([], [])
    async with c as ac:
        r = await ac.post("/api/v1/ops/cascade-deprecation/drain")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Readiness upgrade — new branch + cascade_legacy_flag_on field
# ---------------------------------------------------------------------------

def _mk_readiness_db(rows):
    r = MagicMock(); r.all = MagicMock(return_value=rows)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=r)
    return db


@pytest_asyncio.fixture
async def readiness_client():
    async def _factory(rows):
        app = FastAPI()
        app.include_router(ops_router, prefix="/api/v1")
        app.dependency_overrides[get_db] = lambda: _mk_readiness_db(rows)
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test")
    return _factory


@pytest.mark.asyncio
async def test_readiness_response_includes_cascade_legacy_flag_on(readiness_client):
    """phase15-D PR2: new field cascade_legacy_flag_on in response."""
    c = await readiness_client([])
    async with c as ac:
        r = await ac.get("/api/v1/ops/cascade-deprecation/readiness")
    assert "cascade_legacy_flag_on" in r.json()


@pytest.mark.asyncio
async def test_readiness_cascade_legacy_flag_always_false_post_pr3c(
    readiness_client, monkeypatch,
):
    """phase15-D PR3c: ENABLE_CASCADE_LEGACY retired — cascade_legacy_flag_on
    in response is hardcoded False (cascade always-refused)."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_DEFAULT_FLAT_SESSION", True, raising=False)
    c = await readiness_client([("FLAT_CONTINUOUS", "RUNNING", "USA", 1)])
    async with c as ac:
        r = await ac.get("/api/v1/ops/cascade-deprecation/readiness")
    body = r.json()
    assert body["cascade_legacy_flag_on"] is False


@pytest.mark.asyncio
async def test_readiness_all_green_post_pr3c(readiness_client, monkeypatch):
    """phase15-D PR3c: 0 cascade RUNNING/PAUSED + flat active +
    default_flat ON → ready_to_delete=True + next_action describes
    PR3d + PR4b remaining cleanup."""
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_DEFAULT_FLAT_SESSION", True, raising=False)
    c = await readiness_client([("FLAT_CONTINUOUS", "RUNNING", "USA", 1)])
    async with c as ac:
        r = await ac.get("/api/v1/ops/cascade-deprecation/readiness")
    body = r.json()
    assert body["ready_to_delete"] is True
    assert "PR3d" in body["next_action"] or "PR4b" in body["next_action"]


@pytest.mark.asyncio
async def test_readiness_cascade_paused_says_call_drain_endpoint(readiness_client):
    """1 PAUSED cascade → next_action mentions the drain endpoint."""
    c = await readiness_client([("CONTINUOUS_CASCADE", "PAUSED", "USA", 1)])
    async with c as ac:
        r = await ac.get("/api/v1/ops/cascade-deprecation/readiness")
    body = r.json()
    assert body["ready_to_delete"] is False
    assert "drain" in body["next_action"]
    assert body["cascade_paused_count"] == 1
