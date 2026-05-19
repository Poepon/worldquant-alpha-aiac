"""Phase 4 Sprint 1 A3 — flat-F4 cross-region quota integration tests.

End-to-end via FastAPI ASGI transport:
  - POST /ops/start-flat-session rejects with 400 when ENFORCE=True and
    new region would exceed quota
  - POST allows with warn log when ENFORCE=False (default)
  - Flag QUOTA empty → quota guard skipped (byte-equivalent legacy)
  - GET /ops/flat-region/distribution returns per-region status chips
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

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
    """Spin up an in-memory aiosqlite engine + session, yield both so the
    test can seed MiningTask rows AND the app can read them via get_db
    override."""
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


async def _seed_tasks(seed_session, *, region: str, count: int, status: str = "RUNNING"):
    from backend.models import MiningTask
    for i in range(count):
        seed_session.add(MiningTask(
            task_name=f"t_{region}_{i}",
            region=region,
            universe="TOP3000",
            status=status,
            config={},
        ))
    await seed_session.commit()


# ---------------------------------------------------------------------------
# POST /start-flat-session quota guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_rejects_when_enforce_and_over_quota(
    app_db_session, client_factory,
):
    """ENFORCE=True + 4 USA active tasks (40%) → POST USA returns 400.
    Quota is 30%, projected (5/11=45%) exceeds → reject."""
    seed, _ = app_db_session
    # Seed: 4 USA + 6 CHN → adding USA would project 5/11=45% > 30% USA cap
    await _seed_tasks(seed, region="USA", count=4)
    await _seed_tasks(seed, region="CHN", count=6)

    with (
        patch("backend.config.settings.ENABLE_FLAT_CONTINUOUS", True),
        patch("backend.config.settings.FLAT_CROSS_REGION_ENFORCE", True),
        patch("backend.config.settings.FLAT_CROSS_REGION_QUOTA",
              {"USA": 0.30, "CHN": 0.50}),
        # Don't actually start a Celery task in this test
        patch(
            "backend.services.task_service.TaskService.start_flat_session",
            new=AsyncMock(return_value=MagicMock(
                task_id=999, region="USA", universe="TOP3000", status="RUNNING",
            )),
        ),
    ):
        client = await client_factory()
        async with client as ac:
            r = await ac.post(
                "/api/v1/ops/start-flat-session",
                json={"region": "USA", "universe": "TOP3000", "datasets": []},
            )
    assert r.status_code == 400, r.text
    assert "flat-F4 quota" in r.json()["detail"]
    assert "USA" in r.json()["detail"]


@pytest.mark.asyncio
async def test_post_warns_only_when_enforce_off(
    app_db_session, client_factory,
):
    """ENFORCE=False + same over-quota state → POST allowed (200) but warn
    log emitted."""
    seed, _ = app_db_session
    await _seed_tasks(seed, region="USA", count=4)
    await _seed_tasks(seed, region="CHN", count=6)

    with (
        patch("backend.config.settings.ENABLE_FLAT_CONTINUOUS", True),
        patch("backend.config.settings.FLAT_CROSS_REGION_ENFORCE", False),
        patch("backend.config.settings.FLAT_CROSS_REGION_QUOTA",
              {"USA": 0.30, "CHN": 0.50}),
        patch(
            "backend.services.task_service.TaskService.start_flat_session",
            new=AsyncMock(return_value=MagicMock(
                task_id=42, region="USA", universe="TOP3000", status="RUNNING",
            )),
        ),
    ):
        client = await client_factory()
        async with client as ac:
            r = await ac.post(
                "/api/v1/ops/start-flat-session",
                json={"region": "USA", "universe": "TOP3000", "datasets": []},
            )
    # Allowed despite would_exceed=True
    assert r.status_code == 200, r.text
    assert r.json()["task_id"] == 42


@pytest.mark.asyncio
async def test_post_allows_when_under_quota(
    app_db_session, client_factory,
):
    """ENFORCE=True + USA count is small → projected_share within cap → allowed."""
    seed, _ = app_db_session
    await _seed_tasks(seed, region="USA", count=1)  # 1/X = small
    await _seed_tasks(seed, region="CHN", count=9)

    with (
        patch("backend.config.settings.ENABLE_FLAT_CONTINUOUS", True),
        patch("backend.config.settings.FLAT_CROSS_REGION_ENFORCE", True),
        patch("backend.config.settings.FLAT_CROSS_REGION_QUOTA",
              {"USA": 0.30, "CHN": 0.95}),  # USA new share = 2/11=18% < 30%
        patch(
            "backend.services.task_service.TaskService.start_flat_session",
            new=AsyncMock(return_value=MagicMock(
                task_id=1, region="USA", universe="TOP3000", status="RUNNING",
            )),
        ),
    ):
        client = await client_factory()
        async with client as ac:
            r = await ac.post(
                "/api/v1/ops/start-flat-session",
                json={"region": "USA", "universe": "TOP3000", "datasets": []},
            )
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_post_empty_quota_skips_guard(
    app_db_session, client_factory,
):
    """QUOTA={} → guard skipped (byte-equivalent legacy)."""
    seed, _ = app_db_session
    await _seed_tasks(seed, region="USA", count=100)  # heavy USA, would normally exceed any cap

    with (
        patch("backend.config.settings.ENABLE_FLAT_CONTINUOUS", True),
        patch("backend.config.settings.FLAT_CROSS_REGION_ENFORCE", True),
        patch("backend.config.settings.FLAT_CROSS_REGION_QUOTA", {}),
        patch(
            "backend.services.task_service.TaskService.start_flat_session",
            new=AsyncMock(return_value=MagicMock(
                task_id=1, region="USA", universe="TOP3000", status="RUNNING",
            )),
        ),
    ):
        client = await client_factory()
        async with client as ac:
            r = await ac.post(
                "/api/v1/ops/start-flat-session",
                json={"region": "USA", "universe": "TOP3000", "datasets": []},
            )
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# GET /flat-region/distribution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_distribution_endpoint_returns_status_chips(
    app_db_session, client_factory,
):
    """Mixed regions seeded → endpoint returns per-region chips with status."""
    seed, _ = app_db_session
    await _seed_tasks(seed, region="USA", count=5)   # 5/10=50% > 30% → exceeded
    await _seed_tasks(seed, region="CHN", count=3)   # 30%
    await _seed_tasks(seed, region="JPN", count=2)   # 20%

    with (
        patch("backend.config.settings.FLAT_CROSS_REGION_ENFORCE", False),
        patch("backend.config.settings.FLAT_CROSS_REGION_QUOTA",
              {"USA": 0.30, "CHN": 0.40, "JPN": 0.25}),
        patch("backend.config.settings.FLAT_CROSS_REGION_LOOKBACK_DAYS", 30),
    ):
        client = await client_factory()
        async with client as ac:
            r = await ac.get("/api/v1/ops/flat-region/distribution")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_active_tasks"] == 10
    assert body["enforce"] is False
    by_region = {row["region"]: row for row in body["regions"]}
    assert by_region["USA"]["status"] == "exceeded"
    assert by_region["USA"]["count"] == 5
    assert by_region["JPN"]["count"] == 2
