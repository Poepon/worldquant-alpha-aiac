"""Extract AQR Betting-Against-Beta (BAB) daily returns for one region and
merge them in as the ``low_vol`` factor for the R13 snapshot pipeline.

Why this exists
---------------
Fama-French has no low-volatility factor, so the FF-only USA snapshot
0-fills ``low_vol``. AQR's "Betting Against Beta: Equity Factors — Daily"
workbook supplies a real BAB series per country. This script pulls one
country column out of that .xlsx and appends it as a ``BAB`` column to the
cleaned FF CSV produced by ``prep_fama_french_csv.py``, so the existing
``build_factor_returns_snapshot.py --preset fama_french`` (which already
maps ``low_vol → BAB``) picks it up automatically.

The AQR workbook layout (verified 2026-05-20)
---------------------------------------------
  * sheet ``BAB Factors``
  * ~18 preamble rows, then a header row whose first cell is ``DATE`` and
    whose other columns are ISO country codes (AUS, ..., USA, Global, ...),
  * daily rows keyed by ``MM/DD/YYYY``,
  * **values are DECIMAL daily returns** (USA median |r| ≈ 0.0026, NOT
    percent). The header row is auto-detected (scan for first cell == DATE)
    with a fallback to ``--header-row``.

The scale gotcha (important)
----------------------------
The FF clean CSV is in **percent** units (``prep_fama_french_csv.py`` keeps
FF native; ``build_factor_returns_snapshot.py`` applies ``scale=0.01``).
AQR BAB is **decimal**. To merge into the percent CSV so the single uniform
``×0.01`` is correct for every column, this script converts BAB
decimal→percent (``×100``) by default (``--source-scale decimal``). Pass
``--source-scale percent`` if your file is already in percent.

Usage
-----
::

    python scripts/prep_aqr_bab.py \\
        --xlsx data/AQR_BAB_Equity_Factors_Daily.xlsx \\
        --region-col USA \\
        --merge-into data/ff_usa_clean.csv \\
        --out data/ff_usa_full.csv

    # then rebuild the snapshot WITH low_vol:
    python scripts/build_factor_returns_snapshot.py \\
        --region USA --csv data/ff_usa_full.csv --preset fama_french --date-col Date
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger("prep_aqr_bab")
logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

_DEFAULT_SHEET = "BAB Factors"
_DEFAULT_HEADER_ROW = 18  # 0-indexed fallback if auto-detect fails


def _extract_bab(
    xlsx_path: Path, *, sheet: str, region_col: str,
    header_row_fallback: int, source_scale: str,
):
    """Return a 2-col DataFrame: Date (ISO str) + BAB (percent units)."""
    import pandas as pd

    raw = pd.read_excel(xlsx_path, sheet_name=sheet, header=None)

    # Auto-detect the header row: first row whose first cell == "DATE"
    hdr = None
    for i in range(min(60, len(raw))):
        if str(raw.iloc[i, 0]).strip().upper() == "DATE":
            hdr = i
            break
    if hdr is None:
        logger.warning(
            "no 'DATE' header cell found in first 60 rows of sheet %r — "
            "falling back to --header-row %d", sheet, header_row_fallback)
        hdr = header_row_fallback

    cols = [str(c).strip() for c in raw.iloc[hdr].tolist()]
    body = raw.iloc[hdr + 1:].copy()
    body.columns = cols

    if "DATE" not in cols:
        raise ValueError(f"no DATE column in header row {hdr}: {cols[:8]}")
    if region_col not in cols:
        country_like = [c for c in cols if isinstance(c, str) and 0 < len(c) <= 8]
        raise ValueError(
            f"region column {region_col!r} not found. available: {country_like}")

    out = pd.DataFrame()
    out["Date"] = pd.to_datetime(body["DATE"], errors="coerce")
    out["BAB"] = pd.to_numeric(body[region_col], errors="coerce")
    out = out.dropna(subset=["Date", "BAB"]).sort_values("Date")

    if out.empty:
        raise ValueError(f"0 valid {region_col} rows after parse")

    # decimal → percent so it matches the FF percent CSV (uniform ×0.01 later)
    if source_scale == "decimal":
        out["BAB"] = out["BAB"] * 100.0
    elif source_scale != "percent":
        raise ValueError(f"--source-scale must be decimal|percent, got {source_scale!r}")

    out["Date"] = out["Date"].dt.strftime("%Y-%m-%d")

    # Sanity: in percent units a daily BAB should be ~O(0.1-1.0). Warn if not.
    med = float(out["BAB"].abs()[out["BAB"].abs() > 0].median())
    logger.info(
        "%s BAB: %d rows (%s → %s), median |r| = %.4g (percent units)",
        region_col, len(out), out["Date"].iloc[0], out["Date"].iloc[-1], med)
    if med > 5.0:
        logger.warning(
            "median |BAB| = %.4g in percent units looks 100x too big — is the "
            "source already percent? pass --source-scale percent", med)
    elif med < 1e-3:
        logger.warning(
            "median |BAB| = %.4g in percent units looks too small — check the "
            "region column / source scale", med)
    return out


def build(
    *, xlsx_path: Path, region_col: str, sheet: str, header_row_fallback: int,
    source_scale: str, merge_into: Optional[Path], out_path: Path,
) -> int:
    try:
        import pandas as pd
    except ImportError:
        logger.error("pandas/openpyxl required — pip install pandas openpyxl")
        return 1

    if not xlsx_path.exists():
        logger.error("xlsx not found: %s", xlsx_path)
        return 1

    try:
        bab = _extract_bab(
            xlsx_path, sheet=sheet, region_col=region_col,
            header_row_fallback=header_row_fallback, source_scale=source_scale)
    except ImportError:
        logger.error("openpyxl required to read .xlsx — pip install openpyxl")
        return 1
    except Exception as e:  # noqa: BLE001
        logger.error("BAB extraction failed: %s", e)
        return 1

    if merge_into is None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        bab.to_csv(out_path, index=False)
        logger.info("wrote standalone %s (%d rows)", out_path, len(bab))
        return 0

    if not merge_into.exists():
        logger.error("--merge-into file not found: %s", merge_into)
        return 1
    base = pd.read_csv(merge_into)
    if "Date" not in base.columns:
        logger.error("--merge-into CSV has no 'Date' column: %s", list(base.columns))
        return 1
    if "BAB" in base.columns:
        logger.warning("base CSV already has a BAB column — overwriting it")
        base = base.drop(columns=["BAB"])

    merged = base.merge(bab, on="Date", how="left")
    matched = int(merged["BAB"].notna().sum())
    coverage = matched / len(merged) if len(merged) else 0.0
    logger.info(
        "merged BAB onto %d base rows: %d matched (%.1f%% coverage), %d gaps",
        len(merged), matched, coverage * 100, len(merged) - matched)
    if coverage < 0.95:
        logger.warning(
            "BAB covers only %.1f%% of base dates — R13 decompose drops rows "
            "where ANY factor is NaN, so the usable OLS overlap shrinks to the "
            "intersection. Acceptable if the uncovered span is the early years.",
            coverage * 100)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(out_path, index=False)
    logger.info("wrote %s (%d rows × %d cols)", out_path, len(merged), len(merged.columns))
    logger.info(
        "next: python scripts/build_factor_returns_snapshot.py "
        "--region USA --csv %s --preset fama_french --date-col Date", out_path)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--xlsx", required=True, type=Path, help="AQR BAB daily .xlsx")
    p.add_argument("--region-col", default="USA", help="country column to extract (default USA)")
    p.add_argument("--sheet", default=_DEFAULT_SHEET, help="worksheet name")
    p.add_argument("--header-row", type=int, default=_DEFAULT_HEADER_ROW,
                   help="0-indexed header row fallback if auto-detect fails")
    p.add_argument("--source-scale", default="decimal", choices=["decimal", "percent"],
                   help="units of the AQR file (AQR daily = decimal)")
    p.add_argument("--merge-into", type=Path, default=None,
                   help="FF clean CSV to append BAB onto (omit → standalone Date,BAB CSV)")
    p.add_argument("--out", required=True, type=Path, help="output CSV path")
    args = p.parse_args()
    return build(
        xlsx_path=args.xlsx, region_col=args.region_col, sheet=args.sheet,
        header_row_fallback=args.header_row, source_scale=args.source_scale,
        merge_into=args.merge_into, out_path=args.out)


if __name__ == "__main__":
    sys.exit(main())
