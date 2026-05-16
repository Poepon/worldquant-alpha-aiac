"""Integration tests for /api/v1/ops/llm-op/* endpoints (Phase 4)."""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import AsyncGenerator
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Column, Integer, MetaData, Table
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import get_db
from backend.models.config import FeatureFlagAudit, FeatureFlagOverride
from backend.models.transition import HypothesisStatusTransition
from backend.routers.ops import router as ops_router
from backend.services.feature_flag_service import _flag_override_cache
from backend.services.ops_report_reader import _reset_read_cache_for_tests


SAMPLE_MD = """# LLM op hallucination monitor — 2026-05-16

**Active KB entries scanned**: 2144
**Valid BRAIN ops in registry**: 66
**Clean entries**: 2139
**Pattern-level hallucinations**: 0
**Template-only hallucinations**: 5
**Deactivated**: 2

## Hallucinated op names (count of entries)

- `sign_flip` — 2
- `window` — 1

## Affected entries (first 30)

| KB# | source | bad_ops | pattern |
|---|---|---|---|
| 1191 | template | window | `ts_arg_max(returns, 5)` |
| 5662 | template | sign_flip | `multiply(-1, ts_decay_linear(x, 4))` |
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def isolated_engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    isolated = MetaData()
    FeatureFlagOverride.__table__.to_metadata(isolated)
    FeatureFlagAudit.__table__.to_metadata(isolated)
    Table("hypotheses", isolated, Column("id", Integer, primary_key=True))
    HypothesisStatusTransition.__table__.to_metadata(isolated)
    async with engine.begin() as conn:
        await conn.run_sync(isolated.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session_maker(isolated_engine):
    return sessionmaker(isolated_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _clear_state():
    _flag_override_cache.clear()
    _reset_read_cache_for_tests()
    prev = os.environ.pop("OPS_API_TOKEN", None)
    yield
    _flag_override_cache.clear()
    _reset_read_cache_for_tests()
    if prev is not None:
        os.environ["OPS_API_TOKEN"] = prev


@pytest.fixture
def docs_root(tmp_path: Path, monkeypatch) -> Path:
    docs = tmp_path / "docs"
    monkeypatch.setattr("backend.services.ops_report_reader._DOCS_ROOT", docs)
    return docs


@pytest_asyncio.fixture
async def client(session_maker) -> AsyncGenerator[AsyncClient, None]:
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
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_llm_op_latest_today(client, docs_root):
    kind_dir = docs_root / "llm_op_monitor"
    kind_dir.mkdir(parents=True)
    today = date.today()
    (kind_dir / f"{today.isoformat()}.md").write_text(SAMPLE_MD, encoding="utf-8")

    r = await client.get("/api/v1/ops/llm-op/latest")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "docs_today"
    assert body["summary"]["scanned"] == 2144
    assert body["summary"]["deactivated"] == 2
    assert len(body["summary"]["hallucinated_ops"]) == 2
    assert body["summary"]["hallucinated_ops"][0] == {"op": "sign_flip", "count": 2}


@pytest.mark.asyncio
async def test_llm_op_latest_missing(client, docs_root):
    r = await client.get("/api/v1/ops/llm-op/latest")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "missing"
    assert body["summary"]["scanned"] == 0


@pytest.mark.asyncio
async def test_llm_op_deactivated_kb_lists_affected(client, docs_root):
    kind_dir = docs_root / "llm_op_monitor"
    kind_dir.mkdir(parents=True)
    today = date.today()
    (kind_dir / f"{today.isoformat()}.md").write_text(SAMPLE_MD, encoding="utf-8")

    r = await client.get("/api/v1/ops/llm-op/deactivated-kb")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2
    assert rows[0]["kb_id"] == 1191


@pytest.mark.asyncio
async def test_llm_op_rerun(client):
    fake_redis = _MiniRedis()
    with patch("backend.celery_app.celery_app") as mock_app, \
         patch("backend.services.ops_service.OpsService._redis",
               return_value=fake_redis):
        mock_app.send_task.side_effect = lambda name, **k: _AsyncResult("llm-id")
        r = await client.post("/api/v1/ops/llm-op/rerun")
    assert r.status_code == 200
    assert r.json()["name"] == "backend.tasks.monitor_llm_op_hallucinations"


@pytest.mark.asyncio
async def test_llm_op_endpoints_require_token(client, monkeypatch):
    monkeypatch.setenv("OPS_API_TOKEN", "shhh")
    r = await client.get("/api/v1/ops/llm-op/latest")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _MiniRedis:
    def __init__(self):
        self.kv = {}
        self.ttls = {}

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


class _AsyncResult:
    def __init__(self, id_):
        self.id = id_
