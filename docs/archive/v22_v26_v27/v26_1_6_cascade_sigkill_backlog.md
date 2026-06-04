# V-26.1 / V-26.6 — Cascade `while True` vs Celery `task_time_limit=3600`

**Status**: backlog
**Owner**: unassigned
**Severity**: 🔴 (gated by V-26.4 / V-26.5 mitigations)

## Problem

`backend/celery_app.py:28` sets `task_time_limit=3600` on every worker.
`backend/tasks/mining_tasks.py` (cascade path) runs `while True` —
a CONTINUOUS_CASCADE session is expected to run for hours / days. After
1 hour celery's SIGTERM → SIGKILL fires. SIGKILL means the `finally`
that releases the Redis cascade lock never runs (V-26.1), and the
10800s TTL holds the stale lock until 3 hours later (V-26.6).

## Why this is partially mitigated already

The Batch 1 commits make watchdog actually able to recover:

- **V-26.4** — `_release_lock` is now Lua-atomic. Eliminates the
  GET+DEL race at the TTL boundary. (Doesn't help SIGKILL because the
  finally still doesn't fire.)
- **V-26.5** — watchdog `_redispatch_task` now `force_clear_cascade_lock(...)`
  before re-dispatching celery, so the stale lock from a SIGKILL'd worker
  is evicted within 5 minutes (watchdog beat interval) rather than 3
  hours.

So the practical impact is reduced: instead of a 3-hour blackhole after
every SIGKILL, the user sees a max-5min gap before watchdog revives.
Still, the underlying cause — celery limit < cascade lifetime — remains.

## Two real-fix options (pick one)

### Option A — `task_time_limit=None` for the cascade route only

Smallest change:

```python
@celery_app.task(
    bind=True,
    name="backend.tasks.run_mining_task",
    time_limit=None,         # disable hard limit
    soft_time_limit=None,    # disable soft warning
)
def run_mining_task(self, task_id: int, run_id: int | None = None):
    ...
```

Risk: a runaway worker can occupy a slot indefinitely. Acceptable
because the watchdog already monitors `last_alpha_persisted_at` and
will revive after `DEAD_THRESHOLD_MIN` — but a wedged worker without
external signals doesn't get pre-empted by celery anymore.

### Option B — split cascade into per-phase celery chord

Each phase (T1 / T2 / T3) is its own short-lived task, chained:

```python
chord([
    run_tier_phase.s(task_id, tier=1),
    run_tier_phase.s(task_id, tier=2),
    run_tier_phase.s(task_id, tier=3),
])(finalize_cascade.s(task_id))
```

Pros: each phase fits comfortably within 1h time_limit; phase boundary
becomes a natural checkpoint for KB flush, BRAIN session refresh
(V-26.2), progress writeback (V-26.3). Cons: cascade restart semantics
become more complex (resume from current phase requires explicit state).

V-19 plan section already covered this as the recommended fix; we
deferred it because it interacts with cascade resume / progress
fields.

## When to do this

Trigger conditions:

- Watchdog revive frequency exceeds 1 per hour in production logs.
- A user reports "cascade hung" with a backlog longer than the 5-min
  watchdog gap.
- Plan v5+ Phase 3 (main-loop flip) gets prioritized — fits naturally
  with Option B's chord structure.

## Cross-references

- `docs/quality_review_mining_task_2026-05-13.md` — original review
  citing V-26.1 + V-26.6.
- V-26.4, V-26.5 commits — the mitigations that make this tolerable.
- V-19 plan section ("Celery chord 编排") in the archived Plan v6 — the
  earlier design discussion that landed on Option B.
