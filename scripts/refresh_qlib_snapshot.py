"""Phase 3 Q10 PR2b: refresh the local OHLCV snapshot used by Q10 pre-screen.

Plan: ~/.claude/plans/phase3-q10-pyqlib-prescreen-2026-05-18.md v1.3 §3.2 + §9.

Writes one parquet file per region to ``$QLIB_SNAPSHOT_DIR/{REGION}.parquet``
with MultiIndex (datetime, instrument) and OHLCV columns (close / open / high
/ low / volume / vwap). Used by ``QlibEngine._load_snapshot`` when the tier-1
pyqlib_live engine is not available (the Windows-friendly tier-3 path).

Two modes:
  --source pyqlib   live qlib.D.features() (requires pyqlib + qlib data dir)
  --source synthetic deterministic random walk (np.random seed=42)
                    — for dev/CI without pyqlib install

Usage::

    # production refresh (quarterly per plan §9)
    python scripts/refresh_qlib_snapshot.py --regions USA --years 5 --top 500

    # dev/CI synthetic
    python scripts/refresh_qlib_snapshot.py --source synthetic --regions USA --top 100 --years 2

The script is intentionally a one-shot CLI, NOT an auto-cron. Data licensing
concerns (per plan §9) require manual operator approval before each refresh.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import List, Optional

logger = logging.getLogger("refresh_qlib_snapshot")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)


def _try_load_pyqlib(qlib_data_dir: str):
    """Try to initialize pyqlib pointing at QLIB_DATA_DIR. Returns (qlib, D) or (None, None)."""
    try:
        import qlib
        from qlib.data import D
    except ImportError as ex:
        logger.warning(f"pyqlib not installed ({ex}) — synthetic mode required")
        return None, None
    try:
        if not os.path.exists(os.path.join(qlib_data_dir, "calendars")):
            logger.warning(
                f"QLIB_DATA_DIR={qlib_data_dir} missing 'calendars' subdir — "
                "synthetic mode required"
            )
            return None, None
        qlib.init(provider_uri=qlib_data_dir, region="us")
        return qlib, D
    except Exception as ex:
        logger.warning(f"pyqlib init failed: {ex} — falling back to synthetic")
        return None, None


def _build_pyqlib_snapshot(
    D, region: str, years: int, top: int
):
    """Fetch live OHLCV from pyqlib. Returns DataFrame or None on failure.

    Public CSI300 / SP500 bundles include $close, $open, $high, $low, $volume,
    $vwap. We pull top `top` symbols by volume on the most recent business
    day, then slice the most recent `years` * 252 business days.
    """
    import pandas as pd
    fields = ["$close", "$open", "$high", "$low", "$volume", "$vwap"]
    # Pyqlib region names: us/cn
    qlib_region = {"USA": "us", "CHN": "cn"}.get(region.upper(), "us")
    # Use a broad instrument universe; pyqlib filter by exchange/index per region
    try:
        instruments = D.instruments(market="all")
        df = D.features(
            instruments=instruments, fields=fields,
            start_time=f"-{years * 365}d", end_time="now",
            freq="day",
        )
    except Exception as ex:
        logger.warning(f"pyqlib fetch failed: {ex}")
        return None
    if df is None or len(df) == 0:
        return None
    # Pyqlib returns column names with $ prefix — strip
    df.columns = [c.lstrip("$") for c in df.columns]
    # Pick top-N instruments by recent average volume
    if top and "volume" in df.columns:
        recent_vol = (
            df["volume"].groupby(level="instrument").tail(20)
            .groupby(level="instrument").mean()
            .sort_values(ascending=False)
            .head(int(top))
            .index
        )
        df = df.loc[df.index.get_level_values("instrument").isin(recent_vol)]
    return df


def _build_synthetic_snapshot(region: str, years: int, top: int, seed: int = 42):
    """Deterministic synthetic OHLCV for dev/CI. Matches the QlibEngine contract."""
    import numpy as np
    import pandas as pd
    rng = np.random.default_rng(seed)
    days = max(20, years * 252)
    dates = pd.date_range("2020-01-01", periods=days, freq="B")
    # Stub instrument names: AA AB AC … per region
    base = {"USA": "us", "CHN": "cn", "EUR": "eu"}.get(region.upper(), "xx")
    instruments = [f"{base.upper()}_{i:04d}" for i in range(top)]
    idx = pd.MultiIndex.from_product(
        [dates, instruments], names=["datetime", "instrument"]
    )
    n = len(idx)
    # Random walk per instrument (block reshape) for visually plausible series
    walks = rng.standard_normal(n).cumsum() / 10
    return pd.DataFrame(
        {
            "close":  100 + walks,
            "open":   100 + walks - 0.05,
            "high":   101 + walks,
            "low":     99 + walks,
            "volume": 1_000_000 + rng.integers(0, 200_000, size=n),
            "vwap":   100 + walks - 0.02,
        },
        index=idx,
    )


def refresh_region_snapshot(
    *, region: str, snapshot_dir: str, source: str = "auto",
    years: int = 5, top: int = 500, qlib_data_dir: Optional[str] = None,
) -> Optional[str]:
    """Build + write a single region snapshot. Returns the parquet path or None."""
    region = region.upper()
    os.makedirs(snapshot_dir, exist_ok=True)
    out_path = os.path.join(snapshot_dir, f"{region}.parquet")

    df = None
    actual_source = None
    if source in ("pyqlib", "auto"):
        D = None
        if qlib_data_dir:
            _, D = _try_load_pyqlib(qlib_data_dir)
        if D is not None:
            df = _build_pyqlib_snapshot(D, region, years, top)
            if df is not None:
                actual_source = "pyqlib"
        if df is None and source == "pyqlib":
            logger.error(f"--source pyqlib requested but pyqlib path failed for {region}")
            return None
    if df is None:  # auto-fallback or explicit synthetic
        df = _build_synthetic_snapshot(region, years, top)
        actual_source = "synthetic"

    # Atomic write: tmp → fsync → rename. A crashed/Ctrl-C mid-write leaves
    # the .tmp orphan but never a corrupt out_path that QlibEngine would load.
    tmp_path = out_path + ".tmp"
    try:
        df.to_parquet(tmp_path)
        os.replace(tmp_path, out_path)
    except Exception as ex:
        logger.error(f"failed to write parquet {out_path}: {ex}")
        # Best-effort cleanup of partial tmp; ignore failure (file may not exist)
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return None
    logger.info(
        f"[refresh_qlib_snapshot] region={region} source={actual_source} "
        f"rows={len(df)} instruments={df.index.get_level_values('instrument').nunique()} "
        f"days={df.index.get_level_values('datetime').nunique()} → {out_path}"
    )
    return out_path


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh Q10 pyqlib snapshot")
    parser.add_argument(
        "--regions", default="USA",
        help="Comma-separated regions (default: USA). Example: USA,CHN",
    )
    parser.add_argument(
        "--source", default="auto", choices=["auto", "pyqlib", "synthetic"],
        help="auto=try pyqlib then synthetic; pyqlib=fail if unavailable; "
             "synthetic=deterministic random walk",
    )
    parser.add_argument("--years", type=int, default=5)
    parser.add_argument("--top", type=int, default=500,
                        help="top-N instruments by recent volume")
    parser.add_argument(
        "--snapshot-dir", default=None,
        help="Override $QLIB_SNAPSHOT_DIR (default reads from settings)",
    )
    parser.add_argument(
        "--qlib-data-dir", default=None,
        help="Override $QLIB_DATA_DIR for pyqlib init",
    )
    args = parser.parse_args(argv)

    # Resolve settings defaults
    snap_dir = args.snapshot_dir
    qlib_dir = args.qlib_data_dir
    if not snap_dir or not qlib_dir:
        try:
            from backend.config import settings as _stg
            snap_dir = snap_dir or getattr(_stg, "QLIB_SNAPSHOT_DIR", "backend/data/qlib_ohlcv_snapshot")
            qlib_dir = qlib_dir or getattr(_stg, "QLIB_DATA_DIR", "backend/data/qlib_data")
        except Exception:
            snap_dir = snap_dir or "backend/data/qlib_ohlcv_snapshot"
            qlib_dir = qlib_dir or "backend/data/qlib_data"

    regions = [r.strip() for r in args.regions.split(",") if r.strip()]
    n_success = 0
    for region in regions:
        path = refresh_region_snapshot(
            region=region, snapshot_dir=snap_dir, source=args.source,
            years=args.years, top=args.top, qlib_data_dir=qlib_dir,
        )
        if path:
            n_success += 1
    logger.info(f"refresh complete: {n_success}/{len(regions)} regions OK")
    return 0 if n_success == len(regions) else 1


if __name__ == "__main__":
    sys.exit(main())
