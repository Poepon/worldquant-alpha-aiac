"""Tests for scripts/prep_aqr_bab.py (Tier F low_vol, 2026-05-20).

Builds a synthetic AQR-BAB-format xlsx (preamble + DATE header + country
columns in DECIMAL units) and asserts the extractor:
  - auto-detects the header row (first cell == DATE), not a hardcoded index,
  - extracts the requested region column and converts decimal→percent,
  - merges BAB onto a FF clean CSV (Date join) reporting coverage,
  - the combined CSV produces a snapshot with a NON-zero low_vol factor.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

pytest.importorskip("openpyxl")


def _write_synthetic_aqr(path: Path, n: int = 30, preamble: int = 5) -> None:
    """Write an AQR-BAB-like xlsx: preamble rows, a DATE header, then
    MM/DD/YYYY rows with DECIMAL daily returns for AUS + USA."""
    dates = pd.date_range("2022-01-03", periods=n, freq="B")
    rows = []
    for i in range(preamble):
        rows.append([f"AQR disclaimer line {i}", None, None])
    rows.append(["DATE", "AUS", "USA"])  # header (auto-detected)
    for d in dates:
        rows.append([d.strftime("%m/%d/%Y"), 0.001, 0.0025])  # decimal returns
    df = pd.DataFrame(rows)
    df.to_excel(path, sheet_name="BAB Factors", header=False, index=False)


def test_extract_autodetects_header_and_converts_decimal_to_percent(tmp_path):
    from scripts.prep_aqr_bab import _extract_bab

    xlsx = tmp_path / "aqr.xlsx"
    _write_synthetic_aqr(xlsx, n=20, preamble=7)  # header NOT at row 18
    bab = _extract_bab(
        xlsx, sheet="BAB Factors", region_col="USA",
        header_row_fallback=18, source_scale="decimal",
    )
    assert list(bab.columns) == ["Date", "BAB"]
    assert len(bab) == 20
    assert bab["Date"].iloc[0] == "2022-01-03"  # MM/DD/YYYY → ISO
    # 0.0025 decimal → 0.25 percent (×100)
    assert bab["BAB"].iloc[0] == pytest.approx(0.25)


def test_extract_percent_source_no_rescale(tmp_path):
    from scripts.prep_aqr_bab import _extract_bab

    xlsx = tmp_path / "aqr.xlsx"
    _write_synthetic_aqr(xlsx, n=10)
    bab = _extract_bab(
        xlsx, sheet="BAB Factors", region_col="USA",
        header_row_fallback=18, source_scale="percent",
    )
    # 0.0025 left as-is when caller declares percent
    assert bab["BAB"].iloc[0] == pytest.approx(0.0025)


def test_extract_unknown_region_raises(tmp_path):
    from scripts.prep_aqr_bab import _extract_bab

    xlsx = tmp_path / "aqr.xlsx"
    _write_synthetic_aqr(xlsx, n=10)
    with pytest.raises(ValueError, match="not found"):
        _extract_bab(
            xlsx, sheet="BAB Factors", region_col="ZZZ",
            header_row_fallback=18, source_scale="decimal",
        )


def test_merge_into_ff_clean_adds_bab_column(tmp_path):
    from scripts.prep_aqr_bab import build

    # FF clean CSV (percent units), Date + a couple factors
    dates = pd.date_range("2022-01-03", periods=15, freq="B")
    pd.DataFrame({
        "Date": dates.strftime("%Y-%m-%d"),
        "SMB": 0.1, "HML": 0.2, "RMW": 0.05, "Mom": 0.3,
    }).to_csv(tmp_path / "ff_clean.csv", index=False)

    xlsx = tmp_path / "aqr.xlsx"
    _write_synthetic_aqr(xlsx, n=15)
    out = tmp_path / "ff_full.csv"
    rc = build(
        xlsx_path=xlsx, region_col="USA", sheet="BAB Factors",
        header_row_fallback=18, source_scale="decimal",
        merge_into=tmp_path / "ff_clean.csv", out_path=out,
    )
    assert rc == 0
    df = pd.read_csv(out)
    assert "BAB" in df.columns
    assert len(df) == 15
    assert df["BAB"].notna().all()  # 100% coverage on aligned dates
    assert df["BAB"].iloc[0] == pytest.approx(0.25)  # decimal→percent


def test_full_csv_yields_nonzero_low_vol_snapshot(tmp_path, monkeypatch):
    """End-to-end: FF+BAB combined CSV → snapshot with live low_vol."""
    from scripts.prep_aqr_bab import build as bab_build
    from scripts import build_factor_returns_snapshot as snap

    dates = pd.date_range("2022-01-03", periods=520, freq="B")
    pd.DataFrame({
        "Date": dates.strftime("%Y-%m-%d"),
        "SMB": 0.1, "HML": 0.2, "RMW": 0.05, "Mom": 0.3,
    }).to_csv(tmp_path / "ff_clean.csv", index=False)
    xlsx = tmp_path / "aqr.xlsx"
    _write_synthetic_aqr(xlsx, n=520)
    full = tmp_path / "ff_full.csv"
    assert bab_build(
        xlsx_path=xlsx, region_col="USA", sheet="BAB Factors",
        header_row_fallback=18, source_scale="decimal",
        merge_into=tmp_path / "ff_clean.csv", out_path=full,
    ) == 0

    monkeypatch.setattr(snap, "_OUT_DIR", tmp_path / "snap")
    assert snap.build(
        region="USA", csv_path=full,
        column_map=dict(snap._PRESETS["fama_french"]["map"]),
        scale=0.01, date_col="Date", strict=False, dry_run=False,
    ) == 0
    out = pd.read_parquet(tmp_path / "snap" / "usa.parquet")
    # low_vol now populated, NOT 0-filled; 0.25% → 0.0025 decimal
    assert not (out["low_vol"] == 0.0).all()
    assert out["low_vol"].iloc[0] == pytest.approx(0.0025, rel=1e-3)
