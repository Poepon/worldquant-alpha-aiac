"""Tests for scripts/prep_fama_french_csv.py (Tier F prep, 2026-05-20).

Feeds a synthetic Ken-French-format CSV (preamble + header + daily section +
footer + missing sentinels) and asserts the cleaner:
  - strips the preamble and stops at the footer,
  - converts YYYYMMDD → ISO and FF -99.99 sentinels → NaN,
  - merges FF5 + Momentum on Date and normalizes the momentum column to 'Mom',
  - produces a CSV that build_factor_returns_snapshot.py consumes end-to-end.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pandas as pd
import pytest


def _write_ff5(path: Path, n: int = 30, with_sentinel: bool = True) -> None:
    dates = pd.date_range("2022-01-03", periods=n, freq="B")
    lines = [
        "This file was created by CMPT using the 202312 CRSP database.",
        "The 1-month TBill return is from Ibbotson and Associates, Inc.",
        "",  # blank line before header
        ",Mkt-RF,SMB,HML,RMW,CMA,RF",
    ]
    for i, d in enumerate(dates):
        ds = d.strftime("%Y%m%d")
        if with_sentinel and i == 0:
            # FF missing sentinel on the first row
            lines.append(f"{ds},  0.50,-99.99,  0.20,  0.05, -0.03,  0.001")
        else:
            lines.append(f"{ds},  0.50, -0.10,  0.20,  0.05, -0.03,  0.001")
    # Trailing footer (must be ignored)
    lines += ["", "Copyright 2024 Kenneth R. French"]
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_mom(path: Path, n: int = 30, col_name: str = "Mom") -> None:
    dates = pd.date_range("2022-01-03", periods=n, freq="B")
    lines = [
        "This file was created using the momentum factor construction.",
        "",
        f",{col_name}",
    ]
    for d in dates:
        lines.append(f"{d.strftime('%Y%m%d')},  0.30")
    lines += ["", "Copyright 2024 Kenneth R. French"]
    path.write_text("\n".join(lines), encoding="utf-8")


def test_prep_strips_preamble_footer_and_converts_sentinel(tmp_path):
    from scripts.prep_fama_french_csv import build

    ff5 = tmp_path / "ff5.csv"
    mom = tmp_path / "mom.csv"
    out = tmp_path / "clean.csv"
    _write_ff5(ff5, n=30)
    _write_mom(mom, n=30)

    rc = build(ff5_path=ff5, mom_path=mom, out_path=out)
    assert rc == 0
    assert out.exists()

    df = pd.read_csv(out)
    # Footer + preamble gone → exactly the daily rows survive
    assert len(df) == 30
    assert list(df.columns[:1]) == ["Date"]
    for c in ("SMB", "HML", "RMW", "Mom"):
        assert c in df.columns
    # ISO dates
    assert df["Date"].iloc[0] == "2022-01-03"
    # FF -99.99 sentinel → NaN
    assert pd.isna(df["SMB"].iloc[0])
    # values stay in percent (NOT rescaled here — builder does that)
    assert df["HML"].iloc[0] == pytest.approx(0.20)
    assert df["Mom"].iloc[0] == pytest.approx(0.30)


def test_prep_reads_from_zip_and_renames_wml(tmp_path):
    """Accepts .zip directly; normalizes a non-'Mom' momentum column to 'Mom'."""
    from scripts.prep_fama_french_csv import build

    ff5_csv = tmp_path / "ff5.csv"
    _write_ff5(ff5_csv, n=20, with_sentinel=False)
    ff5_zip = tmp_path / "ff5.zip"
    with zipfile.ZipFile(ff5_zip, "w") as z:
        z.write(ff5_csv, arcname="F-F_Research_Data_5_Factors_2x3_daily.CSV")

    mom_csv = tmp_path / "mom.csv"
    _write_mom(mom_csv, n=20, col_name="WML")  # alternate momentum header
    mom_zip = tmp_path / "mom.zip"
    with zipfile.ZipFile(mom_zip, "w") as z:
        z.write(mom_csv, arcname="F-F_Momentum_Factor_daily.CSV")

    out = tmp_path / "clean.csv"
    rc = build(ff5_path=ff5_zip, mom_path=mom_zip, out_path=out)
    assert rc == 0
    df = pd.read_csv(out)
    assert "Mom" in df.columns  # WML normalized to Mom
    assert len(df) == 20


def test_prep_missing_momentum_still_builds(tmp_path):
    from scripts.prep_fama_french_csv import build

    ff5 = tmp_path / "ff5.csv"
    out = tmp_path / "clean.csv"
    _write_ff5(ff5, n=15)
    rc = build(ff5_path=ff5, mom_path=None, out_path=out)
    assert rc == 0
    df = pd.read_csv(out)
    assert "SMB" in df.columns and "Mom" not in df.columns
    assert len(df) == 15


def test_prep_output_feeds_snapshot_builder_end_to_end(tmp_path, monkeypatch):
    """The cleaned CSV is consumable by build_factor_returns_snapshot."""
    from scripts.prep_fama_french_csv import build as prep_build
    from scripts import build_factor_returns_snapshot as snap

    ff5 = tmp_path / "ff5.csv"
    mom = tmp_path / "mom.csv"
    clean = tmp_path / "clean.csv"
    _write_ff5(ff5, n=520)  # ~2y → satisfies coverage
    _write_mom(mom, n=520)
    assert prep_build(ff5_path=ff5, mom_path=mom, out_path=clean) == 0

    monkeypatch.setattr(snap, "_OUT_DIR", tmp_path / "snap")
    rc = snap.build(
        region="USA", csv_path=clean,
        column_map=dict(snap._PRESETS["fama_french"]["map"]),
        scale=0.01, date_col="Date", strict=False, dry_run=False,
    )
    assert rc == 0
    parquet = tmp_path / "snap" / "usa.parquet"
    assert parquet.exists()
    out = pd.read_parquet(parquet)
    # 5 R13 factor columns present; percent→decimal applied (0.20% → 0.002)
    for c in ("size", "value", "momentum", "quality", "low_vol"):
        assert c in out.columns
    assert out["value"].iloc[0] == pytest.approx(0.002, rel=1e-3)
    # low_vol absent in FF → 0-filled
    assert (out["low_vol"] == 0.0).all()
