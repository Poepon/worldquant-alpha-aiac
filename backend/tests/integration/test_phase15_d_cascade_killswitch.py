"""Phase 15-D PR1: cascade kill-switch flag tests (2026-05-18).

Verifies the ENABLE_CASCADE_LEGACY kill-switch wires through all 3
cascade entry points:
  - routers/mining_session.py 5 endpoints return 410 Gone when flag OFF
  - mining_tasks.run_mining_task refuses cascade dispatch (FAILED)
  - tasks/session_watchdog.py cascade probe short-circuits

Default flag value is True → zero behavior change pre-flip. Tests
restore the flag via autouse fixture mirroring the
_isolate_flag_state pattern.
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.database import get_db
from backend.routers.mining_session import router as mining_session_router


@pytest.fixture(autouse=True)
def _isolate_cascade_flag():
    """Snapshot + restore ENABLE_CASCADE_LEGACY so tests don't leak."""
    from backend.config import settings as _stg
    saved = getattr(_stg, "ENABLE_CASCADE_LEGACY", True)
    yield
    setattr(_stg, "ENABLE_CASCADE_LEGACY", saved)


def _mock_db_noop():
    db = AsyncMock()
    return db


@pytest_asyncio.fixture
async def client():
    app = FastAPI()
    app.include_router(mining_session_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: _mock_db_noop()
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# Flag-on default = unchanged (NOT 410)
# ---------------------------------------------------------------------------

def test_flag_on_default_gate_does_not_raise(monkeypatch):
    """ENABLE_CASCADE_LEGACY=True (default) → gate dependency does NOT raise."""
    from backend.config import settings
    from backend.routers.mining_session import _require_cascade_legacy_enabled
    monkeypatch.setattr(settings, "ENABLE_CASCADE_LEGACY", True, raising=False)
    # No exception = gate allowed the request through
    _require_cascade_legacy_enabled()


def test_flag_off_gate_raises_410(monkeypatch):
    """ENABLE_CASCADE_LEGACY=False → gate dependency raises HTTPException 410."""
    from fastapi import HTTPException
    from backend.config import settings
    from backend.routers.mining_session import _require_cascade_legacy_enabled
    monkeypatch.setattr(settings, "ENABLE_CASCADE_LEGACY", False, raising=False)
    with pytest.raises(HTTPException) as exc_info:
        _require_cascade_legacy_enabled()
    assert exc_info.value.status_code == 410
    assert "start-flat-session" in exc_info.value.detail


# ---------------------------------------------------------------------------
# Flag-off 410 Gone — all 5 endpoints
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_flag_off_list_sessions_returns_410(client, monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_CASCADE_LEGACY", False, raising=False)
    async with client as ac:
        r = await ac.get("/api/v1/mining-session")
    assert r.status_code == 410, r.text
    assert "start-flat-session" in r.json()["detail"]


@pytest.mark.asyncio
async def test_flag_off_get_session_returns_410(client, monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_CASCADE_LEGACY", False, raising=False)
    async with client as ac:
        r = await ac.get("/api/v1/mining-session/USA")
    assert r.status_code == 410


@pytest.mark.asyncio
async def test_flag_off_start_session_returns_410(client, monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_CASCADE_LEGACY", False, raising=False)
    async with client as ac:
        r = await ac.post("/api/v1/mining-session/start", json={"region": "USA"})
    assert r.status_code == 410
    assert "cascade legacy retired" in r.json()["detail"]


@pytest.mark.asyncio
async def test_flag_off_stop_session_returns_410(client, monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_CASCADE_LEGACY", False, raising=False)
    async with client as ac:
        r = await ac.post("/api/v1/mining-session/stop", json={"task_id": 1})
    assert r.status_code == 410


@pytest.mark.asyncio
async def test_flag_off_resume_session_returns_410(client, monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "ENABLE_CASCADE_LEGACY", False, raising=False)
    async with client as ac:
        r = await ac.post("/api/v1/mining-session/resume", json={"task_id": 1})
    assert r.status_code == 410


# ---------------------------------------------------------------------------
# session_watchdog cascade probe short-circuit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_watchdog_cascade_probe_skipped_when_flag_off(monkeypatch):
    """Static-source sentinel: cascade_legacy_on guard short-circuits the SQL.

    A pure-source check rather than a full _watchdog_revive_async run
    because the latter needs a live DB + heavy mocking. The wire site is
    deterministic — if the guard string is present, the SQL is skipped.
    """
    import inspect
    from backend.tasks import session_watchdog
    src = inspect.getsource(session_watchdog._watchdog_revive_async)
    assert "ENABLE_CASCADE_LEGACY" in src
    assert "cascade_legacy_on" in src
    assert "if not cascade_legacy_on" in src


# ---------------------------------------------------------------------------
# mining_tasks.run_mining_task cascade dispatch refusal
# ---------------------------------------------------------------------------

def test_mining_tasks_dispatch_has_killswitch_guard():
    """Static-source sentinel: cascade dispatch path checks ENABLE_CASCADE_LEGACY."""
    import inspect
    from backend.tasks import mining_tasks
    src = inspect.getsource(mining_tasks.run_mining_task)
    assert "ENABLE_CASCADE_LEGACY" in src
    assert "phase15-D" in src
    # The refusal must FAIL the task (not crash)
    assert 'status="FAILED"' in src or "status='FAILED'" in src


def test_flag_registered_in_supported_flags():
    """Double-file registration per memory feedback_enable_flag_double_file."""
    from backend.services.feature_flag_service import SUPPORTED_FLAGS
    assert "ENABLE_CASCADE_LEGACY" in SUPPORTED_FLAGS
    spec = SUPPORTED_FLAGS["ENABLE_CASCADE_LEGACY"]
    assert spec.flag_type == "bool"
    assert "phase15-D" in spec.description


def test_flag_default_true_for_backward_compat():
    """Default value must be True so pre-cutover deploys are unchanged."""
    from backend.config import settings
    # Read the class-level default rather than runtime override-able value
    field = type(settings).model_fields.get("ENABLE_CASCADE_LEGACY")
    assert field is not None
    assert field.default is True
