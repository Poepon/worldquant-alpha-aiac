"""Integration tests for /api/v1/ops/* Phase 3 endpoints (16 new routes).

来源: docs/alphagbm_skills_research_2026-05-15.md ops dashboard plan Phase 3.

Tests mount only the ops router on an isolated FastAPI app + JSONB-free
aiosqlite session. The underlying child services (PillarService /
NegativeKnowledgeService / MacroNarrativeService / RegimeInferenceService)
are mocked via ``patch`` so we never touch the JSONB-bearing knowledge
tables in unit-test layer.

Live-PG coverage for the SQL paths lives in the existing per-service
integration suites (test_pillar_balance_check.py, test_negative_knowledge_
service.py, test_macro_narrative_service.py, test_regime_inference_
service.py) — Phase 3 does not change any of those underlying queries.
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

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
from backend.services.ops_report_reader import (
    OpsReportReader,
    _reset_read_cache_for_tests,
)


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


def _write(docs_root: Path, kind: str, d: date, payload: dict) -> None:
    sub = docs_root / kind
    sub.mkdir(parents=True, exist_ok=True)
    (sub / f"{d.isoformat()}.json").write_text(json.dumps(payload), encoding="utf-8")


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


# ===========================================================================
# Pillar endpoints
# ===========================================================================

@pytest.mark.asyncio
async def test_pillar_latest_fresh_service(client, docs_root):
    today = OpsReportReader.today_sh()
    with patch(
        "backend.services.pillar_service.PillarService.compute_balance_report",
        new=AsyncMock(return_value={
            "report_date": today.isoformat(),
            "regions": {"USA": {"shares": {"momentum": 0.5}}},
        }),
    ):
        r = await client.get("/api/v1/ops/pillar/latest")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "service"
    assert body["payload"]["regions"]["USA"]["shares"]["momentum"] == 0.5


@pytest.mark.asyncio
async def test_pillar_latest_archive(client, docs_root):
    yesterday = OpsReportReader.today_sh() - timedelta(days=1)
    _write(docs_root, "pillar_balance", yesterday, {"from": "yesterday"})
    r = await client.get(f"/api/v1/ops/pillar/latest?date={yesterday.isoformat()}")
    assert r.status_code == 200
    assert r.json()["source"] == "docs_archived"


@pytest.mark.asyncio
async def test_pillar_history(client, docs_root):
    today = OpsReportReader.today_sh()
    _write(docs_root, "pillar_balance", today, {"x": 1})
    _write(docs_root, "pillar_balance", today - timedelta(days=2), {"x": 2})
    r = await client.get("/api/v1/ops/pillar/history?days=5")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_pillar_deficit_recommendation(client):
    with patch(
        "backend.services.pillar_service.PillarService.get_next_pillar_for_region",
        new=AsyncMock(return_value="value"),
    ):
        r = await client.get("/api/v1/ops/pillar/deficit-recommendation?region=USA")
    assert r.status_code == 200
    assert r.json() == {"region": "USA", "next_pillar": "value"}


@pytest.mark.asyncio
async def test_pillar_rerun(client):
    fake_redis = _MiniRedis()
    with patch("backend.celery_app.celery_app") as mock_app, \
         patch("backend.services.ops_service.OpsService._redis", return_value=fake_redis):
        mock_app.send_task.side_effect = lambda name, **k: _AsyncResult("pillar-id")
        r = await client.post("/api/v1/ops/pillar/rerun")
    assert r.status_code == 200
    assert r.json()["name"] == "backend.tasks.run_pillar_balance_check"


# ===========================================================================
# Negative Knowledge endpoints
# ===========================================================================

@pytest.mark.asyncio
async def test_negative_top(client):
    with patch(
        "backend.services.negative_knowledge_service."
        "NegativeKnowledgeService.fetch_top_pitfalls_admin",
        new=AsyncMock(return_value=[
            {"id": 1, "pattern": "rank(close)", "fail_count": 10, "category": "scaffold"},
        ]),
    ):
        r = await client.get("/api/v1/ops/negative-knowledge/top?region=USA&limit=5")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "service"
    assert body["records"][0]["fail_count"] == 10


@pytest.mark.asyncio
async def test_negative_category_breakdown(client):
    with patch(
        "backend.services.negative_knowledge_service."
        "NegativeKnowledgeService.aggregate_by_category",
        new=AsyncMock(return_value={"scaffold": 8, "robustness": 2}),
    ):
        r = await client.get("/api/v1/ops/negative-knowledge/category-breakdown")
    assert r.status_code == 200
    assert r.json()["by_category"]["scaffold"] == 8


@pytest.mark.asyncio
async def test_negative_timeline(client):
    with patch(
        "backend.services.negative_knowledge_service."
        "NegativeKnowledgeService.get_pitfall_timeline",
        new=AsyncMock(return_value=[
            {"date": "2026-05-15", "new_count": 4},
        ]),
    ):
        r = await client.get("/api/v1/ops/negative-knowledge/timeline?days=7")
    assert r.status_code == 200
    assert r.json() == [{"date": "2026-05-15", "new_count": 4}]


@pytest.mark.asyncio
async def test_negative_toggle_success(client):
    with patch(
        "backend.services.ops_service.OpsService.set_pitfall_active",
        new=AsyncMock(return_value=True),
    ):
        r = await client.patch(
            "/api/v1/ops/negative-knowledge/entries/42",
            json={"is_active": False},
        )
    assert r.status_code == 200
    assert r.json() == {"id": 42, "is_active": False, "updated": True}


@pytest.mark.asyncio
async def test_negative_toggle_not_found(client):
    with patch(
        "backend.services.ops_service.OpsService.set_pitfall_active",
        new=AsyncMock(return_value=False),
    ):
        r = await client.patch(
            "/api/v1/ops/negative-knowledge/entries/9999",
            json={"is_active": True},
        )
    assert r.status_code == 404


# ===========================================================================
# Macro Narrative endpoints
# ===========================================================================

@pytest.mark.asyncio
async def test_macro_latest(client, docs_root):
    today = OpsReportReader.today_sh()
    _write(docs_root, "macro_narratives", today, {
        "report_date": today.isoformat(),
        "seed_counters": {"new": 5},
    })
    r = await client.get("/api/v1/ops/macro/latest")
    assert r.status_code == 200
    assert r.json()["source"] == "docs_today"
    assert r.json()["payload"]["seed_counters"]["new"] == 5


@pytest.mark.asyncio
async def test_macro_coverage(client):
    with patch(
        "backend.services.macro_narrative_service."
        "MacroNarrativeService.coverage_stats",
        new=AsyncMock(return_value={
            "by_scope": {"field": 8, "category": 4},
            "total": 12,
            "fields_total": 100,
            "fields_with_narrative": 8,
            "fields_coverage_pct": 8.0,
        }),
    ):
        r = await client.get("/api/v1/ops/macro/coverage")
    assert r.status_code == 200
    assert r.json()["coverage"]["fields_coverage_pct"] == 8.0


@pytest.mark.asyncio
async def test_macro_by_scope_validates_scope(client):
    """Pydantic regex rejects scope=foo with 422."""
    r = await client.get("/api/v1/ops/macro/by-scope?scope=foo")
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_macro_by_scope_returns_records(client):
    with patch(
        "backend.services.macro_narrative_service."
        "MacroNarrativeService.list_narratives_by_scope",
        new=AsyncMock(return_value=[{"id": 1, "field_id": "close", "scope": "field"}]),
    ):
        r = await client.get("/api/v1/ops/macro/by-scope?scope=field&limit=10")
    assert r.status_code == 200
    assert r.json()["records"][0]["field_id"] == "close"


@pytest.mark.asyncio
async def test_macro_token_budget(client, monkeypatch):
    fake = MagicMock()
    fake.get.return_value = b"123"
    monkeypatch.setattr("backend.tasks.redis_pool.get_redis_client", lambda: fake)
    r = await client.get("/api/v1/ops/macro/token-budget?utc_date=2026-05-16")
    assert r.status_code == 200
    body = r.json()
    assert body["tokens_used"] == 123
    assert body["redis_ok"] is True


# ===========================================================================
# Test helpers
# ===========================================================================

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
        self.ttls.pop(key, None)
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
