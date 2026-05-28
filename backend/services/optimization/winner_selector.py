"""WinnerSelector — filter VariantSimResults against the delay-aware band.

Stage A picks "winner" = a sim result that clears every BRAIN gate on the
right delay band:

  - sharpe ≥ band["sharpe_min"]
  - fitness ≥ band["fitness_min"]
  - turnover_min ≤ turnover ≤ turnover_max
  - checks_passed (sub-univ + concentrated + self-corr — already AND'd by
    the Simulator from BRAIN's checks list)
  - no Simulator-side error

The delay split matters: delay-0 BRAIN gates are stricter (sharpe ≥ 2.0
vs delay-1's 1.5 — alpha 15621 empirical, commit ``b8a9560``). Caller must
pass the parent alpha's actual delay; we never default-fall to 1 silently.

Source: ``docs/optimization_closure_plan_v1_2026-05-28.md`` §6.
"""
from __future__ import annotations

from typing import List

from backend.config import settings
from backend.services.optimization.protocols import VariantSimResult


class WinnerSelector:
    """Stateless picker. Single instance is fine."""

    def pick(
        self, results: List[VariantSimResult], delay: int
    ) -> List[VariantSimResult]:
        band = settings.eval_thresholds(int(delay))
        sharpe_min = float(band["sharpe_min"])
        fitness_min = float(band["fitness_min"])
        turn_min = float(band["turnover_min"])
        turn_max = float(band["turnover_max"])

        winners: List[VariantSimResult] = []
        for r in results:
            if r.error:
                continue
            if not r.checks_passed:
                continue
            if r.sharpe is None or r.sharpe < sharpe_min:
                continue
            if r.fitness is None or r.fitness < fitness_min:
                continue
            if r.turnover is None:
                continue
            if not (turn_min <= r.turnover <= turn_max):
                continue
            winners.append(r)
        return winners
