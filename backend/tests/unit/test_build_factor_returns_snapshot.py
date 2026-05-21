"""Tier F prep — build_factor_returns_snapshot builder tests.

Verifies the vendor-CSV → R13 parquet converter produces a snapshot
that factor_lens_service.load_factor_returns can actually consume
(round-trip), plus mapping / scale / missing-factor / coverage paths.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from scripts import build_factor_returns_snapshot as builder


def _make_ff_csv(path: Path, n: int = 600, in_percent: bool = True) -> None:
    """Synthetic Fama-French-style daily CSV (Date + SMB/HML/RMW/Mom/BAB)."""
    dates = pd.date_range("2022-01-01", periods=n, freq="B")
    rng = np.random.default_rng(0)
    scale = 1.0 if in_percent else 0.01  # values ~±0.5% either as 0.5 or 0.005
    df = pd.DataFrame({
        "Date": dates.strftime("%Y-%m-%d"),
        "SMB": rng.normal(0, 0.5, n) / scale,
        "HML": rng.normal(0, 0.5, n) / scale,
        "RMW": rng.normal(0, 0.5, n) / scale,
        "Mom": rng.normal(0, 0.5, n) / scale,
        "BAB": rng.normal(0, 0.5, n) / scale,
    })
    df.to_csv(path, index=False)


def test_build_fama_french_preset_roundtrip(tmp_path, monkeypatch):
    csv = tmp_path / "ff5.csv"
    _make_ff_csv(csv, n=600, in_percent=True)
    out_dir = tmp_path / "snap"
    monkeypatch.setattr(builder, "_OUT_DIR", out_dir)

    rc = builder.build(
        region="USA", csv_path=csv,
        column_map=dict(builder._PRESETS["fama_french"]["map"]),
        scale=0.01, date_col="Date", strict=False, dry_run=False,
    )
    assert rc == 0
    parquet = out_dir / "usa.parquet"
    assert parquet.exists()

    # Round-trip: factor_lens_service.load_factor_returns must read it.
    from backend.services import factor_lens_service as fls
    monkeypatch.setattr(fls, "_SNAPSHOT_DIR", out_dir)
    fdf = fls.load_factor_returns("USA")
    assert fdf is not None
    assert list(fdf.columns) == ["size", "value", "momentum", "quality", "low_vol"]
    assert len(fdf) == 600
    # percent→decimal scale applied: values should be small (~±0.02)
    assert fdf.abs().to_numpy().max() < 0.1


def test_build_custom_map(tmp_path, monkeypatch):
    csv = tmp_path / "c.csv"
    n = 520
    dates = pd.date_range("2023-01-01", periods=n, freq="B")
    rng = np.random.default_rng(1)
    pd.DataFrame({
        "dt": dates.strftime("%Y-%m-%d"),
        "smb": rng.normal(0, 0.005, n),
        "hml": rng.normal(0, 0.005, n),
        "mom": rng.normal(0, 0.005, n),
        "qmj": rng.normal(0, 0.005, n),
        "bab": rng.normal(0, 0.005, n),
    }).to_csv(csv, index=False)

    out_dir = tmp_path / "snap"
    monkeypatch.setattr(builder, "_OUT_DIR", out_dir)
    cmap = {"size": "smb", "value": "hml", "momentum": "mom", "quality": "qmj", "low_vol": "bab"}
    rc = builder.build(
        region="CHN", csv_path=csv, column_map=cmap,
        scale=1.0, date_col="dt", strict=False, dry_run=False,
    )
    assert rc == 0
    assert (out_dir / "chn.parquet").exists()


def test_build_missing_factor_neutral_fill(tmp_path, monkeypatch):
    """FF5 lacks low_vol → filled 0.0 (neutral) when not strict."""
    csv = tmp_path / "ff_no_bab.csv"
    n = 520
    dates = pd.date_range("2022-01-01", periods=n, freq="B")
    rng = np.random.default_rng(2)
    pd.DataFrame({
        "Date": dates.strftime("%Y-%m-%d"),
        "SMB": rng.normal(0, 0.5, n), "HML": rng.normal(0, 0.5, n),
        "RMW": rng.normal(0, 0.5, n), "Mom": rng.normal(0, 0.5, n),
        # no BAB
    }).to_csv(csv, index=False)
    out_dir = tmp_path / "snap"
    monkeypatch.setattr(builder, "_OUT_DIR", out_dir)
    rc = builder.build(
        region="USA", csv_path=csv,
        column_map=dict(builder._PRESETS["fama_french"]["map"]),
        scale=0.01, date_col="Date", strict=False, dry_run=False,
    )
    assert rc == 0
    df = pd.read_parquet(out_dir / "usa.parquet")
    assert (df["low_vol"] == 0.0).all()  # neutral fill


def test_build_strict_fails_on_missing(tmp_path, monkeypatch):
    csv = tmp_path / "ff_no_bab.csv"
    n = 520
    dates = pd.date_range("2022-01-01", periods=n, freq="B")
    pd.DataFrame({
        "Date": dates.strftime("%Y-%m-%d"),
        "SMB": np.zeros(n), "HML": np.zeros(n), "RMW": np.zeros(n), "Mom": np.zeros(n),
    }).to_csv(csv, index=False)
    monkeypatch.setattr(builder, "_OUT_DIR", tmp_path / "snap")
    rc = builder.build(
        region="USA", csv_path=csv,
        column_map=dict(builder._PRESETS["fama_french"]["map"]),
        scale=0.01, date_col="Date", strict=True, dry_run=False,
    )
    assert rc == 1  # strict → fail


def test_build_dry_run_does_not_write(tmp_path, monkeypatch):
    csv = tmp_path / "ff5.csv"
    _make_ff_csv(csv, n=520)
    out_dir = tmp_path / "snap"
    monkeypatch.setattr(builder, "_OUT_DIR", out_dir)
    rc = builder.build(
        region="USA", csv_path=csv,
        column_map=dict(builder._PRESETS["fama_french"]["map"]),
        scale=0.01, date_col="Date", strict=False, dry_run=True,
    )
    assert rc == 0
    assert not (out_dir / "usa.parquet").exists()  # dry-run wrote nothing


def test_build_missing_csv_returns_1(tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "_OUT_DIR", tmp_path / "snap")
    rc = builder.build(
        region="USA", csv_path=tmp_path / "nope.csv",
        column_map=dict(builder._PRESETS["fama_french"]["map"]),
        scale=0.01, date_col="Date", strict=False, dry_run=False,
    )
    assert rc == 1


def test_build_bad_date_col_returns_1(tmp_path, monkeypatch):
    csv = tmp_path / "ff5.csv"
    _make_ff_csv(csv, n=520)
    monkeypatch.setattr(builder, "_OUT_DIR", tmp_path / "snap")
    rc = builder.build(
        region="USA", csv_path=csv,
        column_map=dict(builder._PRESETS["fama_french"]["map"]),
        scale=0.01, date_col="NONEXISTENT", strict=False, dry_run=False,
    )
    assert rc == 1


def test_build_dedups_and_sorts(tmp_path, monkeypatch):
    """Duplicate dates → keep last; unsorted → sorted ascending."""
    csv = tmp_path / "dup.csv"
    pd.DataFrame({
        "Date": ["2022-03-02", "2022-03-01", "2022-03-02"],
        "SMB": [0.1, 0.2, 0.3], "HML": [0, 0, 0], "RMW": [0, 0, 0],
        "Mom": [0, 0, 0], "BAB": [0, 0, 0],
    }).to_csv(csv, index=False)
    out_dir = tmp_path / "snap"
    monkeypatch.setattr(builder, "_OUT_DIR", out_dir)
    builder.build(
        region="USA", csv_path=csv,
        column_map=dict(builder._PRESETS["fama_french"]["map"]),
        scale=1.0, date_col="Date", strict=False, dry_run=False,
    )
    df = pd.read_parquet(out_dir / "usa.parquet")
    assert len(df) == 2  # deduped
    assert df.index.is_monotonic_increasing  # sorted
    # kept last for 2022-03-02 → size=0.3
    assert df.loc["2022-03-02", "size"] == pytest.approx(0.3)
