"""Unit tests for the discrete/FLAT watchdog dead-detection predicate
(_discrete_task_is_dead) — esp. the 2026-06-01 batch-ONESHOT false-revive fix:
a QUEUED (never-started) task on a busy solo worker must NOT be revived."""
from datetime import datetime, timedelta, timezone

from backend.tasks.session_watchdog import _discrete_task_is_dead

NOW = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
DEAD_CUTOFF = NOW - timedelta(minutes=25)          # trace older than this = stale
FRESH = NOW - timedelta(minutes=2)                 # within window
STALE = NOW - timedelta(minutes=40)                # beyond window


def test_own_trace_fresh_not_dead():
    assert _discrete_task_is_dead(FRESH, worker_alive=True, dead_cutoff=DEAD_CUTOFF) is False
    assert _discrete_task_is_dead(FRESH, worker_alive=False, dead_cutoff=DEAD_CUTOFF) is False


def test_no_trace_but_worker_alive_is_queued_not_dead():
    # THE FIX: never-started task + a worker is progressing → queued → skip.
    assert _discrete_task_is_dead(None, worker_alive=True, dead_cutoff=DEAD_CUTOFF) is False


def test_no_trace_and_worker_dead_is_stuck_dead():
    # No trace anywhere (worker dead / lost dispatch) → revive (recovery kept).
    assert _discrete_task_is_dead(None, worker_alive=False, dead_cutoff=DEAD_CUTOFF) is True


def test_own_trace_stale_is_stalled_dead():
    # Started then stalled (own trace beyond window) → dead, regardless of others.
    assert _discrete_task_is_dead(STALE, worker_alive=True, dead_cutoff=DEAD_CUTOFF) is True
    assert _discrete_task_is_dead(STALE, worker_alive=False, dead_cutoff=DEAD_CUTOFF) is True
