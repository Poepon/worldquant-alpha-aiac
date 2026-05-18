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
from typing import Any, Literal, Optional

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

    PR1c ship is the **skeleton**: probe runs, picks a tier (defaulting to
    'disabled' until real eval engines land in PR1d / PR2), and exposes
    ``kind`` for callers. ``evaluate()`` always returns None for now.

    Singleton at module level — engines hold open DataFrame handles + qlib
    init state that's expensive to rebuild per call.
    """

    def __init__(self):
        self.kind: EngineKind = self._probe_engine()
        logger.info(f"[QlibEngine] selected engine={self.kind}")

    def _probe_engine(self) -> EngineKind:
        """Pick the best available engine tier.

        v1.0 (PR1c) always returns 'disabled' — real tier 1/2/3 probes land
        in PR1d (pandas) and PR2 (pyqlib live + snapshot). Soft-fail respects
        ``settings.QLIB_ENGINE_PREFER_PANDAS`` for forced-tier-3 testing.
        """
        # Defensive import — never crash at module load
        try:
            from backend.config import settings as _stg  # noqa: F401
        except Exception as ex:
            logger.debug(f"[QlibEngine] settings import failed: {ex}")
            return "disabled"
        # PR1c skeleton: no real probe yet. PR1d / PR2 add live/snapshot/
        # pandas tiers. Return 'disabled' so prescreen_alpha returns skip
        # for every call until then.
        return "disabled"

    def evaluate(self, qlib_expr: str, region: str, universe: str) -> Optional[Any]:
        """Evaluate a qlib expression on local OHLCV. Returns Series-like or None.

        PR1c skeleton always returns None (engine="disabled"). PR1d wires
        the pandas-snapshot tier; PR2 wires pyqlib live + snapshot.
        """
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
    """Per plan §4.2: compute IC + naive long-short Sharpe from local OHLCV.

    PR1c stub returns (None, None) — PR1d adds the pandas implementation
    (cross-section rank → unit-gross weights → daily PnL → Spearman IC +
    annualized Sharpe × sqrt(252)).

    Deliberately ignores cost / decay / neutralization (plan §4.2 [V1.1-S6]).
    """
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

        # Step 4: compute IC + Sharpe (PR1d implementation)
        ic, sharpe = _compute_ic_and_sharpe(signal, None)
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
