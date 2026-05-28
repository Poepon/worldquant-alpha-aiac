"""Optimization-closure Stage A beat task — scan near-gate alphas and run cycles.

Fired by celery_beat every ``OPT_BEAT_INTERVAL_HOURS`` (default 6h). One
beat firing:

  1. Returns early with ``{"skipped":"flag_off"}`` if
     ``ENABLE_OPTIMIZATION_LOOP`` is False (this is the kill switch — flag
     stays default OFF until operator opts in).
  2. Selects ``OPT_CANDIDATES_PER_CYCLE`` near-gate alphas via the SQL in
     :func:`_select_near_gate_candidates`.
  3. For each candidate, opens a fresh BrainAdapter + CorrelationService,
     instantiates the full Stage A pipeline (SettingsSweepGenerator →
     BrainSimulator → WinnerSelector → Persister → StageASubmitPolicy),
     and calls :meth:`OptimizationService.run_one_cycle` with a per-cycle
     sim budget = ``OPT_DAILY_SIM_BUDGET / 4 / OPT_CANDIDATES_PER_CYCLE``
     (defaults: 400/4/10 = 10 sims per cycle, i.e. one full
     SettingsSweepGenerator batch).
  4. Each cycle commits its own DB transaction so a mid-batch failure
     doesn't roll back earlier cycles.

The task NEVER auto-submits (Stage A SubmitPolicy returns ``"queue"`` only).
Winners land in ``ops/submit-backlog`` for human review.

Source: ``docs/optimization_closure_plan_v1_2026-05-28.md`` §6 + §8 Q1, Q5.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from sqlalchemy import text

from backend.celery_app import celery_app
from backend.config import settings
from backend.database import AsyncSessionLocal
from backend.tasks import run_async


logger = logging.getLogger("optimization.tasks")


@celery_app.task(name="backend.tasks.run_optimization_cycle")
def run_optimization_cycle() -> Dict[str, Any]:
    """Celery entry point. Wraps the async runner via :func:`run_async`
    (Windows --pool=solo friendly)."""
    return run_async(_run())


async def _run() -> Dict[str, Any]:
    if not bool(getattr(settings, "ENABLE_OPTIMIZATION_LOOP", False)):
        logger.info(
            "[optimization_tasks] ENABLE_OPTIMIZATION_LOOP=False — skipping cycle"
        )
        return {"skipped": "flag_off"}

    # Pick candidates in one short session — we want the list before the
    # long-running BrainAdapter / sim loop so the DB connection isn't
    # held idle for 10+ minutes.
    async with AsyncSessionLocal() as db:
        candidates = await _select_near_gate_candidates(
            db,
            limit=int(getattr(settings, "OPT_CANDIDATES_PER_CYCLE", 10)),
        )

    if not candidates:
        logger.info("[optimization_tasks] no near-gate candidates found")
        return {"skipped": "no_candidates", "n_cycles": 0}

    # Per-cycle sim budget = daily / cycles-per-day / candidates-per-cycle.
    # Default math: 400 / 4 / 10 = 10 sims per cycle == one full
    # SettingsSweepGenerator batch. Floored at 1 so a misconfig can't
    # send the Simulator a budget=0.
    cycles_per_day = max(
        1, 24 // int(getattr(settings, "OPT_BEAT_INTERVAL_HOURS", 6) or 6)
    )
    per_cycle_budget = max(
        1,
        int(getattr(settings, "OPT_DAILY_SIM_BUDGET", 400))
        // cycles_per_day
        // max(1, len(candidates)),
    )

    cycles: List[Dict[str, Any]] = []
    for cand in candidates:
        try:
            summary = await _run_one(cand, per_cycle_budget)
            cycles.append(summary)
        except Exception as ex:  # noqa: BLE001
            # Per-candidate failure is logged + recorded in
            # optimization_runs.error (OptimizationService.finish_cycle
            # error-stamp), but does NOT abort the beat.
            logger.exception(
                "[optimization_tasks] cycle for parent_alpha_id=%s failed: %s",
                cand.id, ex,
            )
            cycles.append({
                "parent_alpha_id": int(cand.id),
                "error": f"{type(ex).__name__}: {ex}",
            })

    return {
        "n_cycles": len(cycles),
        "cycles": cycles,
        "per_cycle_budget": per_cycle_budget,
    }


async def _select_near_gate_candidates(db, *, limit: int) -> List:
    """Pick alphas whose sharpe is within ``OPT_NEAR_GATE_BAND`` below the
    delay-1 hard gate, that haven't been optimized yet, and that
    ``_skip_optimize_pool`` (P1-D robustness gate) hasn't flagged.

    Stage A only targets delay-1 (delay-0 near-gate set is tiny — only 2
    candidates as of 2026-05-28; revisit in Stage B if the delay-0 pool
    grows). Dedup is on (parent_alpha_id) and (expression_hash) so
    re-running a beat doesn't re-spawn variants for the same lineage.
    """
    from backend.config import settings as _stg

    hard_gate = float(_stg.eval_thresholds(1)["sharpe_min"])
    band = float(getattr(_stg, "OPT_NEAR_GATE_BAND", 0.5))

    rows = (await db.execute(text(
        """
        SELECT id, alpha_id, expression, region, universe, dataset_id,
               delay, decay, neutralization, truncation, is_sharpe
        FROM alphas
        WHERE delay = 1
          AND is_sharpe IS NOT NULL
          AND is_sharpe >= :lo
          AND is_sharpe <  :hi
          AND (optimization_run_id IS NULL)
          AND id NOT IN (
              SELECT parent_alpha_id FROM optimization_runs
              WHERE cycle_started_at > NOW() - INTERVAL '7 day'
                AND parent_alpha_id IS NOT NULL
          )
          AND NOT COALESCE((metrics->>'_skip_optimize_pool')::bool, false)
        ORDER BY is_sharpe DESC
        LIMIT :limit
        """
    ), {"lo": hard_gate - band, "hi": hard_gate, "limit": int(limit)})).all()

    # Bare row tuples won't fit our protocol (need attribute access). Wrap in
    # a tiny duck-typed object — generator/persister read .expression,
    # .region, .universe, .delay, .truncation, .id.
    class _Cand:
        __slots__ = (
            "id", "alpha_id", "expression", "region", "universe",
            "dataset_id", "delay", "decay", "neutralization", "truncation",
            "is_sharpe",
        )

        def __init__(self, *args):
            (
                self.id, self.alpha_id, self.expression, self.region,
                self.universe, self.dataset_id, self.delay, self.decay,
                self.neutralization, self.truncation, self.is_sharpe,
            ) = args

    return [_Cand(*r) for r in rows]


async def _run_one(candidate, budget: int) -> Dict[str, Any]:
    """Open all the per-cycle collaborators + invoke OptimizationService."""
    # Lazy imports inside the function so the beat module stays importable
    # even when optional deps (BrainAdapter / Redis) are missing in test.
    from backend.adapters.brain_adapter import BrainAdapter
    from backend.services.correlation_service import CorrelationService
    from backend.services.optimization import (
        NoOpKnowledgeFeedback,
        OptimizationService,
    )
    from backend.services.optimization.generators.settings_sweep import (
        SettingsSweepGenerator,
    )
    from backend.services.optimization.persister import Persister
    from backend.services.optimization.repository import (
        OptimizationRunRepositoryImpl,
    )
    from backend.services.optimization.simulator import BrainSimulator
    from backend.services.optimization.submit_policy import StageASubmitPolicy
    from backend.services.optimization.winner_selector import WinnerSelector

    async with AsyncSessionLocal() as db:
        async with BrainAdapter() as brain:
            corr = CorrelationService(brain)
            repo = OptimizationRunRepositoryImpl(db)
            svc = OptimizationService(
                generator=SettingsSweepGenerator(),
                simulator=BrainSimulator(brain),
                winner_selector=WinnerSelector(),
                persister=Persister(db, corr_service=corr, repository=repo),
                submit_policy=StageASubmitPolicy(),
                repository=repo,
                feedback=NoOpKnowledgeFeedback(),
            )
            summary = await svc.run_one_cycle(
                candidate, trigger_source="beat", budget=int(budget),
            )
            await db.commit()
            return summary
