"""Phase 3 Q10: pyqlib local pre-screen (Multi-Fidelity Layer 0).

Plan: ~/.claude/plans/phase3-q10-pyqlib-prescreen-2026-05-18.md v1.3 §3-§4.

This module provides ``prescreen_alpha(brain_expr, region, universe)`` —
the cheap local Sharpe/IC layer that sits in front of BRAIN simulate. Its
job is to **drop obvious losers** so BRAIN sim quota is conserved.

Architecture (3-tier engine degrade, plan §3.3):

    tier 1 — pyqlib_live:    real qlib installed + data dir present
    tier 2 — pyqlib_snapshot: qlib installed, no data, use bundled snapshot
    tier 3 — pandas_snapshot: no qlib at all, pure-pandas evaluator
    tier 4 — disabled:        nothing works → all prescreens skip

PR1c scope (this file): PrescreenResult dataclass + QlibEngine probe
skeleton + ``prescreen_alpha`` public entry. **v1.0 only implements the
disabled tier** — `QlibEngine` always reports ``kind="disabled"`` so
``prescreen_alpha`` always returns ``verdict="skip"``. Tier 1/2/3 eval
arrive in PR1d (pandas engine) and PR2 (pyqlib live + snapshot wiring).

Soft-fail philosophy: ``prescreen_alpha`` is contractually never-raises.
Any exception → verdict="skip" with a skip_reason string, so caller can
proceed to BRAIN (no opinion).

Cache: ``brain_to_qlib`` is already memoized at translator level
(``functools.lru_cache(1024)``); this module does not add another cache.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Literal, Optional

from backend.qlib_translator import brain_to_qlib

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PrescreenResult dataclass
# ---------------------------------------------------------------------------

Verdict = Literal["pass", "reject", "skip"]
EngineKind = Literal["pyqlib_live", "pyqlib_snapshot", "pandas_snapshot", "disabled"]


@dataclass
class PrescreenResult:
    """Output of ``prescreen_alpha``. Pure data, no behavior."""

    brain_expression: str
    region: str
    universe: str

    verdict: Verdict = "skip"          # pass / reject / skip
    reject_reason: Optional[str] = None  # populated when verdict="reject"
    skip_reason: Optional[str] = None    # 'untranslatable' / 'engine_disabled' / 'timeout' / 'metrics_nan'
    translation_error: Optional[str] = None  # full err msg if translator failed

    qlib_expression: Optional[str] = None
    local_sharpe: Optional[float] = None
    local_ic: Optional[float] = None
    engine_kind: EngineKind = "disabled"
    elapsed_ms: int = 0

    # Mode at call time — caller (e.g. node_simulate Q10 block) writes this
    # from settings.QLIB_PRESCREEN_MODE so post-shadow calibration can do
    # cohort analysis from qlib_prescreen_log rows. Default 'shadow' is the
    # safest assumption.
    mode_at_call: str = "shadow"

    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# QlibEngine — 3-tier degrade probe (skeleton)
# ---------------------------------------------------------------------------


class QlibEngine:
    """Abstraction over pyqlib availability — 3 fallback tiers.

    PR1e wires the **tier-3 pandas-snapshot** path: when a parquet snapshot
    file exists under ``QLIB_SNAPSHOT_DIR/{region}.parquet``, the engine
    loads it lazily and dispatches ``evaluate()`` calls to
    ``backend.qlib_prescreen_pandas_engine.evaluate_pandas``. Tier-1
    (pyqlib_live) + tier-2 (pyqlib_snapshot) still skeletons until PR2.

    ``QLIB_ENGINE_PREFER_PANDAS=True`` forces the pandas tier even if
    pyqlib were available — used by tests to deterministically pin the
    fallback path.

    Singleton at module level — DataFrames are expensive to reload.
    """

    def __init__(self):
        # Per-region snapshot cache: {region_upper: pd.DataFrame}. Loaded
        # lazily on first evaluate() for that region so multi-region jobs
        # don't pay for unused regions.
        self._snapshots: Dict[str, Any] = {}
        self.kind: EngineKind = self._probe_engine()
        logger.info(f"[QlibEngine] selected engine={self.kind}")

    def _probe_engine(self) -> EngineKind:
        """Pick the best available engine tier.

        Order (PR1e + plan §3.3):
          1. tier-1 pyqlib_live (deferred to PR2)
          2. tier-2 pyqlib_snapshot (deferred to PR2)
          3. tier-3 pandas_snapshot if QLIB_SNAPSHOT_DIR exists with
             ANY ``{region}.parquet`` OR if QLIB_ENGINE_PREFER_PANDAS=True
          4. tier-4 disabled — nothing works
        """
        try:
            from backend.config import settings as _stg
        except Exception as ex:
            logger.debug(f"[QlibEngine] settings import failed: {ex}")
            return "disabled"

        # PR2 will add tier-1/2 probes here.

        # Tier-3 probe: pandas snapshot directory with at least one region file
        # OR explicit forced-pandas mode for testing.
        snap_dir = getattr(_stg, "QLIB_SNAPSHOT_DIR", None)
        force_pandas = bool(getattr(_stg, "QLIB_ENGINE_PREFER_PANDAS", False))
        if force_pandas:
            logger.info("[QlibEngine] QLIB_ENGINE_PREFER_PANDAS=True forces tier-3")
            return "pandas_snapshot"
        if snap_dir:
            try:
                import os
                if os.path.isdir(snap_dir):
                    files = [f for f in os.listdir(snap_dir) if f.endswith(".parquet")]
                    if files:
                        logger.info(
                            f"[QlibEngine] tier-3 pandas_snapshot ready ({len(files)} regions)"
                        )
                        return "pandas_snapshot"
            except Exception as ex:
                logger.debug(f"[QlibEngine] snapshot dir probe failed: {ex}")

        return "disabled"

    def _load_snapshot(self, region: str) -> Optional[Any]:
        """Lazily load + cache a region snapshot. Returns DataFrame or None.

        Snapshot file convention: ``{QLIB_SNAPSHOT_DIR}/{region_upper}.parquet``
        with MultiIndex (datetime, instrument) and OHLCV columns
        (close/open/high/low/volume/vwap). Caller's expression must
        reference only columns present in the snapshot.
        """
        key = (region or "").upper()
        if key in self._snapshots:
            return self._snapshots[key]
        try:
            from backend.config import settings as _stg
            import os
            snap_dir = getattr(_stg, "QLIB_SNAPSHOT_DIR", None)
            if not snap_dir:
                return None
            path = os.path.join(snap_dir, f"{key}.parquet")
            if not os.path.exists(path):
                return None
            import pandas as pd
            df = pd.read_parquet(path)
            # Memory-map friendly when shared across Celery workers (plan §3.2
            # [V1.1-S4]). Pandas read_parquet does not expose memory_map but
            # PyArrow inherits OS page cache anyway when the file is mmap-able.
            self._snapshots[key] = df
            return df
        except Exception as ex:
            logger.warning(f"[QlibEngine] snapshot load failed for {region}: {ex}")
            return None

    def evaluate(self, qlib_expr: str, region: str, universe: str) -> Optional[Any]:
        """Evaluate a qlib expression on local OHLCV. Returns Series-like or None.

        PR1e: tier-3 'pandas_snapshot' delegates to ``evaluate_pandas``.
        Tier-1/2 still skeleton (PR2).
        """
        if self.kind != "pandas_snapshot":
            return None
        df = self._load_snapshot(region)
        if df is None or len(df) == 0:
            return None
        try:
            from backend.qlib_prescreen_pandas_engine import evaluate_pandas
            return evaluate_pandas(qlib_expr, df)
        except Exception as ex:
            logger.warning(f"[QlibEngine] pandas evaluate failed: {ex}")
            return None

    def get_forward_returns(self, region: str) -> Optional[Any]:
        """Compute 1-day forward returns from snapshot $close (PR1f).

        Returns a pd.Series indexed by (datetime, instrument), per-instrument
        forward 1-day return = close.shift(-1) / close - 1. Cached per
        region after first compute. None when snapshot missing.
        """
        key = (region or "").upper()
        cache_key = f"__fwd_ret_{key}"
        if cache_key in self._snapshots:
            return self._snapshots[cache_key]
        df = self._load_snapshot(region)
        if df is None or "close" not in df.columns:
            return None
        try:
            grouped = df["close"].groupby(level="instrument", group_keys=False)
            shifted = grouped.shift(-1)
            fwd = (shifted / df["close"]) - 1.0
            self._snapshots[cache_key] = fwd
            return fwd
        except Exception as ex:
            logger.warning(f"[QlibEngine] forward returns compute failed: {ex}")
            return None


# Module-level singleton — built lazily on first prescreen_alpha call to
# avoid paying the probe cost during test collection.
_ENGINE_SINGLETON: Optional[QlibEngine] = None


def _get_engine() -> QlibEngine:
    global _ENGINE_SINGLETON
    if _ENGINE_SINGLETON is None:
        _ENGINE_SINGLETON = QlibEngine()
    return _ENGINE_SINGLETON


def _reset_engine_for_test() -> None:
    """Tests use this to force a fresh probe (e.g., after monkeypatching settings)."""
    global _ENGINE_SINGLETON
    _ENGINE_SINGLETON = None


# ---------------------------------------------------------------------------
# Metric computation (PR1d will use this; skeleton kept simple for now)
# ---------------------------------------------------------------------------


def _compute_ic_and_sharpe(
    signal_series: Any, forward_returns: Any
) -> tuple[Optional[float], Optional[float]]:
    """Per plan §4.2 — compute IC + naive long-short Sharpe from local OHLCV.

    PR1f wires the pandas implementation:
      - Cross-section rank: ``rank = signal.groupby('datetime').rank(pct=True) - 0.5``
      - Unit-gross weights: ``w = rank / rank.abs().sum()`` per day
      - Daily PnL: ``pnl[t] = sum(w[t-1] * fwd_return[t])``
      - Annualized Sharpe = ``mean(pnl) / std(pnl) * sqrt(252)``
      - IC = mean across days of daily Spearman corr(signal_rank, fwd_return)

    Deliberately ignores cost / decay / neutralization / turnover (plan
    §4.2 [V1.1-S6]). The floor is calibrated for THIS approximation, not
    BRAIN-equivalent Sharpe.

    Returns (ic, sharpe) — either may be None if not enough data points
    or numerical degeneration.
    """
    if signal_series is None or forward_returns is None:
        return None, None
    try:
        import numpy as np
        import pandas as pd
    except ImportError:
        return None, None
    try:
        # Align on shared index — both should be MultiIndex (datetime, instrument)
        joined = pd.concat(
            [signal_series.rename("sig"), forward_returns.rename("fwd")],
            axis=1, join="inner",
        ).dropna()
        if len(joined) < 5:  # need at least a handful of (date, instrument) pairs
            return None, None

        # Cross-section pct rank centered on 0 per datetime
        sig_rank = joined.groupby(level="datetime")["sig"].rank(pct=True) - 0.5

        # Unit-gross weights — long-short normalized per day
        def _normalize(s):
            denom = s.abs().sum()
            return s / denom if denom > 0 else s * 0.0

        weights = sig_rank.groupby(level="datetime").transform(_normalize)
        # Lag weights by one day per instrument so we trade on t-1 signal
        # against t fwd_return (avoid look-ahead bias).
        weights_lagged = weights.groupby(level="instrument").shift(1)
        pnl_contrib = weights_lagged * joined["fwd"]
        daily_pnl = pnl_contrib.groupby(level="datetime").sum().dropna()
        if len(daily_pnl) < 2 or daily_pnl.std(ddof=0) == 0:
            return None, None
        sharpe = float(daily_pnl.mean() / daily_pnl.std(ddof=0) * np.sqrt(252))

        # Spearman IC — per-day rank-correlation between signal and forward
        def _ic_one_day(group):
            if len(group) < 3:
                return np.nan
            return group["sig"].corr(group["fwd"], method="spearman")

        ic_per_day = joined.groupby(level="datetime", group_keys=False).apply(_ic_one_day)
        ic_per_day = ic_per_day.dropna()
        if len(ic_per_day) == 0:
            ic = None
        else:
            ic = float(ic_per_day.mean())

        # Sanitize NaN / inf
        if not np.isfinite(sharpe):
            sharpe = None
        if ic is not None and not np.isfinite(ic):
            ic = None
        return ic, sharpe
    except Exception as ex:
        logger.warning(f"[Q10 metrics] _compute_ic_and_sharpe failed: {ex}")
        return None, None


# ---------------------------------------------------------------------------
# prescreen_alpha — public entry
# ---------------------------------------------------------------------------


async def prescreen_alpha(
    brain_expr: str,
    region: str = "USA",
    universe: str = "TOP3000",
    *,
    sharpe_floor: Optional[float] = None,
    ic_floor: Optional[float] = None,
    mode: Optional[str] = None,
) -> PrescreenResult:
    """Compute approximate Sharpe + IC for a BRAIN expression on local OHLCV.

    Plan §4.1 — returns ``PrescreenResult`` with verdict in:
        - "pass":   passed floor → proceed to BRAIN
        - "reject": below floor → skip BRAIN (only when caller is in 'hard' mode)
        - "skip":   untranslatable / engine_disabled / numerical error → proceed to BRAIN

    Soft-fail contract: this function NEVER raises. Any exception →
    verdict="skip" with skip_reason populated.

    PR1c ship: engine is always 'disabled' so verdict is always 'skip'
    with skip_reason 'engine_disabled' (unless translation fails first,
    in which case skip_reason is 'untranslatable'). PR1d adds the eval
    path so passes / rejects start happening.

    Args:
        brain_expr: the BRAIN DSL expression to pre-screen
        region: BRAIN region (USA / CHN / EUR / ASI / GLB)
        universe: universe label (TOP3000 / TOP1000 / TOP500 / ...)
        sharpe_floor: override settings.QLIB_PRESCREEN_SHARPE_FLOOR
        ic_floor: override settings.QLIB_PRESCREEN_IC_FLOOR
        mode: 'shadow' / 'soft' / 'hard' — defaults to settings.QLIB_PRESCREEN_MODE

    Returns:
        PrescreenResult with verdict + reasons + metrics + engine_kind +
        elapsed_ms populated.
    """
    t0 = time.perf_counter()
    try:
        from backend.config import settings as _stg
    except Exception:
        _stg = None  # type: ignore

    effective_mode = mode or (getattr(_stg, "QLIB_PRESCREEN_MODE", "shadow") if _stg else "shadow")
    result = PrescreenResult(
        brain_expression=brain_expr or "",
        region=region,
        universe=universe,
        mode_at_call=effective_mode,
    )

    # Defensive — never raises beyond this point
    try:
        # Step 1: translate BRAIN → qlib (never raises; returns None on untranslatable)
        qlib_expr = brain_to_qlib(brain_expr or "", region=region)
        if qlib_expr is None:
            result.verdict = "skip"
            result.skip_reason = "untranslatable"
            result.translation_error = "translator returned None"
            return _stamp_elapsed(result, t0)
        result.qlib_expression = qlib_expr

        # Step 2: engine probe (singleton)
        engine = _get_engine()
        result.engine_kind = engine.kind
        if engine.kind == "disabled":
            result.verdict = "skip"
            result.skip_reason = "engine_disabled"
            return _stamp_elapsed(result, t0)

        # Step 3+: real eval lands in PR1d (pandas) / PR2 (pyqlib).
        # For PR1c, even if a non-disabled engine were somehow returned,
        # the evaluate() stub returns None → treat as skip:empty_series.
        signal = engine.evaluate(qlib_expr, region, universe)
        if signal is None:
            result.verdict = "skip"
            result.skip_reason = "empty_series"
            return _stamp_elapsed(result, t0)

        # Step 4: compute IC + Sharpe (PR1f implementation)
        fwd_returns = engine.get_forward_returns(region)
        ic, sharpe = _compute_ic_and_sharpe(signal, fwd_returns)
        result.local_ic = ic
        result.local_sharpe = sharpe

        # Step 5: verdict (PR1d active; PR1c never reaches here)
        _sf = sharpe_floor if sharpe_floor is not None else (
            float(getattr(_stg, "QLIB_PRESCREEN_SHARPE_FLOOR", 0.3)) if _stg else 0.3
        )
        _if = ic_floor if ic_floor is not None else (
            float(getattr(_stg, "QLIB_PRESCREEN_IC_FLOOR", 0.005)) if _stg else 0.005
        )
        if sharpe is None or ic is None:
            result.verdict = "skip"
            result.skip_reason = "metrics_nan"
        elif sharpe < _sf or abs(ic) < _if:
            result.verdict = "reject"
            result.reject_reason = (
                f"sharpe={sharpe:.3f}<{_sf} OR |ic|={abs(ic):.4f}<{_if}"
            )
        else:
            result.verdict = "pass"
        return _stamp_elapsed(result, t0)

    except Exception as ex:  # pragma: no cover — contract: never raises
        logger.warning(f"[Q10 prescreen] unexpected exception, soft-falling to skip: {ex}")
        result.verdict = "skip"
        result.skip_reason = f"eval_error:{type(ex).__name__}"
        return _stamp_elapsed(result, t0)


def _stamp_elapsed(result: PrescreenResult, t0: float) -> PrescreenResult:
    result.elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return result


__all__ = [
    "PrescreenResult",
    "QlibEngine",
    "prescreen_alpha",
    "_reset_engine_for_test",  # exposed for tests; underscored to discourage prod use
]
