"""P2-C run_regime_infer task integration tests (2026-05-16).

PG-only via S5 ``_pg_reachable`` + module-level pytestmark.

Covers T1..T2:
    T1 task emits archive: 3-day fixture → docs/regime_state/<sh-date>.json
       contains a per-region snapshot for USA with regime label.
    T2 flag-off: ENABLE_REGIME_INFERENCE=False → status='skipped', no write.
"""
from __future__ import annotations

import json
import os
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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

# Warm-up
import backend.tasks  # noqa: E402, F401

from backend.tasks.regime_infer import _run_async  # noqa: E402


SH_TZ = timezone(timedelta(hours=8))


def _write_health(dir_: Path, sh_date: str, region: str, by_band: dict,
                  checked: int = 100):
    blob = {
        "report_date": sh_date,
        "totals": {"checked": checked, "by_band": by_band},
        "regions": {region: {"checked": checked, "by_band": by_band}},
    }
    (dir_ / f"{sh_date}.json").write_text(
        json.dumps(blob, indent=2), encoding="utf-8"
    )


class TestRegimeInferTask:

    @pytest.mark.asyncio
    async def test_task_emits_archive(self, tmp_path, monkeypatch):
        """T1: flag=True + 3-day health fixture → archive written with USA
        regime snapshot."""
        # Stub Redis so writes don't depend on a live broker
        store: dict = {}
        cli = MagicMock()

        def _setex(k, ttl, v):
            store[k] = (ttl, v)
            return True
        cli.setex = MagicMock(side_effect=_setex)
        cli.get = MagicMock(side_effect=lambda k: (store.get(k) or (None, None))[1])

        # Stub _HEALTH_DIR to our fixture dir
        fake_health = tmp_path / "alpha_health_check"
        fake_health.mkdir(parents=True, exist_ok=True)
        sh_today = datetime.now(timezone.utc).astimezone(SH_TZ).date()
        for back in range(3):
            day = sh_today - timedelta(days=back)
            _write_health(
                fake_health, day.isoformat(), "USA",
                {"GREEN": 5, "YELLOW": 10, "ORANGE": 30,
                 "RED": 30, "CRITICAL": 25},
            )

        fake_output = tmp_path / "regime_state"
        monkeypatch.setattr(
            "backend.services.regime_inference_service._HEALTH_DIR",
            fake_health,
        )
        monkeypatch.setattr(
            "backend.tasks.regime_infer._OUTPUT_DIR",
            fake_output,
        )

        from backend.config import settings
        original = settings.ENABLE_REGIME_INFERENCE
        settings.ENABLE_REGIME_INFERENCE = True
        try:
            with patch(
                "backend.tasks.redis_pool.get_redis_client",
                return_value=cli,
            ):
                result = await _run_async()
        finally:
            settings.ENABLE_REGIME_INFERENCE = original

        assert result["status"] == "ok"
        assert "USA" in result["regions"]
        usa = result["regions"]["USA"]
        assert usa["regime"] in {
            "crisis", "elevated", "normal", "calm", "very_calm",
        }
        # Archive file must exist
        archive_path = Path(result["json_path"])
        assert archive_path.exists(), f"archive not written to {archive_path}"
        archive = json.loads(archive_path.read_text(encoding="utf-8"))
        assert archive["schema_version"] == "p2c.v1"
        assert "USA" in archive["regions"]
        # Redis was called: at least 1 SETEX per region per call
        assert cli.setex.call_count >= 2  # 2 keys × USA at minimum

    @pytest.mark.asyncio
    async def test_task_disabled_flag(self, tmp_path, monkeypatch):
        """T2: flag=False → status='skipped' without any write."""
        fake_output = tmp_path / "regime_state"
        monkeypatch.setattr(
            "backend.tasks.regime_infer._OUTPUT_DIR",
            fake_output,
        )

        from backend.config import settings
        original = settings.ENABLE_REGIME_INFERENCE
        settings.ENABLE_REGIME_INFERENCE = False
        try:
            result = await _run_async()
        finally:
            settings.ENABLE_REGIME_INFERENCE = original

        assert result["status"] == "skipped"
        # No archive emitted
        assert not fake_output.exists() or not any(fake_output.iterdir())
