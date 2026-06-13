"""BrainSimulator — run a batch of variants on BRAIN, return packed results.

Stage A's slot management:

  - Each variant acquires one BRAIN sim slot via
    :meth:`BrainAdapter._acquire_sim_slot` (role-aware, cross-process Redis
    counter — USER mode = 3 slots, CONSULTANT = 80). The slot acquire IS
    the parallelism throttle; we run all variants in parallel and let the
    slot semantics back-pressure.
  - Per-sim timeout = ``settings.OPT_SIM_TIMEOUT_SECONDS`` (default 600s).
  - Per-cycle hard budget cap = the ``budget`` argument; the Simulator
    truncates ``variants[:budget]`` BEFORE firing any sims.
  - Every sim spent (success OR error) increments
    ``aiac:opt:sim_budget:{YYYYMMDD}`` so Stage B's budget allocator has
    historical data to calibrate against.

The Simulator does NOT pre-classify or skip; a variant that returns NO
metrics still gets a ``VariantSimResult(error="…")`` row so the
OptimizationRun.sim_budget_used counter reflects truth.

Source: ``docs/optimization_closure_plan_v1_2026-05-28.md`` §6.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from backend.adapters.brain_adapter import BrainAdapter
from backend.config import settings
from backend.services.optimization.protocols import Variant, VariantSimResult


logger = logging.getLogger("optimization.simulator")


# Redis key (UTC date) — Stage B allocator reads this same key to know
# how much budget OPT has already burned today.
_BUDGET_KEY_FMT = "aiac:opt:sim_budget:{date}"


def _today_utc_str() -> str:
    return datetime.utcnow().strftime("%Y%m%d")


def _extract_is_metrics(sim: Dict[str, Any]) -> Dict[str, Any]:
    """Pull the IS metric dict from BRAIN's many response shapes.

    Empirical "shape spotter" for live BRAIN responses — ``sim["is"]`` is the most
    common, ``sim["metrics"]`` is occasionally seen, and the metric scalars
    are sometimes nested one more level under ``"metrics"``.
    """
    is_m = sim.get("is") or sim.get("metrics") or {}
    if isinstance(is_m, dict) and "sharpe" not in is_m and "metrics" in is_m:
        is_m = is_m["metrics"]
    return is_m if isinstance(is_m, dict) else {}


def _all_checks_passed(sim: Dict[str, Any]) -> bool:
    """True iff every BRAIN check entry's ``result`` ≠ ``"FAIL"``.

    Empty/missing checks list is treated as "no information" → True
    (a sim without checks should not be rejected just on missing data).
    The WinnerSelector still enforces the threshold band on raw metrics,
    so an empty-checks pass-through to "winner" still has to clear the
    numeric gates.
    """
    is_m = _extract_is_metrics(sim)
    checks = is_m.get("checks") or sim.get("checks") or []
    if not isinstance(checks, list):
        return True
    for c in checks:
        if isinstance(c, dict) and str(c.get("result", "")).upper() == "FAIL":
            return False
    return True


def _extract_sub_univ_sharpe(sim: Dict[str, Any]) -> Optional[float]:
    """Find the LOW_SUB_UNIVERSE_SHARPE check value, if present."""
    is_m = _extract_is_metrics(sim)
    checks = is_m.get("checks") or sim.get("checks") or []
    if not isinstance(checks, list):
        return None
    for c in checks:
        if (
            isinstance(c, dict)
            and c.get("name") == "LOW_SUB_UNIVERSE_SHARPE"
        ):
            v = c.get("value")
            return float(v) if v is not None else None
    return None


def _extract_brain_alpha_id(sim: Dict[str, Any]) -> Optional[str]:
    """BRAIN response carries the new alpha id in one of three places."""
    if not isinstance(sim, dict):
        return None
    return (
        sim.get("alpha_id")
        or sim.get("id")
        or (sim.get("alpha") or {}).get("id")
    )


class BrainSimulator:
    """Stage A Simulator. Takes a single ``BrainAdapter`` instance (injected
    so tests can swap in :class:`MockBrainAdapter`)."""

    def __init__(self, brain: BrainAdapter):
        self.brain = brain

    async def run_batch(
        self, variants: List[Variant], budget: int
    ) -> List[VariantSimResult]:
        if not variants:
            return []
        to_run = variants[: max(0, int(budget))]
        if not to_run:
            return []
        coros = [self._run_one(v) for v in to_run]
        results = await asyncio.gather(*coros, return_exceptions=False)
        # Track total spend (every attempt counts, error or not — Stage B
        # allocator wants the truth).
        try:
            await self._record_budget_spend(len(to_run))
        except Exception as ex:
            logger.warning(
                "[BrainSimulator] budget counter increment failed (non-fatal): %s",
                ex,
            )
        return list(results)

    async def _run_one(self, variant: Variant) -> VariantSimResult:
        # NOTE (2026-06-08, double-acquire root fix): do NOT acquire a BRAIN sim
        # slot here. simulate_alpha (brain_adapter) already acquires + shield-
        # releases ONE slot for its full POST→poll→terminal lifecycle. A second
        # acquire here made every sim hold 2 of the 3 USER slots, so any >=2
        # concurrent run_batch sims saturated the limit and each inner acquire
        # deadlocked → all 600s sim_timeout (2026-06-07 regime run: 0/23). One
        # acquire (simulate_alpha's) = 1 slot/sim → run_batch concurrency works.
        # See reference_brainsim_double_acquire_deadlock.
        timeout = float(getattr(settings, "OPT_SIM_TIMEOUT_SECONDS", 600))
        try:
            sim = await asyncio.wait_for(
                self.brain.simulate_alpha(
                    expression=variant.expression, **variant.settings
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return _err(variant, f"sim_timeout({timeout:.0f}s)")
        except Exception as ex:  # noqa: BLE001 — any sim error becomes a row
            return _err(variant, f"sim_exception: {type(ex).__name__}: {ex}")

        if not isinstance(sim, dict):
            return _err(variant, f"sim_response_not_dict({type(sim).__name__})")
        # simulate_alpha signals slot-timeout / 429 / auth / creation-failed as
        # {"success": False, "error": ...}; surface that reason instead of parsing
        # an error dict as metrics (which would yield a phantom sharpe=None row).
        if sim.get("success") is False:
            return _err(variant, str(sim.get("error") or "sim_failed"))

        m = _extract_is_metrics(sim)
        return VariantSimResult(
            variant=variant,
            sim_response=sim,
            sharpe=_safe_float(m.get("sharpe")),
            fitness=_safe_float(m.get("fitness")),
            turnover=_safe_float(m.get("turnover")),
            margin=_safe_float(m.get("margin")),
            subuniv=_extract_sub_univ_sharpe(sim),
            brain_alpha_id=_extract_brain_alpha_id(sim),
            checks_passed=_all_checks_passed(sim),
            self_corr=None,  # filled in by Persister, not here
            error=None,
        )

    async def _record_budget_spend(self, n: int) -> None:
        if n <= 0:
            return
        key = _BUDGET_KEY_FMT.format(date=_today_utc_str())
        r = await BrainAdapter._get_slot_redis()
        # 48h TTL so today + yesterday are both visible; the allocator
        # reads the current day only.
        await r.incrby(key, int(n))
        await r.expire(key, 48 * 3600)


def _err(variant: Variant, message: str) -> VariantSimResult:
    return VariantSimResult(
        variant=variant,
        sim_response={},
        sharpe=None,
        fitness=None,
        turnover=None,
        margin=None,
        subuniv=None,
        brain_alpha_id=None,
        checks_passed=False,
        self_corr=None,
        error=message,
    )


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None
