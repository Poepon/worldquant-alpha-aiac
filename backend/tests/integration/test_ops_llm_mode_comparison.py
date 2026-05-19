"""Phase 4 Sprint 1 A1.4 — /ops/llm-mode/{comparison,go-gate} integration tests.

End-to-end via FastAPI ASGI transport + in-memory aiosqlite:
  - GET /ops/llm-mode/comparison returns stratified buckets
  - GET /ops/llm-mode/go-gate computes bootstrap CI + decision
  - X-Ops-Token auth gating
  - region filter propagates
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
async def app_db():
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

    seed = session_maker()
    try:
        yield seed, _override_get_db
    finally:
        await seed.rollback()
        await seed.close()
        await engine.dispose()


@pytest_asyncio.fixture
async def client_factory(app_db):
    _seed, override_get_db = app_db

    async def _build():
        app = FastAPI()
        app.include_router(ops_router, prefix="/api/v1")
        app.dependency_overrides[get_db] = override_get_db
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test")
    yield _build


async def _seed_alphas(seed, items):
    """items: list of (region, llm_mode, template_id, is_pass, sharpe)"""
    from backend.models import Alpha
    for region, mode, tmpl_id, is_pass, sharpe in items:
        meta = {}
        if mode is not None:
            meta["llm_mode_used"] = mode
        if tmpl_id is not None:
            meta["assistant_template_id"] = tmpl_id
            meta["assistant_template_fallthrough"] = False
        elif mode == "assistant":
            meta["assistant_template_fallthrough"] = True
        seed.add(Alpha(
            expression=f"x_{id(meta)}",
            region=region,
            universe="TOP3000",
            quality_status=("PASS" if is_pass else "FAIL"),
            is_sharpe=sharpe,
            metrics=meta,
        ))
    await seed.commit()


# ---------------------------------------------------------------------------
# /ops/llm-mode/comparison
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_comparison_returns_stratified_buckets(app_db, client_factory):
    seed, _ = app_db
    # 3 author USA (2 PASS, 1 FAIL); 2 assistant USA via momentum template (both PASS);
    # 1 assistant CHN with fallthrough
    await _seed_alphas(seed, [
        ("USA", "author", None, True, 1.5),
        ("USA", "author", None, True, 2.0),
        ("USA", "author", None, False, 0.5),
        ("USA", "assistant", "momentum.basic_ts_zscore", True, 1.8),
        ("USA", "assistant", "momentum.basic_ts_zscore", True, 1.9),
        ("CHN", "assistant", None, True, 1.2),
    ])
    client = await client_factory()
    async with client as ac:
        r = await ac.get("/api/v1/ops/llm-mode/comparison")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_alphas"] == 6
    assert body["by_mode"]["author"]["total"] == 3
    assert body["by_mode"]["author"]["pass"] == 2
    assert body["by_mode"]["assistant"]["total"] == 3
    assert body["by_mode"]["assistant"]["pass"] == 3
    assert body["by_template"]["momentum.basic_ts_zscore"]["total"] == 2
    assert body["assistant_fallthrough_count"] == 1
    assert "USA" in body["by_region_mode"]
    assert body["by_region_mode"]["USA"]["assistant"]["total"] == 2


@pytest.mark.asyncio
async def test_comparison_region_filter(app_db, client_factory):
    seed, _ = app_db
    await _seed_alphas(seed, [
        ("USA", "author", None, True, 1.5),
        ("CHN", "author", None, True, 1.2),
    ])
    client = await client_factory()
    async with client as ac:
        r = await ac.get("/api/v1/ops/llm-mode/comparison?region=USA")
    body = r.json()
    assert body["total_alphas"] == 1
    assert body["region_filter"] == "USA"


@pytest.mark.asyncio
async def test_comparison_requires_ops_token(app_db, client_factory):
    """OPS_API_TOKEN set → 401 without X-Ops-Token."""
    os.environ["OPS_API_TOKEN"] = "secret_a14"
    try:
        client = await client_factory()
        async with client as ac:
            r = await ac.get("/api/v1/ops/llm-mode/comparison")
            assert r.status_code == 401
    finally:
        os.environ.pop("OPS_API_TOKEN", None)


# ---------------------------------------------------------------------------
# /ops/llm-mode/go-gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_go_gate_no_go_when_assistant_underperforms(app_db, client_factory):
    seed, _ = app_db
    # 500 author USA (20% PASS) vs 500 assistant USA (2% PASS)
    items = []
    for i in range(500):
        items.append(("USA", "author", None, i < 100, 1.0))   # 100 PASS / 500
    for i in range(500):
        items.append(("USA", "assistant", "momentum.basic_ts_zscore", i < 10, 1.0))
    await _seed_alphas(seed, items)
    client = await client_factory()
    async with client as ac:
        r = await ac.get("/api/v1/ops/llm-mode/go-gate?seed=42")
    body = r.json()
    assert body["decision"] == "NO-GO"
    assert body["stats"]["author_rate"] == pytest.approx(0.20)
    assert body["stats"]["assistant_rate"] == pytest.approx(0.02)


@pytest.mark.asyncio
async def test_go_gate_go_when_assistant_clearly_better(app_db, client_factory):
    seed, _ = app_db
    items = []
    for i in range(500):
        items.append(("USA", "author", None, i < 25, 1.0))   # 5%
    for i in range(500):
        items.append(("USA", "assistant", "momentum.basic_ts_zscore", i < 100, 1.0))  # 20%
    await _seed_alphas(seed, items)
    client = await client_factory()
    async with client as ac:
        r = await ac.get("/api/v1/ops/llm-mode/go-gate?seed=42")
    body = r.json()
    assert body["decision"] == "GO"


@pytest.mark.asyncio
async def test_go_gate_insufficient_when_no_assistant_data(app_db, client_factory):
    seed, _ = app_db
    # Only author alphas
    items = [("USA", "author", None, True, 1.0) for _ in range(50)]
    await _seed_alphas(seed, items)
    client = await client_factory()
    async with client as ac:
        r = await ac.get("/api/v1/ops/llm-mode/go-gate")
    body = r.json()
    assert body["decision"] == "INSUFFICIENT"


@pytest.mark.asyncio
async def test_go_gate_requires_ops_token(app_db, client_factory):
    os.environ["OPS_API_TOKEN"] = "secret_a14b"
    try:
        client = await client_factory()
        async with client as ac:
            r = await ac.get("/api/v1/ops/llm-mode/go-gate")
            assert r.status_code == 401
    finally:
        os.environ.pop("OPS_API_TOKEN", None)
