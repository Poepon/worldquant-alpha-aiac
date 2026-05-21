"""Tier A-F 3-round review follow-up verification tests (2026-05-20).

0 MUST found across R1/R2/R3; these pin the SHOULD/NICE fixes:
  - bandit cron watermark idempotency (re-run does NOT double-count)
  - build_factor_returns_snapshot symmetric scale warning (too-small)
  - _DistillLLMShim price table prefix (startswith, not substring)
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# bandit cron watermark idempotency (R1+R2 top SHOULD)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bandit_cron_rerun_does_not_double_count(monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from sqlalchemy import select
    from backend.database import SQLAlchemyBase
    from backend.models import Alpha
    from backend.models.cognitive_layer_bandit import CognitiveLayerBanditState

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLAlchemyBase.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        for i in range(4):
            s.add(Alpha(alpha_id=f"m{i}", expression="x", region="USA",
                        universe="TOP3000", quality_status="PASS",
                        metrics={"_cognitive_layer_used": "macro_top_down"}))
        await s.commit()
    monkeypatch.setattr("backend.database.AsyncSessionLocal", maker, raising=False)

    import backend.tasks.cognitive_layer_bandit_tasks as mod
    r1 = await mod._update_async(window_days=30)
    r2 = await mod._update_async(window_days=30)  # immediate re-run

    assert r1["updated_layers"] == 1
    # Re-run: watermark advanced past all existing alphas → empty window
    assert r2["updated_layers"] == 0

    async with maker() as s:
        row = (await s.execute(
            select(CognitiveLayerBanditState).where(
                CognitiveLayerBanditState.layer_id == "macro_top_down"
            )
        )).scalar_one()
    # 4 PASS counted ONCE, not doubled
    assert row.pass_count == 4
    await engine.dispose()


@pytest.mark.asyncio
async def test_bandit_cron_counts_alphas_after_seeded_watermark(monkeypatch):
    """Watermark does NOT freeze counting: an alpha created AFTER a
    pre-seeded watermark IS counted. Uses explicit created_at + a
    pre-seeded SystemConfig watermark to avoid SQLite's 1-second
    func.now() resolution (a sub-second run1→run2 window is
    unrepresentable on SQLite; production Postgres has µs precision)."""
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from sqlalchemy import select
    from backend.database import SQLAlchemyBase
    from backend.models import Alpha, SystemConfig
    from backend.models.cognitive_layer_bandit import CognitiveLayerBanditState

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLAlchemyBase.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        # Pre-seed watermark to a clearly-past edge
        s.add(SystemConfig(
            config_key="cognitive_layer_bandit_watermark",
            config_value="2020-01-01T00:00:00",
            config_type="timestamp",
        ))
        # Alpha created well after the watermark, before now
        s.add(Alpha(alpha_id="m1", expression="x", region="USA",
                    universe="TOP3000", quality_status="PASS",
                    created_at=datetime(2024, 6, 1, 12, 0, 0),
                    metrics={"_cognitive_layer_used": "macro_top_down"}))
        await s.commit()
    monkeypatch.setattr("backend.database.AsyncSessionLocal", maker, raising=False)

    import backend.tasks.cognitive_layer_bandit_tasks as mod
    r = await mod._update_async(window_days=30)
    assert r["updated_layers"] == 1  # the 2024 alpha is past the 2020 watermark

    async with maker() as s:
        row = (await s.execute(
            select(CognitiveLayerBanditState).where(
                CognitiveLayerBanditState.layer_id == "macro_top_down"
            )
        )).scalar_one()
    assert row.pass_count == 1
    await engine.dispose()


# ---------------------------------------------------------------------------
# build_factor_returns_snapshot symmetric scale warning (R2 SHOULD)
# ---------------------------------------------------------------------------

def _write_factor_csv(path: Path, scale_divisor: float, n: int = 520) -> None:
    """Write FF-style CSV with values ~±0.5%/scale_divisor."""
    dates = pd.date_range("2022-01-01", periods=n, freq="B")
    rng = np.random.default_rng(0)
    pd.DataFrame({
        "Date": dates.strftime("%Y-%m-%d"),
        "SMB": rng.normal(0, 0.5, n) / scale_divisor,
        "HML": rng.normal(0, 0.5, n) / scale_divisor,
        "RMW": rng.normal(0, 0.5, n) / scale_divisor,
        "Mom": rng.normal(0, 0.5, n) / scale_divisor,
        "BAB": rng.normal(0, 0.5, n) / scale_divisor,
    }).to_csv(path, index=False)


def test_factor_builder_warns_on_too_small(tmp_path, monkeypatch, caplog):
    """Already-decimal data (values ~0.005) double-scaled by 0.01 → ~5e-5 →
    too-small warning fires."""
    import logging
    from scripts import build_factor_returns_snapshot as b
    # Source already decimal (divide by 100 makes ~0.005 values)
    csv = tmp_path / "decimal.csv"
    _write_factor_csv(csv, scale_divisor=100.0)  # values ~0.005
    monkeypatch.setattr(b, "_OUT_DIR", tmp_path / "snap")
    with caplog.at_level(logging.WARNING):
        rc = b.build(
            region="USA", csv_path=csv,
            column_map=dict(b._PRESETS["fama_french"]["map"]),
            scale=0.01,  # WRONG — double-scales already-decimal data
            date_col="Date", strict=False, dry_run=False,
        )
    assert rc == 0
    assert any("too SMALL" in r.message for r in caplog.records)


def test_factor_builder_warns_on_too_big(tmp_path, monkeypatch, caplog):
    """Percent data NOT rescaled (--scale 1.0) → values ~0.5 → too-big warning."""
    import logging
    from scripts import build_factor_returns_snapshot as b
    csv = tmp_path / "percent.csv"
    _write_factor_csv(csv, scale_divisor=1.0)  # values ~0.5 (percent)
    monkeypatch.setattr(b, "_OUT_DIR", tmp_path / "snap")
    with caplog.at_level(logging.WARNING):
        b.build(
            region="USA", csv_path=csv,
            column_map=dict(b._PRESETS["fama_french"]["map"]),
            scale=1.0,  # WRONG — percent not rescaled
            date_col="Date", strict=False, dry_run=False,
        )
    assert any("not rescaled" in r.message for r in caplog.records)


def test_factor_builder_no_warn_correct_scale(tmp_path, monkeypatch, caplog):
    """Percent data correctly rescaled by 0.01 → values ~0.005 → no scale warn."""
    import logging
    from scripts import build_factor_returns_snapshot as b
    csv = tmp_path / "percent.csv"
    _write_factor_csv(csv, scale_divisor=1.0)  # ~0.5 in percent
    monkeypatch.setattr(b, "_OUT_DIR", tmp_path / "snap")
    with caplog.at_level(logging.WARNING):
        b.build(
            region="USA", csv_path=csv,
            column_map=dict(b._PRESETS["fama_french"]["map"]),
            scale=0.01,  # correct → ~0.005
            date_col="Date", strict=False, dry_run=False,
        )
    msgs = " ".join(r.message for r in caplog.records)
    assert "too SMALL" not in msgs and "not rescaled" not in msgs


# ---------------------------------------------------------------------------
# price table startswith (R1 NICE)
# ---------------------------------------------------------------------------

def test_price_table_startswith_not_substring():
    from backend.tasks.logic_distill_tasks import _DistillLLMShim
    shim = _DistillLLMShim(MagicMock())
    # exact prefix matches
    assert shim._rate_for_model("deepseek-chat") == 0.0014
    assert shim._rate_for_model("claude-opus-4-7") == 0.045
    assert shim._rate_for_model("gpt-4o-mini") == 0.0004
    # a model that merely CONTAINS 'gpt' but doesn't start with it → default
    # (substring match would have wrongly resolved it)
    assert shim._rate_for_model("custom-gpt-proxy") == shim._DEFAULT_COST_PER_1K
    # unknown → default
    assert shim._rate_for_model("mystery-model") == shim._DEFAULT_COST_PER_1K
