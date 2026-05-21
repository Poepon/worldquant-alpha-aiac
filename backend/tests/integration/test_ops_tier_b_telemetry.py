"""Phase 4 Tier B — obs telemetry endpoint integration tests (2026-05-20).

End-to-end via FastAPI ASGI transport on in-memory aiosqlite:
  - /ops/r11/capacity-stats: column-based, cross-dialect → real data test
  - /ops/r13/factor-residuals: JSONB → Postgres-only → empty-degrade on SQLite
  - /ops/r13/snapshot-stale-check: filesystem → real test (no DB)
  - /ops/g3v2/parse-stats: JSONB → Postgres-only → empty-degrade on SQLite
"""
from __future__ import annotations

import os

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import SQLAlchemyBase, get_db
from backend.routers.ops import router as ops_router


@pytest.fixture(autouse=True)
def _isolate_ops_token():
    prev = os.environ.pop("OPS_API_TOKEN", None)
    yield
    if prev is not None:
        os.environ["OPS_API_TOKEN"] = prev
    else:
        os.environ.pop("OPS_API_TOKEN", None)


@pytest_asyncio.fixture
async def app_db_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLAlchemyBase.metadata.create_all)
    session_maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _override_get_db():
        async with session_maker() as s:
            yield s

    seed_session = session_maker()
    try:
        yield seed_session, _override_get_db
    finally:
        await seed_session.rollback()
        await seed_session.close()
        await engine.dispose()


@pytest_asyncio.fixture
async def client_factory(app_db_session):
    _seed, override_get_db = app_db_session

    async def _build():
        app = FastAPI()
        app.include_router(ops_router, prefix="/api/v1")
        app.dependency_overrides[get_db] = override_get_db
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test")
    yield _build


async def _seed_alpha(seed_session, **kwargs):
    from backend.models import Alpha
    defaults = dict(
        alpha_id=None, expression="rank(close)", region="USA",
        universe="TOP3000", quality_status="PASS",
    )
    defaults.update(kwargs)
    seed_session.add(Alpha(**defaults))
    await seed_session.commit()


# ---------------------------------------------------------------------------
# R11 capacity-stats (cross-dialect column)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_r11_capacity_stats_buckets(app_db_session, client_factory):
    seed, _ = app_db_session
    # 3 alphas with capacity in different buckets
    await _seed_alpha(seed, alpha_id="a1", capacity_usd_estimate=5e6, quality_status="PASS")    # $1M-$10M
    await _seed_alpha(seed, alpha_id="a2", capacity_usd_estimate=5e9, quality_status="PASS")    # $1B-$10B
    await _seed_alpha(seed, alpha_id="a3", capacity_usd_estimate=5e7, quality_status="FAIL")    # $10M-$100M
    # 1 alpha without capacity → excluded
    await _seed_alpha(seed, alpha_id="a4", capacity_usd_estimate=None)

    client = await client_factory()
    async with client:
        resp = await client.get("/api/v1/ops/r11/capacity-stats?days=30")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_with_capacity"] == 3
    assert body["pass_count_with_capacity"] == 2
    assert body["capacity_pass_rate"] == pytest.approx(2 / 3, rel=1e-3)
    # bucket counts
    bucket_map = {b["bucket_label"]: b["count"] for b in body["buckets"]}
    assert bucket_map["$1M-$10M"] == 1
    assert bucket_map["$10M-$100M"] == 1
    assert bucket_map["$1B-$10B"] == 1


@pytest.mark.asyncio
async def test_r11_capacity_stats_empty(app_db_session, client_factory):
    client = await client_factory()
    async with client:
        resp = await client.get("/api/v1/ops/r11/capacity-stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_with_capacity"] == 0
    assert body["capacity_pass_rate"] == 0.0


# ---------------------------------------------------------------------------
# R13 factor-residuals (JSONB → empty-degrade on SQLite)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_r13_factor_residuals_degrades_on_sqlite(app_db_session, client_factory):
    client = await client_factory()
    async with client:
        resp = await client.get("/api/v1/ops/r13/factor-residuals")
    assert resp.status_code == 200
    body = resp.json()
    # SQLite dev → empty payload (Postgres-only JSONB query guarded)
    assert body["total_decomposed"] == 0
    assert body["by_mode"] == {}
    assert "ENABLE_FACTOR_LENS" in body["flags"]


# ---------------------------------------------------------------------------
# R13 snapshot-stale-check (filesystem, no DB)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_r13_snapshot_stale_check_missing_files(app_db_session, client_factory):
    client = await client_factory()
    async with client:
        resp = await client.get("/api/v1/ops/r13/snapshot-stale-check")
    assert resp.status_code == 200
    body = resp.json()
    # No real parquet files shipped → all regions stale
    assert body["any_stale"] is True
    assert set(body["per_region"].keys()) == {"usa", "chn", "jpn", "eur", "hkg"}
    for region, st in body["per_region"].items():
        assert st["exists"] is False
        assert st["stale"] is True
    assert body["stale_threshold_days"] == 90


@pytest.mark.asyncio
async def test_r13_snapshot_stale_custom_threshold(app_db_session, client_factory):
    client = await client_factory()
    async with client:
        resp = await client.get("/api/v1/ops/r13/snapshot-stale-check?stale_days=30")
    assert resp.status_code == 200
    assert resp.json()["stale_threshold_days"] == 30


# ---------------------------------------------------------------------------
# G3-v2 parse-stats (JSONB → empty-degrade on SQLite)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_g3v2_parse_stats_degrades_on_sqlite(app_db_session, client_factory):
    client = await client_factory()
    async with client:
        resp = await client.get("/api/v1/ops/g3v2/parse-stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["degrade_open_readmit_count"] == 0
    assert body["unknown_ops_alpha_count"] == 0
    assert body["top_unknown_ops"] == {}
    assert "ENABLE_GRAMMAR_VALIDATOR" in body["flags"]
