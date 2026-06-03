"""RobustnessFilter — deflate settings-sweep winners against multiple-testing
and lone-peak overfitting.

This is the "止血" fix from the 2026-06-03 methodology review + industry survey
(`docs/industry_alpha_optimization_survey_2026-06-03.md`, L3 抗过拟合选择). A
settings sweep generates N variants of ONE parent and crowns the best one that
clears the band. That is the textbook definition of backtest overfitting
(Bailey, Borwein, López de Prado & Zhu, *Pseudo-Mathematics and Financial
Charlatanism*, 2014): the observed best Sharpe MUST be deflated for the N
trials, and shown to sit on a stable PLATEAU rather than a lone spike, before
it is trusted.

Two gates, applied AFTER WinnerSelector, BEFORE persistence:

  1. **DEFLATION (expected-max-Sharpe under the null, SR0)** — among the cycle's
     N variant Sharpes, the winner must beat the Sharpe that the LUCKIEST of N
     zero-skill trials would post given their spread::

         SR0 = sqrt(Var[SR]) · ((1-γ)·Φ⁻¹(1-1/N) + γ·Φ⁻¹(1-1/(N·e)))

     (Bailey & López de Prado, *The Deflated Sharpe Ratio*, JPM 2014; γ =
     Euler-Mascheroni.) A winner whose Sharpe ≤ SR0 is indistinguishable from
     the best of N coin-flips → rejected.

  2. **PLATEAU** — a winner's neutralization region must have ≥ 2 cells near the
     bar (≥ 1 same-neut sibling with sharpe ≥ sharpe_min − plateau_band). A
     single isolated high cell is a fragile spike, not a robust setting.

Pure / dependency-free (stdlib ``statistics.NormalDist`` for Φ⁻¹) — unit-testable
per the repo's standalone-analytics-module convention. Effective-N deflation for
the within-parent correlation of variants (1/ρ shrink) is a documented future
refinement; using the raw N here is intentionally CONSERVATIVE (SR0 is a bit
higher → harder to pass), which suits a stop-the-bleeding filter.
"""
from __future__ import annotations

import logging
import math
from statistics import NormalDist
from typing import Any, Dict, List, Optional, Tuple

from backend.config import settings
from backend.services.optimization.protocols import VariantSimResult


logger = logging.getLogger("optimization.robustness")

_EULER_GAMMA = 0.5772156649015329
_NORM = NormalDist(0.0, 1.0)


def expected_max_sharpe(sharpes: List[Optional[float]]) -> float:
    """SR0 — expected maximum Sharpe of N zero-skill trials whose Sharpe
    estimates have the given cross-trial variance (Bailey-López de Prado).

    Returns 0.0 when N < 2 (no multiple-testing problem) or the variance is ~0
    (all trials identical → no dispersion for luck to exploit).
    """
    vals = [
        float(s) for s in sharpes
        if s is not None and not math.isnan(float(s))
    ]
    n = len(vals)
    if n < 2:
        return 0.0
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / (n - 1)
    if var <= 0:
        return 0.0
    std = math.sqrt(var)
    # Inverse-normal quantiles; clamp args strictly inside (0,1) for tiny N.
    q1 = _NORM.inv_cdf(min(1 - 1e-12, max(1e-12, 1.0 - 1.0 / n)))
    q2 = _NORM.inv_cdf(min(1 - 1e-12, max(1e-12, 1.0 - 1.0 / (n * math.e))))
    return std * ((1.0 - _EULER_GAMMA) * q1 + _EULER_GAMMA * q2)


def _variant_neut(r: VariantSimResult) -> Optional[str]:
    try:
        return str(r.variant.settings.get("neutralization"))
    except Exception:  # noqa: BLE001 — defensive against odd variant shapes
        return None


def plateau_ok(
    winner: VariantSimResult,
    all_results: List[VariantSimResult],
    *,
    sharpe_min: float,
    plateau_band: float,
) -> Tuple[bool, str]:
    """A winner sits on a plateau if ≥ 1 SAME-neutralization sibling (excluding
    itself, no sim error, measurable sharpe) also reaches
    ``sharpe ≥ sharpe_min − plateau_band``.

    Returns ``(ok, reason)``. Unassessable (no measurable same-neut sibling) →
    ``(True, "plateau_unassessed")`` — we don't drop a genuine winner on a thin
    grid; the band gate already vouched for it.
    """
    neut = _variant_neut(winner)
    floor = sharpe_min - plateau_band
    siblings = [
        r for r in all_results
        if r is not winner and not r.error and _variant_neut(r) == neut
        and r.sharpe is not None
    ]
    if not siblings:
        return True, "plateau_unassessed"
    best_sib = max(s.sharpe for s in siblings)
    if best_sib >= floor:
        return True, f"plateau_ok(best_sib={best_sib:.2f}>={floor:.2f})"
    return False, f"lone_spike(best_sib={best_sib:.2f}<{floor:.2f})"


class RobustnessFilter:
    """Deflate WinnerSelector output. Stateless; a single instance is fine.

    ``apply`` returns ``(survivors, rejections)`` — survivors are the winners
    that pass both gates; rejections carry ``{tag, sharpe, reason}`` for
    cycle telemetry (persisted to ``optimization_runs.cycle_metadata``).
    """

    def __init__(
        self,
        *,
        plateau_band: Optional[float] = None,
        require_plateau: bool = True,
        require_deflation: bool = True,
    ):
        self.plateau_band = (
            float(getattr(settings, "OPT_PLATEAU_BAND", 0.15))
            if plateau_band is None else float(plateau_band)
        )
        self.require_plateau = bool(require_plateau)
        self.require_deflation = bool(require_deflation)

    def apply(
        self,
        winners: List[VariantSimResult],
        all_results: List[VariantSimResult],
        delay: int,
    ) -> Tuple[List[VariantSimResult], List[Dict[str, Any]]]:
        band = settings.eval_thresholds(int(delay))
        sharpe_min = float(band["sharpe_min"])
        # Deflation baseline uses the spread of ALL sim'd variants this cycle
        # (the N trials), not just the winners.
        sr0 = expected_max_sharpe([
            r.sharpe for r in all_results if not r.error and r.sharpe is not None
        ])

        survivors: List[VariantSimResult] = []
        rejections: List[Dict[str, Any]] = []
        for w in winners:
            tag = getattr(getattr(w, "variant", None), "tag", "?")
            if (
                self.require_deflation
                and w.sharpe is not None
                and w.sharpe <= sr0
            ):
                rejections.append({
                    "tag": tag,
                    "sharpe": round(float(w.sharpe), 4),
                    "reason": "failed_deflation",
                    "sr0": round(sr0, 4),
                })
                continue
            if self.require_plateau:
                ok, why = plateau_ok(
                    w, all_results,
                    sharpe_min=sharpe_min, plateau_band=self.plateau_band,
                )
                if not ok:
                    rejections.append({
                        "tag": tag,
                        "sharpe": round(float(w.sharpe), 4) if w.sharpe is not None else None,
                        "reason": why,
                    })
                    continue
            survivors.append(w)

        if rejections:
            logger.info(
                "[RobustnessFilter] delay=%s SR0=%.3f kept=%d/%d rejected=%s",
                delay, sr0, len(survivors), len(winners),
                [r["reason"] for r in rejections],
            )
        return survivors, rejections
