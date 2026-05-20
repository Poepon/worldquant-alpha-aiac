"""Build a R13 factor-returns snapshot parquet from a vendor CSV.

Phase 4 Tier F prep (decision-independent). R13 factor_lens
(``backend/services/factor_lens_service.py``) reads
``backend/data/factor_returns_snapshot/{region}.parquet`` with schema:

    DatetimeIndex (trading day) × 5 factor columns:
        size  value  momentum  quality  low_vol      (decimal daily returns)

The operator downloads the underlying factor returns from a vendor
(licensing / source is their call) and points this script at the CSV;
the script maps the vendor's column names onto the 5 R13 factors,
optionally rescales percent→decimal, validates coverage, and writes the
parquet. It does NOT fetch any URL — input is a local CSV path.

Vendor presets (column mapping + scale)
---------------------------------------
``--preset fama_french`` (Ken French Data Library, daily, returns in %):
    Date → index
    SMB  → size       HML → value      RMW → quality
    Mom  → momentum   (low_vol absent in FF5 → see --low-vol-col / BAB)
    scale = 0.01 (FF data is in percent)

``--preset aqr`` (AQR Data Sets, daily):
    DATE → index
    SMB → size  HML_FF / HML_DEVIL → value  QMJ → quality
    UMD → momentum  BAB → low_vol
    scale = 1.0 (AQR daily files are already decimal — VERIFY your file)

``--preset custom`` (default): supply ``--map`` JSON explicitly, e.g.
    --map '{"size":"SMB","value":"HML","momentum":"Mom","quality":"RMW","low_vol":"BAB"}'

Missing factors
---------------
FF5 has no momentum or low-vol in the main file (momentum is a separate
download; low-vol/BAB is AQR). When a mapped column is absent, the script
fills that factor with 0.0 (neutral) + WARNS — R13's OLS still runs, that
factor's beta is just ~0. Better than refusing to build. Use --strict to
fail instead.

Usage
-----
::

    # Fama-French 5-factor daily (returns in %), momentum merged in
    python scripts/build_factor_returns_snapshot.py \\
        --region USA --csv ff5_daily.csv --preset fama_french \\
        --date-col Date

    # explicit custom mapping
    python scripts/build_factor_returns_snapshot.py \\
        --region CHN --csv my_factors.csv --preset custom \\
        --date-col date \\
        --map '{"size":"smb","value":"hml","momentum":"mom","quality":"qmj","low_vol":"bab"}' \\
        --scale 1.0

Writes ``backend/data/factor_returns_snapshot/{region}.parquet`` and
prints a coverage summary. ``--dry-run`` validates without writing.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("build_factor_returns_snapshot")
logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")


_TARGET_FACTORS = ["size", "value", "momentum", "quality", "low_vol"]

_PRESETS: Dict[str, Dict] = {
    "fama_french": {
        "map": {
            "size": "SMB", "value": "HML", "quality": "RMW",
            "momentum": "Mom", "low_vol": "BAB",
        },
        "scale": 0.01,   # FF daily data is in percent
        "date_col": "Date",
    },
    "aqr": {
        "map": {
            "size": "SMB", "value": "HML_FF", "quality": "QMJ",
            "momentum": "UMD", "low_vol": "BAB",
        },
        "scale": 1.0,    # VERIFY: AQR daily files vary
        "date_col": "DATE",
    },
    "custom": {"map": {}, "scale": 1.0, "date_col": "date"},
}

# Output dir mirrors factor_lens_service._SNAPSHOT_DIR
_OUT_DIR = (
    Path(__file__).resolve().parent.parent
    / "backend" / "data" / "factor_returns_snapshot"
)

_MIN_COVERAGE_DAYS = 504  # FACTOR_LENS_OLS_LOOKBACK_DAYS default (~2y)


def build(
    *,
    region: str,
    csv_path: Path,
    column_map: Dict[str, str],
    scale: float,
    date_col: str,
    strict: bool,
    dry_run: bool,
) -> int:
    try:
        import numpy as np
        import pandas as pd
    except ImportError:
        logger.error("pandas/numpy required — pip install pandas")
        return 1

    if not csv_path.exists():
        logger.error("CSV not found: %s", csv_path)
        return 1

    try:
        raw = pd.read_csv(csv_path)
    except Exception as e:  # noqa: BLE001
        logger.error("CSV parse failed: %s", e)
        return 1

    if date_col not in raw.columns:
        logger.error(
            "date column %r not in CSV columns %s", date_col, list(raw.columns)
        )
        return 1

    # Build the 5-factor frame
    out = pd.DataFrame()
    out["__date"] = pd.to_datetime(raw[date_col], errors="coerce")
    missing_factors = []
    for factor in _TARGET_FACTORS:
        src = column_map.get(factor)
        if src and src in raw.columns:
            out[factor] = pd.to_numeric(raw[src], errors="coerce") * scale
        else:
            missing_factors.append((factor, src))
            out[factor] = 0.0  # neutral fill

    if missing_factors:
        msg = ", ".join(f"{f}(←{s or 'unmapped'})" for f, s in missing_factors)
        if strict:
            logger.error("missing/unmapped factors in --strict mode: %s", msg)
            return 1
        logger.warning(
            "filling missing factors with 0.0 (neutral): %s — "
            "R13 OLS runs but these factors get ~0 beta", msg
        )

    # Index by date, drop bad/dup rows, sort
    out = out.dropna(subset=["__date"]).set_index("__date").sort_index()
    out = out[~out.index.duplicated(keep="last")]
    out.index.name = "date"

    n_days = len(out)
    if n_days == 0:
        logger.error("0 valid rows after date parse — check --date-col / CSV")
        return 1

    span_days = (out.index.max() - out.index.min()).days
    logger.info(
        "parsed %d trading days (calendar span %d days, %s → %s)",
        n_days, span_days, out.index.min().date(), out.index.max().date(),
    )
    if n_days < _MIN_COVERAGE_DAYS:
        logger.warning(
            "coverage %d days < recommended %d (~2y); R13 needs ≥ "
            "FACTOR_LENS_OLS_LOOKBACK_DAYS overlap per alpha",
            n_days, _MIN_COVERAGE_DAYS,
        )

    # Sanity: daily factor returns realistically have abs_max ~0.02-0.05.
    # Symmetric check (R2 review fix) — flag BOTH over- and under-scaled:
    nonzero = out[_TARGET_FACTORS].abs().to_numpy()
    abs_max = float(nonzero.max()) if nonzero.size else 0.0
    nz = nonzero[nonzero > 0]
    median_abs = float(np.median(nz)) if nz.size else 0.0
    if abs_max > 0.5:
        logger.warning(
            "max |daily return| = %.4g after scale=%.4g — looks like "
            "percent data not rescaled? (pass --scale 0.01 for FF percent)",
            abs_max, scale,
        )
    elif median_abs > 0 and median_abs < 1e-4:
        logger.warning(
            "median |daily return| = %.2e after scale=%.4g — looks 10-100x "
            "too SMALL (already-decimal data double-scaled by 0.01? pass "
            "--scale 1.0). R13 OLS betas would be silently wrong.",
            median_abs, scale,
        )

    if dry_run:
        logger.info("[dry-run] validated; NOT writing. Preview:\n%s", out.head())
        return 0

    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _OUT_DIR / f"{region.lower()}.parquet"
    try:
        out.to_parquet(out_path)
    except Exception as e:  # noqa: BLE001
        logger.error("parquet write failed (need pyarrow/fastparquet?): %s", e)
        return 1

    logger.info("wrote %s (%d days × %d factors)", out_path, n_days, len(_TARGET_FACTORS))
    logger.info(
        "verify via: GET /ops/r13/snapshot-stale-check (region %s should now "
        "show exists=true)", region.lower(),
    )
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--region", required=True, help="region code (USA/CHN/JPN/EUR/HKG)")
    p.add_argument("--csv", required=True, type=Path, help="vendor factor-returns CSV")
    p.add_argument(
        "--preset", default="custom", choices=list(_PRESETS.keys()),
        help="vendor preset (fama_french / aqr / custom)",
    )
    p.add_argument("--date-col", default=None, help="override date column name")
    p.add_argument("--map", default=None, help="custom factor→column JSON map")
    p.add_argument("--scale", type=float, default=None, help="multiply returns (FF=0.01)")
    p.add_argument("--low-vol-col", default=None, help="override the low_vol source column")
    p.add_argument("--strict", action="store_true", help="fail on any missing factor")
    p.add_argument("--dry-run", action="store_true", help="validate, do not write")
    args = p.parse_args()

    preset = _PRESETS[args.preset]
    column_map = dict(preset["map"])
    if args.map:
        try:
            column_map.update(json.loads(args.map))
        except Exception as e:  # noqa: BLE001
            logger.error("--map is not valid JSON: %s", e)
            return 1
    if args.low_vol_col:
        column_map["low_vol"] = args.low_vol_col
    scale = args.scale if args.scale is not None else preset["scale"]
    date_col = args.date_col or preset["date_col"]

    logger.info(
        "region=%s preset=%s date_col=%s scale=%.4g map=%s",
        args.region, args.preset, date_col, scale, column_map,
    )
    return build(
        region=args.region, csv_path=args.csv, column_map=column_map,
        scale=scale, date_col=date_col, strict=args.strict, dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
