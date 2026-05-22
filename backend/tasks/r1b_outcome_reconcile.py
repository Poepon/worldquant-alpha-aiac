"""R1b CoSTEER outcome reconciliation cron (Break 2 fix, 2026-05-22).

r1b_retry_log.outcome was written "pending" and NOTHING ever filled it — the
"post-BRAIN reconciliation hook" referenced in the loop code was never
implemented (verified: 355/355 rows pending, 0 outcome_sharpe). So the loop
had ZERO feedback signal — it never learned whether a retry/mutate helped, and
the /ops r1b telemetry pass/fail counts were always 0.

This periodic job closes the feedback half of the loop by matching pending log
rows to the alphas they produced and stamping outcome (pass/fail) + sharpe:

  - mutate_hyp:  alphas whose hypothesis_id == new_hypothesis_id (the inject
    path now links them — CoSTEER loop-closure fix). The mutated hypothesis
    drives round N+1, so its alphas appear after the log row.
  - retry_impl:  alphas in the same task whose expression == new_expression
    (the in-place rewrite is simulated in the same round), created at/after
    the log row to avoid matching an unrelated older alpha.

outcome rule (over REAL sims only — excludes metrics._pre_brain_skip):
  any PASS/PASS_PROVISIONAL → 'pass' (+ outcome_sharpe = max is_sharpe);
  else any real sim         → 'fail';
  else                      → stay 'pending' (alpha not simulated yet → retry
                              next run; idempotent).

flag-gated (ENABLE_R1B_RETRY_LOOP OR ENABLE_R1B_HYPOTHESIS_MUTATE). Never raises.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from backend.celery_app import celery_app
from backend.tasks import run_async

_PASS_STATUSES = ("PASS", "PASS_PROVISIONAL")


@celery_app.task(name="backend.tasks.reconcile_r1b_outcomes")
def reconcile_r1b_outcomes() -> Dict[str, Any]:
    """Beat-triggered R1b outcome reconciliation. Never raises."""
    try:
        from backend.config import settings
    except Exception as ex:  # noqa: BLE001
        logger.error(f"[r1b-reconcile] settings import failed: {ex}")
        return {"reconciled": 0, "error": str(ex)[:200]}

    if not (
        bool(getattr(settings, "ENABLE_R1B_RETRY_LOOP", False))
        or bool(getattr(settings, "ENABLE_R1B_HYPOTHESIS_MUTATE", False))
    ):
        logger.info("[r1b-reconcile] both R1b flags OFF — skip")
        return {"reconciled": 0, "skipped_reason": "flag_off"}

    try:
        return run_async(_reconcile_async(
            max_rows=int(getattr(settings, "R1B_RECONCILE_MAX_ROWS", 1000)),
        ))
    except Exception as ex:  # noqa: BLE001
        logger.error(f"[r1b-reconcile] failed: {ex}")
        return {"reconciled": 0, "error": str(ex)[:200]}


def _outcome_from_alphas(rows: List[Tuple[Any, Any, Any]]) -> Tuple[Optional[str], Optional[float]]:
    """rows = [(quality_status, is_sharpe, metrics), ...] for one log row's
    candidate alphas. Returns (outcome, outcome_sharpe) — None outcome means
    "no real sim yet, stay pending"."""
    real = []
    for status, sharpe, metrics in rows:
        m = metrics if isinstance(metrics, dict) else {}
        if m.get("_pre_brain_skip"):
            continue  # never hit BRAIN
        real.append((getattr(status, "value", status), sharpe))
    if not real:
        return None, None
    passes = [s for st, s in real if st in _PASS_STATUSES]
    if passes:
        sharpes = [float(s) for s in passes if s is not None]
        return "pass", (max(sharpes) if sharpes else None)
    return "fail", None


async def _has_real_failure(db, row) -> bool:
    """True if the retry/mutate attempt produced a real (non-PRESIM_SKIP)
    failure in alpha_failures — matched by hypothesis_id (mutate) or
    task_id+expression (retry). Used as the fail fallback when no PASS landed
    in the `alphas` table."""
    from sqlalchemy import func, select

    from backend.models import AlphaFailure

    q = select(func.count()).select_from(AlphaFailure).where(
        func.coalesce(AlphaFailure.error_type, "") != "PRESIM_SKIP"
    )
    if row.attempt_type == "mutate_hyp" and row.new_hypothesis_id:
        q = q.where(AlphaFailure.hypothesis_id == row.new_hypothesis_id)
    elif row.attempt_type == "retry_impl" and row.new_expression:
        q = q.where(AlphaFailure.expression == row.new_expression)
        if row.task_id is not None:
            q = q.where(AlphaFailure.task_id == row.task_id)
    else:
        return False
    return bool((await db.execute(q)).scalar() or 0)


async def _reconcile_async(*, max_rows: int, session_factory=None) -> Dict[str, Any]:
    from sqlalchemy import select

    from backend.models import Alpha
    from backend.models.r1b_retry import R1bRetryLog

    if session_factory is None:
        from backend.database import AsyncSessionLocal as session_factory  # noqa: N813

    reconciled = {"pass": 0, "fail": 0}
    still_pending = 0

    async with session_factory() as db:
        pend = (await db.execute(
            select(R1bRetryLog)
            .where(R1bRetryLog.outcome == "pending")
            .limit(max_rows)
        )).scalars().all()

        for row in pend:
            cand: List[Tuple[Any, Any, Any]] = []
            if row.attempt_type == "mutate_hyp" and row.new_hypothesis_id:
                cand = (await db.execute(
                    select(Alpha.quality_status, Alpha.is_sharpe, Alpha.metrics)
                    .where(Alpha.hypothesis_id == row.new_hypothesis_id)
                )).all()
            elif row.attempt_type == "retry_impl" and row.new_expression:
                # Same-task exact-expression match: the in-place rewrite is
                # simulated in the same round, so the rewritten alpha lands
                # under this task with expression == new_expression. (task_id +
                # exact expression is specific enough; a retry rewrites to a
                # NEW expression so an unrelated collision is unlikely.)
                q = select(Alpha.quality_status, Alpha.is_sharpe, Alpha.metrics).where(
                    Alpha.expression == row.new_expression
                )
                if row.task_id is not None:
                    q = q.where(Alpha.task_id == row.task_id)
                cand = (await db.execute(q)).all()

            outcome, sharpe = _outcome_from_alphas(cand)
            if outcome is None:
                # No real-sim alpha in `alphas` (no PASS) — the rewritten /
                # mutated alpha most often BRAIN-failed and landed in
                # alpha_failures, not alphas. A real failure there (not a
                # pre-sim skip) means the attempt didn't yield a pass → 'fail'.
                if await _has_real_failure(db, row):
                    outcome = "fail"
            if outcome is None:
                still_pending += 1
                continue
            row.outcome = outcome
            if sharpe is not None:
                row.outcome_sharpe = sharpe
            reconciled[outcome] += 1

        await db.commit()

    total = reconciled["pass"] + reconciled["fail"]
    logger.info(
        f"[r1b-reconcile] reconciled={total} (pass={reconciled['pass']} "
        f"fail={reconciled['fail']}) still_pending={still_pending} scanned={len(pend)}"
    )
    return {
        "reconciled": total,
        "pass": reconciled["pass"],
        "fail": reconciled["fail"],
        "still_pending": still_pending,
    }
