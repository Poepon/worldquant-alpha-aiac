"""Two-transaction claim / lease primitives for the pool queues (Phase 1b).

The claim/lease contract (plan §4, gotcha #9 idle-in-txn):
  - ``claim_one`` runs ONE short transaction: ``SELECT ... FOR UPDATE SKIP
    LOCKED`` + flip stage to in-flight + stamp claimed_by / lease_expires_at /
    attempts, then COMMIT. The row lock is released the instant the transaction
    commits — BEFORE the worker runs any (long) node. The worker NEVER holds an
    open transaction / row lock across a node await (that is the idle-in-txn
    lock-leak this design exists to avoid).
  - ``renew_lease`` is the heartbeat for long ops (a BRAIN sim legitimately holds
    30-90 min); a separate coroutine renews so the lease-recycle beat doesn't
    double-run a still-live claim.
  - ``complete`` / ``fail_or_retry`` write the RESULT in a SECOND transaction.
  - ``recycle_expired`` (lease-recycle beat) reclaims rows whose worker died:
    in-flight ∧ lease_expires_at<now → attempts<cap back to PENDING else FAILED
    poison-pill.

Generic over HypothesisIntent + CandidateQueue (same stage/claimed_by/
lease_expires_at/attempts machinery). ``session_factory`` is injectable so tests
bind a test engine; it MUST be configured ``expire_on_commit=False`` (as
AsyncSessionLocal is) so a returned claimed row's columns stay readable after the
claim transaction closes.
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from backend.database import AsyncSessionLocal
from backend.pool.stages import (
    CAND_FAILED,
    INFLIGHT_FOR,
    INTENT_CLAIMED,
    INTENT_FAILED,
    PENDING_FOR_INFLIGHT,
    SIM_INFLIGHT,
    EVAL_INFLIGHT,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _failed_stage_for(model: Any) -> str:
    """The terminal FAILED stage literal for a given queue model."""
    return INTENT_FAILED if model.__tablename__ == "hyp_intent" else CAND_FAILED


def _inflight_stages_for(model: Any) -> List[str]:
    """The in-flight stages a lease-recycle scan must consider for a model."""
    if model.__tablename__ == "hyp_intent":
        return [INTENT_CLAIMED]
    return [SIM_INFLIGHT, EVAL_INFLIGHT]


async def claim_one(
    model: Any,
    pending_stage: str,
    worker_id: str,
    lease_seconds: int,
    *,
    session_factory: Any = None,
) -> Optional[Any]:
    """Atomically claim the oldest ``pending_stage`` row (FOR UPDATE SKIP LOCKED).

    Returns the claimed ORM row (stage already flipped to in-flight, lease
    stamped) or None if the queue is empty. The transaction COMMITS before
    return — the caller holds no lock while running its node. Read only plain
    columns off the returned (detached) row.
    """
    factory = session_factory or AsyncSessionLocal
    inflight = INFLIGHT_FOR[pending_stage]
    async with factory() as s:
        async with s.begin():
            stmt = (
                select(model)
                .where(model.stage == pending_stage)
                .order_by(model.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            row = (await s.execute(stmt)).scalars().first()
            if row is None:
                return None
            row.stage = inflight
            row.claimed_by = worker_id
            row.lease_expires_at = _utcnow() + timedelta(seconds=lease_seconds)
            row.attempts = (row.attempts or 0) + 1
        # s.begin() block committed → row lock released here.
    return row


async def renew_lease(
    model: Any,
    row_id: int,
    lease_seconds: int,
    *,
    worker_id: Optional[str] = None,
    session_factory: Any = None,
) -> bool:
    """Heartbeat: extend the lease of a still-in-flight claimed row.

    Returns False if the row is gone, terminal, or (when worker_id given) claimed
    by someone else (so a recycled-and-reclaimed row isn't double-renewed).
    """
    factory = session_factory or AsyncSessionLocal
    async with factory() as s:
        async with s.begin():
            row = await s.get(model, row_id)
            if row is None or row.stage not in PENDING_FOR_INFLIGHT:
                return False
            if worker_id is not None and row.claimed_by != worker_id:
                return False
            row.lease_expires_at = _utcnow() + timedelta(seconds=lease_seconds)
    return True


async def complete(
    model: Any,
    row_id: int,
    next_stage: str,
    *,
    updates: Optional[Dict[str, Any]] = None,
    session_factory: Any = None,
) -> bool:
    """TXN-2: mark a row done/advanced (e.g. PENDING_SIM→PENDING_EVAL→DONE).

    Clears claimed_by/lease and applies ``updates`` (result columns:
    sim_result, verdict, error, trace_records, ...). Returns False if the row
    vanished.
    """
    factory = session_factory or AsyncSessionLocal
    async with factory() as s:
        async with s.begin():
            row = await s.get(model, row_id)
            if row is None:
                return False
            row.stage = next_stage
            row.claimed_by = None
            row.lease_expires_at = None
            for k, v in (updates or {}).items():
                setattr(row, k, v)
    return True


async def fail_or_retry(
    model: Any,
    row_id: int,
    pending_stage: str,
    max_attempts: int,
    *,
    error: Optional[str] = None,
    session_factory: Any = None,
) -> str:
    """TXN-2 failure path: attempts<cap → back to ``pending_stage`` ('retry'),
    else → FAILED poison-pill ('failed'). 'missing' if the row vanished.

    The row's ``attempts`` was already incremented at claim time, so a row that
    has been claimed ``max_attempts`` times poison-pills here.
    """
    factory = session_factory or AsyncSessionLocal
    async with factory() as s:
        async with s.begin():
            row = await s.get(model, row_id)
            if row is None:
                return "missing"
            if (row.attempts or 0) < max_attempts:
                row.stage = pending_stage
                row.claimed_by = None
                row.lease_expires_at = None
                outcome = "retry"
            else:
                row.stage = _failed_stage_for(model)
                row.claimed_by = None
                row.lease_expires_at = None
                if error is not None and hasattr(row, "error"):
                    row.error = str(error)[:2000]
                outcome = "failed"
    return outcome


async def recycle_expired(
    model: Any,
    max_attempts: int,
    *,
    session_factory: Any = None,
) -> Dict[str, int]:
    """Lease-recycle (beat): in-flight ∧ lease_expires_at<now → attempts<cap back
    to its PENDING stage, else FAILED poison-pill. The single recovery path
    (there is NO task-level watchdog revive — avoids the double-run footgun).

    Returns ``{"recycled": n, "poisoned": m}``.
    """
    factory = session_factory or AsyncSessionLocal
    inflight = _inflight_stages_for(model)
    now = _utcnow()
    recycled = 0
    poisoned = 0
    async with factory() as s:
        async with s.begin():
            stmt = (
                select(model)
                .where(
                    model.stage.in_(inflight),
                    model.lease_expires_at.is_not(None),
                    model.lease_expires_at < now,
                )
                .with_for_update(skip_locked=True)
            )
            rows = (await s.execute(stmt)).scalars().all()
            for row in rows:
                if (row.attempts or 0) < max_attempts:
                    row.stage = PENDING_FOR_INFLIGHT[row.stage]
                    row.claimed_by = None
                    row.lease_expires_at = None
                    recycled += 1
                else:
                    row.stage = _failed_stage_for(model)
                    row.claimed_by = None
                    row.lease_expires_at = None
                    poisoned += 1
    return {"recycled": recycled, "poisoned": poisoned}
