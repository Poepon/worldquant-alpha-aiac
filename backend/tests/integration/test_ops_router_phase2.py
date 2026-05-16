"""Integration tests for /api/v1/ops/* Phase 2 endpoints.

来源: docs/alphagbm_skills_research_2026-05-15.md ops dashboard plan §1.2 (Phase 2).

Re-uses the isolated FastAPI + aiosqlite + JSONB-free metadata fixture
pattern from test_ops_router.py. Each test points the OpsReportReader
at a tmp_path docs directory via monkeypatch.

Coverage:
* GET /alpha-health/latest with docs / missing / archived sources
* GET /alpha-health/history chronological
* GET /alpha-health/alphas filtering (band, region, limit)
* POST /alpha-health/rerun maps to send_task + per-task 60s lock
* GET /hypothesis-health/latest + history
* GET /hypothesis-health/transitions (empty when no DB rows)
* POST /hypothesis-health/rerun
* GET /overview fans out to all sources

We do NOT seed HypothesisStatusTransition rows here — that table imports
fine without JSONB, but seeding it requires a hypothesis row which drags
in JSONB columns. The empty-transitions case is the meaningful one for
this layer; full seed coverage lives in the live-PG integration suite.
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
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
from backend.services.ops_report_reader import (
    OpsReportReader,
    _reset_read_cache_for_tests,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def isolated_engine():
    """Same JSONB-free trick as Phase 1 router tests — only mount the
    tables we actually need on aiosqlite."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    isolated = MetaData()
    FeatureFlagOverride.__table__.to_metadata(isolated)
    FeatureFlagAudit.__table__.to_metadata(isolated)
    # Stub for the FK target — Hypothesis model has JSONB columns we don't
    # want to bring in. SQLite ignores FK enforcement by default; the stub
    # just gives `metadata.create_all` something to point at so DDL emits
    # cleanly.
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
    monkeypatch.setattr(
        "backend.services.ops_report_reader._DOCS_ROOT", docs,
    )
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


# ---------------------------------------------------------------------------
# Alpha Health
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_alpha_health_latest_docs_today(client, docs_root):
    today = OpsReportReader.today_sh()
    _write(docs_root, "alpha_health_check", today, {
        "report_date": today.isoformat(),
        "records": [
            {"region": "USA", "health_band": "GREEN", "alpha_id": "a1"},
            {"region": "CHN", "health_band": "RED", "alpha_id": "a2"},
        ],
    })
    r = await client.get("/api/v1/ops/alpha-health/latest")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "docs_today"
    assert body["summary"]["band_counts"] == {"GREEN": 1, "RED": 1}
    assert body["summary"]["by_region"]["USA"] == {"GREEN": 1}
    assert len(body["payload"]["records"]) == 2


@pytest.mark.asyncio
async def test_alpha_health_latest_missing_returns_empty_summary(client, docs_root):
    r = await client.get("/api/v1/ops/alpha-health/latest")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "missing"
    assert body["summary"]["total_alphas"] == 0
    assert body["payload"] == {}


@pytest.mark.asyncio
async def test_alpha_health_history_chronological(client, docs_root):
    today = OpsReportReader.today_sh()
    _write(docs_root, "alpha_health_check", today, {
        "records": [{"region": "USA", "health_band": "GREEN"}],
    })
    _write(docs_root, "alpha_health_check", today - timedelta(days=3), {
        "records": [{"region": "USA", "health_band": "RED"}],
    })
    r = await client.get("/api/v1/ops/alpha-health/history?days=7")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    assert body[0]["_date"] == (today - timedelta(days=3)).isoformat()
    assert body[1]["_date"] == today.isoformat()


@pytest.mark.asyncio
async def test_alpha_health_records_filter(client, docs_root):
    today = OpsReportReader.today_sh()
    _write(docs_root, "alpha_health_check", today, {
        "records": [
            {"alpha_id": "a1", "region": "USA", "health_band": "GREEN"},
            {"alpha_id": "a2", "region": "USA", "health_band": "RED"},
            {"alpha_id": "a3", "region": "CHN", "health_band": "RED"},
        ],
    })
    r = await client.get("/api/v1/ops/alpha-health/alphas?band=RED&region=USA")
    assert r.status_code == 200
    body = r.json()
    assert [rec["alpha_id"] for rec in body["records"]] == ["a2"]
    assert body["total_unfiltered"] == 3


@pytest.mark.asyncio
async def test_alpha_health_rerun_calls_send_task(client):
    fake_redis = _MiniRedis()

    def _send(name, kwargs=None, **rest):
        m = MagicMock()
        m.id = "fake-id"
        return m

    with patch("backend.celery_app.celery_app") as mock_app, \
         patch("backend.services.ops_service.OpsService._redis",
               return_value=fake_redis):
        mock_app.send_task.side_effect = _send
        r = await client.post("/api/v1/ops/alpha-health/rerun")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "backend.tasks.run_alpha_health_check"


@pytest.mark.asyncio
async def test_alpha_health_rerun_throttle_409(client):
    fake_redis = _MiniRedis()

    def _send(name, kwargs=None, **rest):
        m = MagicMock()
        m.id = "ok"
        return m

    with patch("backend.celery_app.celery_app") as mock_app, \
         patch("backend.services.ops_service.OpsService._redis",
               return_value=fake_redis):
        mock_app.send_task.side_effect = _send
        r1 = await client.post("/api/v1/ops/alpha-health/rerun")
        r2 = await client.post("/api/v1/ops/alpha-health/rerun")
    assert r1.status_code == 200
    assert r2.status_code == 409


# ---------------------------------------------------------------------------
# Hypothesis Health
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hypothesis_health_latest(client, docs_root):
    today = OpsReportReader.today_sh()
    _write(docs_root, "hypothesis_health_check", today, {
        "report_date": today.isoformat(),
        "hypotheses": [
            {"is_triggered": True, "thesis_score": 65,
             "trigger_detail": {"fired": ["sharpe_down"]}},
            {"is_triggered": False, "thesis_score": 80},
        ],
    })
    r = await client.get("/api/v1/ops/hypothesis-health/latest")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "docs_today"
    assert body["summary"]["total_active"] == 2
    assert body["summary"]["total_triggered"] == 1
    assert body["summary"]["trigger_histogram"] == {"sharpe_down": 1}


@pytest.mark.asyncio
async def test_hypothesis_transitions_empty(client):
    """No rows in HypothesisStatusTransition table → empty list, 200."""
    r = await client.get("/api/v1/ops/hypothesis-health/transitions?limit=10")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_hypothesis_health_rerun(client):
    fake_redis = _MiniRedis()
    with patch("backend.celery_app.celery_app") as mock_app, \
         patch("backend.services.ops_service.OpsService._redis",
               return_value=fake_redis):
        mock_app.send_task.side_effect = lambda name, **k: _AsyncResult("hyp-id")
        r = await client.post("/api/v1/ops/hypothesis-health/rerun")
    assert r.status_code == 200
    assert r.json()["name"] == "backend.tasks.run_hypothesis_health_check"


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_overview_with_partial_sources(client, docs_root):
    today = OpsReportReader.today_sh()
    _write(docs_root, "alpha_health_check", today, {
        "records": [{"region": "USA", "health_band": "GREEN"}],
    })
    _write(docs_root, "regime_state", today, {
        "regions": {"USA": {"regime": "calm"}},
    })

    r = await client.get("/api/v1/ops/overview")
    assert r.status_code == 200
    body = r.json()

    # All 7 beat keys present, mixed source tags
    assert set(body["beat_status"].keys()) == {
        "alpha_health_check", "hypothesis_health_check", "pillar_balance",
        "regime_infer", "negative_knowledge_extract",
        "macro_narrative_extract", "llm_op_monitor",
    }
    assert body["beat_status"]["alpha_health_check"]["source"] == "docs_today"
    assert body["beat_status"]["pillar_balance"]["source"] == "missing"
    assert body["region_regime"]["USA"] == "calm"
    assert body["alpha_health_summary"]["band_counts"] == {"GREEN": 1}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_phase2_endpoints_require_token_when_env_set(client, monkeypatch):
    monkeypatch.setenv("OPS_API_TOKEN", "shhh")
    r = await client.get("/api/v1/ops/alpha-health/latest")
    assert r.status_code == 401

    r = await client.get(
        "/api/v1/ops/alpha-health/latest",
        headers={"X-Ops-Token": "shhh"},
    )
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Test helpers
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
