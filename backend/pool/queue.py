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
    worker_id: Optional[str] = None,
    session_factory: Any = None,
) -> bool:
    """TXN-2: mark a row done/advanced (e.g. PENDING_SIM→PENDING_EVAL→DONE).

    Clears claimed_by/lease and applies ``updates`` (result columns:
    sim_result, verdict, error, trace_records, ...). Returns False if the row
    vanished — or (when ``worker_id`` is given) if the row is no longer owned by
    this worker. That owner guard is load-bearing: if a slow worker's lease
    expired and ``recycle_expired`` re-PENDING'd the row (then another worker
    re-claimed it), a stale ``complete`` from the original worker must be a NO-OP
    rather than clobbering the new claimant's stage/result (lost-update). Mirrors
    the guard ``renew_lease`` already has.
    """
    factory = session_factory or AsyncSessionLocal
    async with factory() as s:
        async with s.begin():
            # FOR UPDATE so the owner check + stage flip serialize against a
            # concurrent recycle_expired / claim_one on the same row (those also
            # lock). Without the lock, READ COMMITTED could read a pre-recycle
            # claimed_by, pass the guard, then UPDATE WHERE id= a row recycle just
            # re-PENDING'd. SQLite (tests) ignores FOR UPDATE — harmless no-op.
            row = await s.get(model, row_id, with_for_update=True)
            if row is None:
                return False
            if worker_id is not None and row.claimed_by != worker_id:
                return False  # recycled + reclaimed by someone else — don't clobber
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
    worker_id: Optional[str] = None,
    session_factory: Any = None,
) -> str:
    """TXN-2 failure path: attempts<cap → back to ``pending_stage`` ('retry'),
    else → FAILED poison-pill ('failed'). 'missing' if the row vanished, 'stale'
    if (when ``worker_id`` is given) the row is no longer owned by this worker.

    The row's ``attempts`` was already incremented at claim time, so a row that
    has been claimed ``max_attempts`` times poison-pills here. The owner guard
    prevents a stale worker (whose row was lease-recycled + reclaimed) from
    re-PENDING'ing / poisoning the new claimant's in-flight row.
    """
    factory = session_factory or AsyncSessionLocal
    async with factory() as s:
        async with s.begin():
            # FOR UPDATE — same serialize-against-recycle rationale as complete().
            row = await s.get(model, row_id, with_for_update=True)
            if row is None:
                return "missing"
            if worker_id is not None and row.claimed_by != worker_id:
                return "stale"  # recycled + reclaimed by someone else — don't touch
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
    batch_limit: Optional[int] = None,
    session_factory: Any = None,
) -> Dict[str, int]:
    """Lease-recycle (beat): in-flight ∧ lease_expires_at<now → attempts<cap back
    to its PENDING stage, else FAILED poison-pill. The single recovery path
    (there is NO task-level watchdog revive — avoids the double-run footgun).

    ``batch_limit`` bounds the reclaimed set per call (oldest-expired first) so one
    beat can't lock a pathological backlog in a single FOR UPDATE transaction; a
    larger backlog drains over successive beats. None → unbounded (legacy).

    INVARIANT (load-bearing): the LIMIT is honoured as "up to N rows" only because
    recycle runs SERIALLY (one every-2-min beat, no second recycler) over ORPHANED
    rows (lease expired ⇒ the owning worker is gone ⇒ no live FOR UPDATE lock to
    skip). So SKIP LOCKED skips nothing and PG's LIMIT-before-SKIP-LOCKED ordering
    can't under-reclaim. If a CONCURRENT recycler is ever added, re-examine this
    (and add a @requires_postgres test — the SQLite unit test no-ops SKIP LOCKED).

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
                .order_by(model.lease_expires_at)  # most-overdue first
            )
            if batch_limit is not None:
                stmt = stmt.limit(int(batch_limit))
            stmt = stmt.with_for_update(skip_locked=True)
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
