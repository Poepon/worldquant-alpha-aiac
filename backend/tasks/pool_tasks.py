"""Pool pipeline beats (Phase 1b B5) — scheduler + lease-recycle.

Both GATE on ENABLE_POOL_PIPELINE → no-op when OFF, so registering them in
celery_beat_schedule is INERT until Phase 1c-flip. The scheduler inserts
hyp_intent rows (weighted by mining_weight); lease-recycle is the single recovery
path (reclaims rows whose worker died mid-claim) — there is NO task-level
watchdog revive (the double-run footgun this design avoids).
"""
from loguru import logger

from backend.celery_app import celery_app
from backend.config import settings
from backend.tasks import run_async


def _pool_on() -> bool:
    return bool(getattr(settings, "ENABLE_POOL_PIPELINE", False))


@celery_app.task(name="backend.tasks.run_pool_scheduler")
def run_pool_scheduler():  # pragma: no cover - thin Celery wrapper
    if not _pool_on():
        return {"skipped": "ENABLE_POOL_PIPELINE OFF"}
    from backend.pool.scheduler import schedule_round
    n = int(getattr(settings, "POOL_SCHEDULER_BATCH", 5))
    try:
        inserted = run_async(schedule_round(n))
        return {"inserted": int(inserted)}
    except Exception as ex:  # noqa: BLE001
        logger.warning(f"[pool.scheduler beat] failed (non-fatal): {ex}")
        return {"error": str(ex)}


@celery_app.task(name="backend.tasks.run_pool_lease_recycle")
def run_pool_lease_recycle():  # pragma: no cover - thin Celery wrapper
    if not _pool_on():
        return {"skipped": "ENABLE_POOL_PIPELINE OFF"}
    from backend.models import CandidateQueue, HypothesisIntent
    from backend.pool.queue import recycle_expired

    cap = int(getattr(settings, "POOL_LEASE_MAX_ATTEMPTS", 3))
    batch = int(getattr(settings, "POOL_RECYCLE_BATCH", 200))

    async def _both():
        return {
            "hyp_intent": await recycle_expired(HypothesisIntent, cap, batch_limit=batch),
            "candidate_queue": await recycle_expired(CandidateQueue, cap, batch_limit=batch),
        }

    try:
        return run_async(_both())
    except Exception as ex:  # noqa: BLE001
        logger.warning(f"[pool.lease-recycle beat] failed (non-fatal): {ex}")
        return {"error": str(ex)}
