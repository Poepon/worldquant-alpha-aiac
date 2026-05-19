# Factor Returns Snapshot

Static per-region factor-returns snapshots consumed by
``backend/services/factor_lens_service.py`` (Phase 4 R13 B2 — plan v5
§6.9 / v2 §4.6).

## Schema

One file per region:
``{region_lowercase}.parquet`` (e.g. ``usa.parquet``, ``chn.parquet``).

| Column | Type | Description |
|--------|------|-------------|
| (index) | DatetimeIndex | Trading day |
| `size` | float64 | Daily return of the size factor |
| `value` | float64 | Daily return of the value factor |
| `momentum` | float64 | Daily return of the momentum factor |
| `quality` | float64 | Daily return of the quality factor |
| `low_vol` | float64 | Daily return of the low-vol factor |

Each cell = daily return (decimal, e.g. 0.0023 = +0.23%). Sign
convention: positive = long-leg outperforms short-leg.

## Coverage requirements

- ≥ `FACTOR_LENS_OLS_LOOKBACK_DAYS` (default 504 ≈ 2 years) of data
- Sorted DatetimeIndex, monotonic ascending
- All 5 factors present (extras OK — `FACTOR_LENS_FACTORS` filters)

## Refresh cadence

Operator responsibility — monthly via internal script (TBD,
fast-follow). Stale > 90d → `/ops/r13/snapshot-stale-check` surfaces
warning.

## Region coverage (Phase 4 Sprint 2 targets)

- `usa.parquet` — required for Sprint 2 GO
- `chn.parquet` — required for Sprint 2 GO
- `jpn.parquet` — best-effort
- `eur.parquet` — best-effort
- `hkg.parquet` — best-effort

When a region snapshot is missing, ``load_factor_returns(region)``
returns ``None`` and the evaluation node soft-skips R13 decomposition
for that alpha (stamps ``_r13_residual_sharpe_mode = "no_snapshot"``).

## Source

Operator may construct these from:
- **Fama-French Data Library** (USA 5-factor) —
  https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html
- **Refinitiv StarMine / Compustat** factor return monthly snapshots
  resampled to daily via equal-weight cross-sectional regression
- **AQR Data Sets** (USA + global) —
  https://www.aqr.com/Insights/Datasets

Or build internally from BRAIN universe data using a 5-factor regression
on Fama-French + momentum lineage (see plan v3 §4.6 references).

## Calibration

Once snapshots exist + R13 is in shadow mode for ≥7d ≥30 alphas
worth of OLS decomposition, run:

    python scripts/calibrate_r13_threshold.py --region USA \\
        --output docs/r13_threshold_recommendation.json

(Script TBD — fast-follow.)

## Why static, not live

- AIAC's primary need is *cross-sectional* style exposure, not
  high-frequency factor timing
- Style factors decay slowly (months to quarters); 1-month staleness
  introduces << 5% relative error in residual sharpe
- Maintaining a live factor model would add a dependency on a real-
  time data feed not currently in the AIAC stack
- Mirrors the approach in Citadel's risk monitor + Two Sigma's
  internal factor lens — they refresh weekly to monthly
