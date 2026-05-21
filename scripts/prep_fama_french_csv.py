"""Clean + merge Ken French daily factor files into ONE CSV that
``build_factor_returns_snapshot.py --preset fama_french`` can consume.

Why this exists
---------------
Ken French Data Library files are NOT directly readable by pandas:

  * a multi-line free-text preamble (copyright / description),
  * then the real header row (first cell empty, e.g. ``,Mkt-RF,SMB,HML,...``),
  * then the daily data section (rows keyed by an 8-digit ``YYYYMMDD``),
  * sometimes a trailing copyright line or an appended annual/monthly section.

FF also returns are in **percent** and use ``-99.99`` / ``-999`` as missing
sentinels. This script:

  1. (optionally) reads straight from the downloaded ``.zip`` (no manual unzip),
  2. locates the header by finding the first ``YYYYMMDD,`` data row and taking
     the line just above it as the column names,
  3. keeps only consecutive ``YYYYMMDD`` data rows (stops at footer / next
     section),
  4. converts the date to ISO ``YYYY-MM-DD`` and FF missing sentinels to NaN,
  5. merges the 5-Factor file with the Momentum file on Date (inner join),
  6. writes a single clean CSV with columns
     ``Date, Mkt-RF, SMB, HML, RMW, CMA, RF, Mom``.

It does NOT rescale percent→decimal — leave that to
``build_factor_returns_snapshot.py`` (its ``--preset fama_french`` applies
``scale=0.01``). Keeping the cleaned CSV in the vendor's native percent units
means the scale lives in exactly one place.

Download (Ken French Data Library, daily, free)
------------------------------------------------
  * F-F Research Data 5 Factors (daily):
      F-F_Research_Data_5_Factors_2x3_daily_CSV.zip   → SMB / HML / RMW
  * F-F Momentum Factor (daily):
      F-F_Momentum_Factor_daily_CSV.zip               → Mom
  (low-vol/BAB is NOT in FF — build_factor_returns_snapshot fills low_vol=0.)

Usage
-----
::

    python scripts/prep_fama_french_csv.py \\
        --ff5 F-F_Research_Data_5_Factors_2x3_daily_CSV.zip \\
        --mom F-F_Momentum_Factor_daily_CSV.zip \\
        --out ff_usa_clean.csv

    # then:
    python scripts/build_factor_returns_snapshot.py \\
        --region USA --csv ff_usa_clean.csv --preset fama_french --date-col Date
"""
from __future__ import annotations

import argparse
import io
import logging
import re
import sys
import zipfile
from pathlib import Path

logger = logging.getLogger("prep_fama_french_csv")
logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

_DATE_ROW_RE = re.compile(r"^\s*\d{8}\s*,")


def _read_text(path: Path) -> str:
    """Read a vendor file as text, transparently extracting the single CSV
    member if given a .zip."""
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            members = [n for n in z.namelist() if n.lower().endswith(".csv")]
            if not members:
                raise ValueError(f"no .csv inside zip {path.name}: {z.namelist()}")
            if len(members) > 1:
                logger.warning("zip %s has %d CSVs; using %s",
                               path.name, len(members), members[0])
            return z.read(members[0]).decode("utf-8", errors="replace")
    return path.read_text(encoding="utf-8", errors="replace")


def _load_ff_section(path: Path):
    """Return a DataFrame of the daily section: Date (ISO str) + factor cols."""
    import pandas as pd

    lines = _read_text(path).splitlines()

    # First real data row = first line matching YYYYMMDD,
    first = next((i for i, ln in enumerate(lines) if _DATE_ROW_RE.match(ln)), None)
    if first is None:
        raise ValueError(f"no YYYYMMDD daily rows found in {path.name}")
    if first == 0:
        raise ValueError(f"{path.name}: data starts at line 0, no header row above")

    header = lines[first - 1]
    # Keep consecutive date rows; stop at first non-date line (footer / next section)
    data = []
    for ln in lines[first:]:
        if _DATE_ROW_RE.match(ln):
            data.append(ln)
        else:
            break

    df = pd.read_csv(io.StringIO("\n".join([header] + data)))
    df.columns = [str(c).strip() for c in df.columns]
    # FF header's first cell is empty → pandas names it "Unnamed: 0"
    df = df.rename(columns={df.columns[0]: "Date"})

    df["Date"] = pd.to_datetime(
        df["Date"].astype(str).str.strip(), format="%Y%m%d"
    ).dt.strftime("%Y-%m-%d")

    # Numeric + FF missing sentinels (-99.99 / -999) → NaN
    for c in df.columns:
        if c == "Date":
            continue
        df[c] = pd.to_numeric(df[c], errors="coerce")
        df.loc[df[c] <= -99.0, c] = float("nan")

    logger.info("%s: %d daily rows (%s → %s), cols=%s",
                path.name, len(df), df["Date"].iloc[0], df["Date"].iloc[-1],
                [c for c in df.columns if c != "Date"])
    return df


def build(*, ff5_path: Path, mom_path: Path | None, out_path: Path) -> int:
    try:
        import pandas as pd  # noqa: F401
    except ImportError:
        logger.error("pandas required — pip install pandas")
        return 1

    if not ff5_path.exists():
        logger.error("FF5 file not found: %s", ff5_path)
        return 1

    try:
        ff5 = _load_ff_section(ff5_path)
    except Exception as e:  # noqa: BLE001
        logger.error("failed to parse FF5 file %s: %s", ff5_path.name, e)
        return 1

    merged = ff5
    if mom_path is not None:
        if not mom_path.exists():
            logger.error("momentum file not found: %s", mom_path)
            return 1
        try:
            mom = _load_ff_section(mom_path)
        except Exception as e:  # noqa: BLE001
            logger.error("failed to parse momentum file %s: %s", mom_path.name, e)
            return 1
        # Momentum file has a single factor column → normalize its name to "Mom"
        mom_factor_cols = [c for c in mom.columns if c != "Date"]
        if len(mom_factor_cols) == 1 and mom_factor_cols[0] != "Mom":
            logger.info("renaming momentum column %r → 'Mom'", mom_factor_cols[0])
            mom = mom.rename(columns={mom_factor_cols[0]: "Mom"})
        merged = ff5.merge(mom[["Date", "Mom"]], on="Date", how="inner")
        lost = len(ff5) - len(merged)
        if lost > 0:
            logger.warning(
                "inner join dropped %d FF5 rows with no momentum match "
                "(date-range mismatch between files)", lost)
    else:
        logger.warning(
            "no --mom file: momentum will be 0-filled by the snapshot builder "
            "(R13 momentum beta ≈ 0). Download F-F_Momentum_Factor_daily for it.")

    if len(merged) == 0:
        logger.error("0 rows after merge — check the two files overlap in dates")
        return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(out_path, index=False)
    logger.info("wrote %s (%d rows × %d cols)", out_path, len(merged), len(merged.columns))
    logger.info(
        "next: python scripts/build_factor_returns_snapshot.py "
        "--region USA --csv %s --preset fama_french --date-col Date", out_path)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--ff5", required=True, type=Path,
                   help="F-F 5 Factors daily CSV or ZIP")
    p.add_argument("--mom", type=Path, default=None,
                   help="F-F Momentum Factor daily CSV or ZIP (optional but recommended)")
    p.add_argument("--out", required=True, type=Path,
                   help="output cleaned CSV path")
    args = p.parse_args()
    return build(ff5_path=args.ff5, mom_path=args.mom, out_path=args.out)


if __name__ == "__main__":
    sys.exit(main())
