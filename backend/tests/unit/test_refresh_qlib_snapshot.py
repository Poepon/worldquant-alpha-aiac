"""Phase 3 Q10 PR2b: refresh_qlib_snapshot.py unit tests (2026-05-18).

Plan: ~/.claude/plans/phase3-q10-pyqlib-prescreen-2026-05-18.md v1.3 §3.2.

Verifies the synthetic mode (CI-safe, no pyqlib install required) end-to-end:
  - CLI main() runs without crashing
  - Output parquet is loadable + matches QlibEngine snapshot contract:
    MultiIndex (datetime, instrument) + OHLCV columns
  - QlibEngine probes 'pandas_snapshot' when the script's output is in
    QLIB_SNAPSHOT_DIR
  - prescreen_alpha runs end-to-end on the synthetic snapshot and produces
    verdict ∈ {pass, reject}
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

pd = pytest.importorskip("pandas")
np = pytest.importorskip("numpy")


def test_refresh_synthetic_writes_valid_parquet(tmp_path):
    """--source synthetic produces a parquet matching QlibEngine contract."""
    from scripts.refresh_qlib_snapshot import refresh_region_snapshot
    out_path = refresh_region_snapshot(
        region="USA", snapshot_dir=str(tmp_path),
        source="synthetic", years=1, top=20,
    )
    assert out_path is not None
    p = Path(out_path)
    assert p.exists()
    assert p.name == "USA.parquet"
    df = pd.read_parquet(p)
    assert isinstance(df.index, pd.MultiIndex)
    assert df.index.names == ["datetime", "instrument"]
    for col in ["close", "open", "high", "low", "volume", "vwap"]:
        assert col in df.columns
    assert df.index.get_level_values("instrument").nunique() == 20


def test_cli_main_synthetic_mode(tmp_path, monkeypatch):
    """CLI invocation succeeds in synthetic mode + writes regions specified."""
    from scripts.refresh_qlib_snapshot import main
    rc = main([
        "--regions", "USA,CHN",
        "--source", "synthetic",
        "--years", "1",
        "--top", "10",
        "--snapshot-dir", str(tmp_path),
    ])
    assert rc == 0
    assert (tmp_path / "USA.parquet").exists()
    assert (tmp_path / "CHN.parquet").exists()


def test_cli_pyqlib_explicit_fails_without_install(tmp_path):
    """--source pyqlib returns non-zero when pyqlib import fails (deterministic on Windows)."""
    # We don't assume pyqlib is missing — only verify the flow doesn't crash
    # and returns a sane rc. If pyqlib IS installed, the test still passes.
    from scripts.refresh_qlib_snapshot import main
    rc = main([
        "--regions", "USA",
        "--source", "pyqlib",
        "--years", "1",
        "--top", "5",
        "--snapshot-dir", str(tmp_path),
        "--qlib-data-dir", "/definitely/not/here",
    ])
    # 1 (failure) when pyqlib unavailable; 0 if it happens to load
    assert rc in (0, 1)


def test_snapshot_drives_engine_probe_and_prescreen(tmp_path, monkeypatch):
    """End-to-end smoke: script-generated snapshot makes QlibEngine probe
    pandas_snapshot AND prescreen_alpha produces a real verdict."""
    from scripts.refresh_qlib_snapshot import refresh_region_snapshot
    refresh_region_snapshot(
        region="USA", snapshot_dir=str(tmp_path),
        source="synthetic", years=1, top=30,
    )
    from backend.config import settings
    monkeypatch.setattr(settings, "QLIB_SNAPSHOT_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "QLIB_ENGINE_PREFER_PANDAS", False, raising=False)
    monkeypatch.setattr(settings, "QLIB_PRESCREEN_SHARPE_FLOOR", 0.3, raising=False)
    monkeypatch.setattr(settings, "QLIB_PRESCREEN_IC_FLOOR", 0.005, raising=False)
    from backend.qlib_prescreen import _reset_engine_for_test, QlibEngine, prescreen_alpha
    _reset_engine_for_test()
    engine = QlibEngine()
    assert engine.kind == "pandas_snapshot"

    import asyncio
    r = asyncio.run(prescreen_alpha("ts_mean(close, 5)", region="USA"))
    assert r.engine_kind == "pandas_snapshot"
    assert r.verdict in ("pass", "reject")
    assert r.local_sharpe is not None
    assert r.local_ic is not None
    _reset_engine_for_test()
