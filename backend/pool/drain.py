"""Pool drain control plane (Phase 1b B3).

Replaces MiningTask-status-as-control: each pool checks ``pool:{name}:drain``
before claiming. STOP = SET drain + purge PENDING→PURGED (in-flight rows are
left to finish / lease-recycle; drain NEVER touches CLAIMED/SIMULATING/
EVALUATING). RESUME = clear drain.

``is_draining`` FAILS OPEN (redis blip → not draining → the pool keeps working;
a transient redis error must not silently halt mining). Operator STOP/RESUME
(set/clear) propagate the redis error to the caller (the ops endpoint).
"""
from typing import Any, Optional

from loguru import logger
from sqlalchemy import update

from backend.database import AsyncSessionLocal
from backend.pool.stages import (
    CAND_PURGED,
    EVAL_PENDING,
    INTENT_PENDING,
    INTENT_PURGED,
    SIM_PENDING,
)

POOL_NAMES = ("hg", "s", "e")


def _drain_key(name: str) -> str:
    return f"pool:{name}:drain"


def _redis():
    from backend.tasks.redis_pool import get_redis_client  # lazy (tasks↔agents cycle)
    return get_redis_client()


def is_draining(name: str) -> bool:
    """True iff ``pool:{name}:drain`` is set. Fail-open on redis error."""
    try:
        return _redis().get(_drain_key(name)) is not None
    except Exception as ex:  # noqa: BLE001 — redis blip must not halt the pool
        logger.debug(f"[pool.drain] is_draining({name}) redis error (fail-open): {ex}")
        return False


def set_drain(name: str) -> None:
    _redis().set(_drain_key(name), "1")


def clear_drain(name: str) -> None:
    _redis().delete(_drain_key(name))


# PENDING-family stages that a drain purges (NOT the in-flight ones).
_PENDING_STAGES = {
    "hyp_intent": [INTENT_PENDING],
    "candidate_queue": [SIM_PENDING, EVAL_PENDING],
}
_PURGED_STAGE = {"hyp_intent": INTENT_PURGED, "candidate_queue": CAND_PURGED}


async def purge_pending(model: Any, *, session_factory: Any = None) -> int:
    """STOP-time purge: PENDING-family rows → PURGED. Leaves in-flight rows
    (CLAIMED/SIMULATING/EVALUATING) alone — they finish or get lease-recycled.
    Returns the number of rows purged.
    """
    factory = session_factory or AsyncSessionLocal
    pend = _PENDING_STAGES[model.__tablename__]
    purged = _PURGED_STAGE[model.__tablename__]
    async with factory() as s:
        async with s.begin():
            res = await s.execute(
                update(model).where(model.stage.in_(pend)).values(stage=purged)
            )
    return res.rowcount or 0
