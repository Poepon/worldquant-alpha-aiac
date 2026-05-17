"""Dump Qlib's Alpha158 feature config to backend/data/alpha158_qlib_raw.json.

Usage (developer machine, one-shot — pyqlib NOT in production requirements):

    pip install pyqlib    # temporary install
    python scripts/extract_alpha158.py [--out PATH]
    pip uninstall pyqlib  # cleanup

The output JSON is committed to the repo so production / CI never needs
pyqlib at runtime. Q3-3 (qlib_translator.py) reads this raw dump, applies
BRAIN-DSL translation, and writes alpha158_qlib.json (the production
file consumed by ACADEMIC_PATTERNS module-level merge).

Format of the dump (list of dicts, ordered as Qlib returns):
    [{"name": "KMID", "expr": "($close-$open)/$open"}, ...]

Also runs the CA-1 v1.2 inspect step automatically: prints how many of
the 158 expressions are "raw features" (no Rank/StdNorm/ZScore/Mean wrapper)
vs already-wrapped, so the operator can choose Q3-2 strategy A/B/C.

Plan reference: §3.1 + §3.2 (data source B) + §3.11 (CA-1 inspect)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


WRAPPER_TOKENS = ("Rank", "Mean", "Std", "ZScore", "Resi", "WMA", "EMA", "Quantile")


def extract() -> list[dict]:
    """Return list of {"name": str, "expr": str} from Qlib Alpha158."""
    try:
        from qlib.contrib.data.handler import Alpha158
    except ImportError as ex:
        print(
            "ERROR: pyqlib not installed. Run `pip install pyqlib` first.\n"
            f"       Underlying error: {ex}",
            file=sys.stderr,
        )
        sys.exit(2)

    # Alpha158 has a classmethod get_feature_config() returning
    # (fields: list[str], names: list[str]); both lists are 158-long.
    fields, names = Alpha158.get_feature_config()
    if len(fields) != len(names):
        print(
            f"ERROR: Alpha158 returned mismatched lengths: fields={len(fields)} names={len(names)}",
            file=sys.stderr,
        )
        sys.exit(3)

    return [{"name": n, "expr": e} for n, e in zip(names, fields)]


def inspect_raw_ratio(rows: list[dict]) -> None:
    """CA-1 inspect: print raw-feature vs wrapped ratio + first 10 samples."""
    raw_count = 0
    wrapped_count = 0
    for r in rows:
        if any(tok in r["expr"] for tok in WRAPPER_TOKENS):
            wrapped_count += 1
        else:
            raw_count += 1

    total = len(rows)
    print(f"\nCA-1 inspect — Alpha158 raw-feature vs wrapped ratio:")
    print(f"  total            = {total}")
    print(f"  wrapped (has any of {WRAPPER_TOKENS}) = {wrapped_count} ({100*wrapped_count/total:.1f}%)")
    print(f"  raw feature      = {raw_count} ({100*raw_count/total:.1f}%)")

    if raw_count / total > 0.80:
        suggestion = "→ Strategy B recommended (>80% raw): filter by IC, import ~30-50 best"
    elif raw_count / total > 0.50:
        suggestion = "→ Strategy A recommended (50-80% raw): auto-wrap rank/ts_zscore"
    else:
        suggestion = "→ Strategy C recommended (<30% raw): direct translate"
    print(f"  {suggestion}")

    print(f"\nFirst 10 sample expressions:")
    for r in rows[:10]:
        marker = "RAW" if not any(tok in r["expr"] for tok in WRAPPER_TOKENS) else "WRAPPED"
        print(f"  [{marker:7s}] {r['name']:12s} = {r['expr']}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent.parent / "backend" / "data" / "alpha158_qlib_raw.json"),
        help="Output JSON path",
    )
    args = p.parse_args()

    rows = extract()
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=2)

    print(f"Wrote {len(rows)} rows → {out_path}")

    inspect_raw_ratio(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
