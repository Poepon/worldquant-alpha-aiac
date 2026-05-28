"""Submit-backlog drain — /ops/submit-backlog + /ops/submit-backlog/scan
integration tests (2026-05-28).

GET mocks AsyncSession.execute returning 2 results in order:
  1. queue rows (.all()) — BacklogItem column order
  2. summary aggregate (.one()) — (total, submit, neutral, skip, unknown, pending)

POST doesn't depend on get_db; it reads settings.iqc_audit_scope() and (when a
scope is set) calls _iqc_audit_backfill_sweep_async. Tests monkeypatch both.

NB (per reference_jsonb_null_footgun_ops_mock_gap): these mocks DON'T exercise
the raw jsonb SQL — that path is verified against live PG. These cover response
assembly + the scope-unset branch only.
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


def _mock_db(*, rows, summary):
    def _all_result(r):
        m = MagicMock()
        m.all = MagicMock(return_value=list(r))
        return m

    def _one_result(t):
        m = MagicMock()
        m.one = MagicMock(return_value=t)
        return m

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_all_result(rows), _one_result(summary)])
    return db


@pytest_asyncio.fixture
async def client_factory():
    async def _build(*, rows, summary):
        app = FastAPI()
        app.include_router(ops_router, prefix="/api/v1")
        app.dependency_overrides[get_db] = lambda: _mock_db(rows=rows, summary=summary)
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test")

    return _build


# BacklogItem column order:
# id, alpha_id, region, universe, is_sharpe, is_fitness, is_turnover, is_margin,
# verdict, composite, margin_bps, scope, audited_at, stale, pending
_ROW_SUBMIT = (
    14841, "AbC123", "USA", "TOP3000", 1.32, 1.10, 0.25, 0.012,
    "SUBMIT", 0.941, 120.12, "competitions/IQC2026S2", "2026-05-20T09:55:56Z",
    False, False,
)
_ROW_NEUTRAL = (
    14790, "XyZ999", "USA", "TOP3000", 1.82, 0.90, 0.30, 0.0006,
    "NEUTRAL", -0.154, 6.0, "competitions/IQC2026S2", "2026-05-20T09:55:56Z",
    False, False,
)
_ROW_PENDING = (
    15000, "Pen001", "USA", "TOP3000", 1.50, 1.00, 0.20, 0.001,
    None, None, None, None, None, False, True,
)


@pytest.mark.asyncio
async def test_submit_backlog_assembles_and_flags_pending(client_factory):
    """Queue rows pass through with verdict/composite; summary derives audited."""
    summary = (3, 1, 1, 0, 0, 1)  # total, submit, neutral, skip, unknown, pending
    client = await client_factory(
        rows=[_ROW_SUBMIT, _ROW_NEUTRAL, _ROW_PENDING], summary=summary,
    )
    async with client as ac:
        r = await ac.get("/api/v1/ops/submit-backlog")
    assert r.status_code == 200, r.text
    body = r.json()
    # scope comes from real settings.iqc_audit_scope() (default IQC2026S2)
    assert body["scope"] == "competitions/IQC2026S2"
    assert body["summary"]["total"] == 3
    assert body["summary"]["pending"] == 1
    assert body["summary"]["audited"] == 2  # total - pending
    items = body["items"]
    assert len(items) == 3
    assert items[0]["verdict"] == "SUBMIT"
    assert items[0]["composite"] == 0.941
    assert items[0]["margin_bps"] == 120.12
    # pending row surfaces with no verdict + pending flag
    pend = next(it for it in items if it["alpha_pk"] == 15000)
    assert pend["pending"] is True
    assert pend["verdict"] is None


@pytest.mark.asyncio
async def test_submit_backlog_empty(client_factory):
    client = await client_factory(rows=[], summary=(0, 0, 0, 0, 0, 0))
    async with client as ac:
        r = await ac.get("/api/v1/ops/submit-backlog")
    body = r.json()
    assert body["summary"]["total"] == 0
    assert body["summary"]["audited"] == 0
    assert body["items"] == []


@pytest.mark.asyncio
async def test_scan_scope_unset_returns_message(monkeypatch):
    """Both competition + team empty → scan refuses with a message, enqueued 0."""
    from backend.config import settings as _stg
    monkeypatch.setattr(_stg, "IQC_AUTO_AUDIT_COMPETITION", "", raising=False)
    monkeypatch.setattr(_stg, "IQC_AUTO_AUDIT_TEAM", "", raising=False)

    app = FastAPI()
    app.include_router(ops_router, prefix="/api/v1")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/api/v1/ops/submit-backlog/scan?limit=200")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enqueued"] == 0
    assert body["scanned_limit"] == 200
    assert "scope" in (body["message"] or "").lower() or "未配置" in (body["message"] or "")


@pytest.mark.asyncio
async def test_scan_scope_set_invokes_sweep(monkeypatch):
    """Scope set → calls the sweep with the limit, passes enqueued count through."""
    from backend.config import settings as _stg
    monkeypatch.setattr(_stg, "IQC_AUTO_AUDIT_COMPETITION", "IQC2026S2", raising=False)

    captured = {}

    async def _fake_sweep(limit=None):
        captured["limit"] = limit
        return {"enqueued": 7, "skipped_inflight": 2}

    import backend.tasks.refresh_tasks as _rt
    monkeypatch.setattr(_rt, "_iqc_audit_backfill_sweep_async", _fake_sweep, raising=True)

    app = FastAPI()
    app.include_router(ops_router, prefix="/api/v1")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/api/v1/ops/submit-backlog/scan?limit=150")
    assert r.status_code == 200, r.text
    body = r.json()
    assert captured["limit"] == 150
    assert body["enqueued"] == 7
    assert body["skipped_inflight"] == 2
    assert body["scope"] == "competitions/IQC2026S2"
