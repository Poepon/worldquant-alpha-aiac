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

# Manual-cycle concurrency lock (set NX by _run_manual, auto-expires after
# OPT_MANUAL_INFLIGHT_MINUTES so a crashed worker can't wedge re-triggers).
_MANUAL_LOCK_KEY_FMT = "aiac:opt:manual_lock:{alpha_id}"


class _Cand:
    """Duck-typed parent-alpha row for the optimization pipeline.

    The generator / persister read attribute access (.expression, .region,
    .universe, .delay, .truncation, .id) — a bare Row tuple won't fit. We
    deliberately use a plain object (NOT the ORM Alpha row) so the
    ``open_cycle`` commit inside ``OptimizationService.run_one_cycle`` can't
    trip SQLAlchemy's expire-on-commit lazy-refresh on a now-detached
    attribute (which would raise ``greenlet_spawn has not been called`` under
    the async session).

    Column order is shared by both producers (``_select_near_gate_candidates``
    SQL and ``_load_candidate`` SQL) — keep the two SELECTs aligned with the
    ``__slots__`` order below.
    """
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

    # User-abort flag: POST /ops/optimization/abort-batch sets this Redis key.
    # We check between candidate cycles (one-shot: clear after observing) so
    # the operator can stop a bad batch mid-flight without flipping
    # ENABLE_OPTIMIZATION_LOOP (which would also block the next beat).
    # Sims already dispatched to BRAIN keep running until BRAIN returns —
    # the abort only short-circuits the candidate-loop iteration.
    from backend.adapters.brain_adapter import BrainAdapter
    _redis = None
    try:
        _redis = await BrainAdapter._get_slot_redis()
    except Exception as _redis_ex:  # noqa: BLE001
        logger.warning(
            "[optimization_tasks] redis unavailable for abort flag (continuing without abort support): %s",
            _redis_ex,
        )

    cycles: List[Dict[str, Any]] = []
    aborted = False
    for cand in candidates:
        # Between-cycle abort check. Skip on Redis failure (soft-fail to
        # legacy behaviour rather than block the batch).
        if _redis is not None:
            try:
                if await _redis.get("aiac:opt:abort_requested"):
                    # One-shot: clear so it doesn't carry into the next beat.
                    await _redis.delete("aiac:opt:abort_requested")
                    logger.warning(
                        "[optimization_tasks] user abort observed — exiting beat after %d/%d cycles",
                        len(cycles), len(candidates),
                    )
                    aborted = True
                    break
            except Exception as _check_ex:  # noqa: BLE001
                logger.warning(
                    "[optimization_tasks] abort-flag check failed (continuing): %s",
                    _check_ex,
                )

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
        "aborted_by_user": aborted,
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

    return [_Cand(*r) for r in rows]


async def _load_candidate(db, alpha_id: int):
    """Load one alpha as a ``_Cand`` for a manual cycle.

    Mirrors the column projection of ``_select_near_gate_candidates`` so the
    same duck-typed object feeds the pipeline. Returns ``None`` when the
    alpha id doesn't exist.
    """
    rows = (await db.execute(text(
        """
        SELECT id, alpha_id, expression, region, universe, dataset_id,
               delay, decay, neutralization, truncation, is_sharpe
        FROM alphas
        WHERE id = :id
        """
    ), {"id": int(alpha_id)})).all()
    if not rows:
        return None
    return _Cand(*rows[0])


async def _run_one(
    candidate, budget: int, *, trigger_source: str = "beat",
) -> Dict[str, Any]:
    """Open all the per-cycle collaborators + invoke OptimizationService."""
    # Lazy imports inside the function so the beat module stays importable
    # even when optional deps (BrainAdapter / Redis) are missing in test.
    from backend.adapters.brain_adapter import BrainAdapter
    from backend.services.optimization.factory import build_optimization_service

    async with AsyncSessionLocal() as db:
        async with BrainAdapter() as brain:
            svc = build_optimization_service(db, brain)
            summary = await svc.run_one_cycle(
                candidate, trigger_source=trigger_source, budget=int(budget),
            )
            await db.commit()
            return summary


@celery_app.task(name="backend.tasks.run_manual_optimization_cycle")
def run_manual_optimization_cycle(
    alpha_id: int, budget: int, actor: str = "ui",
) -> Dict[str, Any]:
    """User-triggered single-alpha optimization cycle (trigger_source='manual').

    Dispatched by ``POST /alphas/{id}/optimize``. Runs ONE
    SettingsSweepGenerator cycle against the chosen alpha, INDEPENDENT of
    ``ENABLE_OPTIMIZATION_LOOP`` (that flag only gates the 6h beat). Winners
    land in the submit-backlog (Stage A SubmitPolicy never auto-submits).
    """
    return run_async(_run_manual(int(alpha_id), int(budget), str(actor)))


async def _run_manual(alpha_id: int, budget: int, actor: str) -> Dict[str, Any]:
    """Acquire a per-alpha Redis NX lock, load the alpha, run one cycle.

    The lock is the race-proof concurrency guard (the router's DB in-flight
    check is just fast UX feedback). It auto-expires after
    ``OPT_MANUAL_INFLIGHT_MINUTES`` so a crashed worker can't wedge future
    re-triggers. Redis unavailable → fail-open (proceed without the lock;
    the DB guard still applies).
    """
    from backend.adapters.brain_adapter import BrainAdapter

    lock_key = _MANUAL_LOCK_KEY_FMT.format(alpha_id=alpha_id)
    ttl = max(60, int(getattr(settings, "OPT_MANUAL_INFLIGHT_MINUTES", 40)) * 60)
    redis = None
    have_lock = False
    try:
        try:
            redis = await BrainAdapter._get_slot_redis()
        except Exception as ex:  # noqa: BLE001
            logger.warning(
                "[optimization_tasks] manual lock redis unavailable "
                "(proceeding without lock): %s", ex,
            )
            redis = None
        if redis is not None:
            try:
                have_lock = bool(
                    await redis.set(lock_key, str(actor), nx=True, ex=ttl)
                )
            except Exception as ex:  # noqa: BLE001
                logger.warning(
                    "[optimization_tasks] manual lock set failed (proceeding "
                    "without lock): %s", ex,
                )
                have_lock = False
                redis = None  # don't try to release a lock we didn't take
            if redis is not None and not have_lock:
                logger.info(
                    "[optimization_tasks] manual cycle for alpha_id=%s already "
                    "in flight — skipping", alpha_id,
                )
                return {"skipped": "in_flight", "alpha_id": int(alpha_id)}

        # Short session to fetch the candidate before the long sim loop —
        # don't hold a DB connection idle during BRAIN sims.
        async with AsyncSessionLocal() as db:
            candidate = await _load_candidate(db, alpha_id)
        if candidate is None:
            logger.warning(
                "[optimization_tasks] manual cycle alpha_id=%s not found",
                alpha_id,
            )
            return {"skipped": "not_found", "alpha_id": int(alpha_id)}

        logger.info(
            "[optimization_tasks] manual cycle start alpha_id=%s budget=%s "
            "actor=%s", alpha_id, budget, actor,
        )
        summary = await _run_one(candidate, budget, trigger_source="manual")
        summary["actor"] = actor
        return summary
    finally:
        if redis is not None and have_lock:
            try:
                await redis.delete(lock_key)
            except Exception as ex:  # noqa: BLE001
                logger.warning(
                    "[optimization_tasks] manual lock release failed "
                    "(TTL will clear it): %s", ex,
                )
