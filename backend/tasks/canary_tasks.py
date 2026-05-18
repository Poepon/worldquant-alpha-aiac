"""Canary monitoring Celery tasks (2026-05-18).

Beat-triggered red-flag check wrapping
:func:`backend.tasks.canary_redflag.check_redflags`. Runs every 6h at
``*/6:15`` SH so it slots between ``update-operator-stats`` (``*/6:00``)
and ``refresh-portfolio-skeletons`` (``*/6:45``) without DB contention on
the Windows ``--pool=solo`` worker.

Each invocation scopes its checks to the trailing 6h window. Red rows
are logged at ERROR level (becomes a `.celery.err` line operator can
grep + alert on). Green rows are logged at INFO. The task itself never
raises so beat scheduling can't crash the worker.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from loguru import logger

from backend.celery_app import celery_app
from backend.tasks import run_async


@celery_app.task(name="backend.tasks.run_canary_redflag_check")
def run_canary_redflag_check() -> dict:
    """Beat-triggered wrapper around ``check_redflags``.

    Returns a dict ``{red_count, total, first_rollback, results}`` so the
    Celery beat result backend (Redis) retains a structured trace.

    Never raises — soft-fails to ``{red_count: 0, total: 0, error: str}``
    on any unexpected runtime exception so beat scheduling stays alive.
    """
    try:
        from backend.tasks.canary_redflag import check_redflags, summarize
    except Exception as ex:
        logger.error(f"[canary_redflag] import failed: {ex}")
        return {"red_count": 0, "total": 0, "error": str(ex)[:200]}

    try:
        t0 = datetime.now(timezone.utc) - timedelta(hours=6)
        results = run_async(check_redflags(t0=t0))
        red_count, first_rollback = summarize(results)
    except Exception as ex:
        logger.error(f"[canary_redflag] runtime failed: {ex}")
        return {"red_count": 0, "total": 0, "error": str(ex)[:200]}

    for r in results:
        if r.get("triggered"):
            logger.error(
                f"[canary_redflag] RED {r['label']!r} "
                f"value={r.get('value')} → rollback {r['rollback']!r} "
                "(see docs/production_canary_sop_2026_05_18.md §5)"
            )
        elif "error" in r:
            # already warned in check_redflags; noop here to keep log volume low
            pass
        else:
            logger.info(
                f"[canary_redflag] green {r['label']!r} value={r.get('value')}"
            )

    return {
        "red_count": red_count,
        "total": len(results),
        "first_rollback": first_rollback,
        "t0_utc": t0.isoformat(),
        "results": results,
    }
