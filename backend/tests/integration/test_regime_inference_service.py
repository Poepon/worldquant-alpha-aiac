"""P2-C RegimeInferenceService integration tests (2026-05-16).

PG-only via S5 ``_pg_reachable`` + module-level pytestmark.

Covers I1..I4:
    I1 cold-start: empty dir → "normal" + cold_start=True + conf=0.0
    I2 full window: 7 fixture days → smoothed + pass_rate_7d_mean
    I3 get_cached_regime Redis-miss → None (no exception)
    I4 write_regime_state idempotent (two calls same day overwrite cleanly)
"""
from __future__ import annotations

import json
import os
import socket
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("POSTGRES_PORT", "5433")


def _pg_reachable() -> bool:
    host = os.getenv("POSTGRES_HOST", "127.0.0.1")
    port = int(os.getenv("POSTGRES_PORT", "5433"))
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_reachable(),
    reason="P2-C regime tests require Postgres reachable",
)


# Warm-up: break circular import (mining_tasks ↔ agents).
import backend.tasks  # noqa: E402, F401

from backend.services.regime_inference_service import (  # noqa: E402
    RegimeInferenceService,
    _HEALTH_DIR,
)
from datetime import datetime, timedelta, timezone  # noqa: E402

SH_TZ = timezone(timedelta(hours=8))


@pytest_asyncio.fixture
async def pg_engine_maker():
    from backend.config import settings
    engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI, echo=False)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield engine, maker
    finally:
        await engine.dispose()


@pytest.fixture
def isolated_health_dir(tmp_path, monkeypatch):
    """Redirect _HEALTH_DIR to a temp dir so tests don't read real history."""
    fake_dir = tmp_path / "alpha_health_check"
    fake_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "backend.services.regime_inference_service._HEALTH_DIR",
        fake_dir,
    )
    return fake_dir


def _write_health(
    dir_: Path,
    *,
    sh_date: str,
    region: str = "USA",
    by_band: dict | None = None,
    checked: int = 100,
    use_totals_only: bool = False,
):
    """Helper: write a synthetic alpha_health JSON for a given day."""
    bb = by_band or {"GREEN": 5, "YELLOW": 10, "ORANGE": 30, "RED": 30, "CRITICAL": 25}
    blob = {
        "report_date": sh_date,
        "totals": {"checked": checked, "by_band": bb},
    }
    if not use_totals_only:
        blob["regions"] = {region: {"checked": checked, "by_band": bb}}
    (dir_ / f"{sh_date}.json").write_text(
        json.dumps(blob, indent=2), encoding="utf-8"
    )


class TestInferCurrentRegime:

    @pytest.mark.asyncio
    async def test_infer_cold_start_no_files(
        self, pg_engine_maker, isolated_health_dir,
    ):
        """I1: empty health dir → cold_start=True + regime='normal' + conf=0."""
        engine, maker = pg_engine_maker
        async with maker() as db:
            svc = RegimeInferenceService(db)
            snap = await svc.infer_current_regime(region="USA")
        assert snap["regime"] == "normal", (
            f"cold start should yield 'normal', got {snap['regime']}"
        )
        assert snap["cold_start"] is True
        assert snap["confidence"] == 0.0
        assert snap["pass_rate"] is None
        assert snap["pass_rate_7d_mean"] is None
        assert snap["history"] == []
        # MF1 invariant: never includes sharpe_avg_7d
        assert "sharpe_avg_7d" not in snap

    @pytest.mark.asyncio
    async def test_infer_full_window(
        self, pg_engine_maker, isolated_health_dir,
    ):
        """I2: 7 day fixture → smoothed + non-None pass_rate_7d_mean."""
        engine, maker = pg_engine_maker
        sh_today = datetime.now(timezone.utc).astimezone(SH_TZ).date()
        # Seed 7 days with 'normal' pass_rate ≈ 0.15 (15 GREEN/YELLOW of 100)
        for back in range(7):
            day = sh_today - timedelta(days=back)
            _write_health(
                isolated_health_dir, sh_date=day.isoformat(),
                by_band={"GREEN": 5, "YELLOW": 10, "ORANGE": 30,
                         "RED": 30, "CRITICAL": 25},
                checked=100,
            )
        async with maker() as db:
            svc = RegimeInferenceService(db)
            snap = await svc.infer_current_regime(region="USA")
        assert snap["regime"] in {"normal", "elevated", "calm"}, (
            f"pass_rate=0.15 should give normal, got {snap['regime']}"
        )
        assert snap["cold_start"] is False
        assert snap["confidence"] == 1.0
        assert snap["pass_rate"] == pytest.approx(0.15)
        assert snap["pass_rate_7d_mean"] == pytest.approx(0.15)
        assert len(snap["history"]) == 7
        assert "sharpe_avg_7d" not in snap

    @pytest.mark.asyncio
    async def test_infer_crisis_window(
        self, pg_engine_maker, isolated_health_dir,
    ):
        """Stress: 7 days at pass_rate≈0.03 → crisis or elevated regime."""
        engine, maker = pg_engine_maker
        sh_today = datetime.now(timezone.utc).astimezone(SH_TZ).date()
        for back in range(7):
            day = sh_today - timedelta(days=back)
            _write_health(
                isolated_health_dir, sh_date=day.isoformat(),
                by_band={"GREEN": 1, "YELLOW": 2, "ORANGE": 50,
                         "RED": 30, "CRITICAL": 17},
                checked=100,
            )
        async with maker() as db:
            svc = RegimeInferenceService(db)
            snap = await svc.infer_current_regime(region="USA")
        assert snap["regime"] == "crisis"
        assert snap["pass_rate"] == pytest.approx(0.03)

    @pytest.mark.asyncio
    async def test_infer_region_fallback_to_totals(
        self, pg_engine_maker, isolated_health_dir,
    ):
        """When ``regions.<R>`` missing, fall back to ``totals.by_band``."""
        engine, maker = pg_engine_maker
        sh_today = datetime.now(timezone.utc).astimezone(SH_TZ).date()
        for back in range(7):
            day = sh_today - timedelta(days=back)
            _write_health(
                isolated_health_dir, sh_date=day.isoformat(),
                use_totals_only=True,
                by_band={"GREEN": 10, "YELLOW": 30, "ORANGE": 30,
                         "RED": 20, "CRITICAL": 10},
                checked=100,
            )
        async with maker() as db:
            svc = RegimeInferenceService(db)
            snap = await svc.infer_current_regime(region="CHN")
        assert snap["cold_start"] is False
        assert snap["pass_rate"] == pytest.approx(0.40)
        assert snap["regime"] == "very_calm"


class TestRedisPath:

    @pytest.mark.asyncio
    async def test_get_cached_regime_redis_miss(self, pg_engine_maker):
        """I3: Redis-down / miss → returns None without raising."""
        engine, maker = pg_engine_maker

        # Simulate redis raising on .get()
        broken_cli = MagicMock()
        broken_cli.get = MagicMock(side_effect=RuntimeError("redis down"))
        with patch(
            "backend.tasks.redis_pool.get_redis_client",
            return_value=broken_cli,
        ):
            async with maker() as db:
                svc = RegimeInferenceService(db)
                got = await svc.get_cached_regime(region="USA")
        assert got is None

    @pytest.mark.asyncio
    async def test_get_cached_regime_miss_returns_none(
        self, pg_engine_maker,
    ):
        """Fresh region (key absent) → None."""
        engine, maker = pg_engine_maker
        store: dict = {}
        cli = MagicMock()
        cli.get = MagicMock(side_effect=lambda k: store.get(k))
        with patch(
            "backend.tasks.redis_pool.get_redis_client",
            return_value=cli,
        ):
            async with maker() as db:
                svc = RegimeInferenceService(db)
                got = await svc.get_cached_regime(
                    region=f"FAKE_{uuid.uuid4().hex[:6]}",
                )
        assert got is None

    @pytest.mark.asyncio
    async def test_write_regime_state_idempotent(self, pg_engine_maker):
        """I4: write twice for same region → 2nd overwrites cleanly (no
        leaked TTL / no exception). Verify Redis SETEX is called twice."""
        engine, maker = pg_engine_maker
        store: dict = {}
        cli = MagicMock()

        def _setex(k, ttl, v):
            store[k] = (ttl, v)
            return True
        cli.setex = MagicMock(side_effect=_setex)
        cli.get = MagicMock(side_effect=lambda k: (store.get(k) or (None, None))[1])

        with patch(
            "backend.tasks.redis_pool.get_redis_client",
            return_value=cli,
        ):
            async with maker() as db:
                svc = RegimeInferenceService(db)
                snap1 = {"regime": "elevated", "history": ["normal", "elevated"]}
                snap2 = {"regime": "calm", "history": ["calm", "calm"]}

                r1 = await svc.write_regime_state(region="USA", snapshot=snap1)
                assert r1["redis_ok"] is True
                assert r1["regime"] == "elevated"
                assert "aiac:current_regime:USA" in store

                r2 = await svc.write_regime_state(region="USA", snapshot=snap2)
                assert r2["redis_ok"] is True
                # Latest write wins
                _, val = store["aiac:current_regime:USA"]
                assert val == "calm"

        # SETEX called 4× total (2 keys × 2 writes)
        assert cli.setex.call_count == 4
