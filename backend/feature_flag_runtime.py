"""Runtime helpers for the cross-process feature-flag refresher loop.

Source: docs/alphagbm_skills_research_2026-05-15.md ops dashboard plan §1.4 / §6.

The FastAPI process and each Celery worker process maintains its own copy of
``backend.config._flag_override_cache``. To converge them after a flip
through /ops/feature-flags we run a periodic refresher that pulls every
override row from the DB and replaces the cache atomically.

Two entry points:

* :func:`start_async_refresher` — for the FastAPI lifespan; spawns an
  ``asyncio.create_task`` polling every ``REFRESH_INTERVAL_SEC`` and on
  Redis bump-key change.
* :func:`start_sync_refresher` — for Celery's ``worker_process_init``
  signal; spawns a daemon ``threading.Timer`` because the Celery worker
  process has no asyncio event loop running for arbitrary tasks.

Both refreshers are best-effort: any DB / Redis blip is logged + swallowed,
the loop never dies, and the in-process cache is left in its prior state
so callers keep seeing a consistent value.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Optional

logger = logging.getLogger("feature_flag_runtime")

# Default 60 s — small enough that a flip propagates in under one minute,
# large enough to keep DB load trivial. Override via env for tests.
REFRESH_INTERVAL_SEC: int = int(os.getenv("FEATURE_FLAG_REFRESH_INTERVAL_SEC", "60"))


# ---------------------------------------------------------------------------
# Async path (FastAPI)
# ---------------------------------------------------------------------------

_async_task: Optional[asyncio.Task] = None


async def _async_refresh_once() -> None:
    """Pull overrides from DB into the in-process cache, once.

    Built so the caller can also invoke it directly from an endpoint
    (e.g. POST /ops/flags/refresh-all on the FastAPI side).
    """
    # Import inside so test environments that haven't built a real DB
    # session pool don't crash at module import time.
    from backend.database import AsyncSessionLocal
    from backend.services.feature_flag_service import FeatureFlagService

    try:
        async with AsyncSessionLocal() as session:
            await FeatureFlagService(session).load_overrides_into_cache()
    except Exception as ex:
        # Never let the refresh loop die — log and continue.
        logger.warning("[feature_flag_runtime] async refresh failed: %s", ex)


async def _async_refresh_loop() -> None:
    """Forever-loop calling _async_refresh_once on a fixed interval."""
    while True:
        await _async_refresh_once()
        try:
            await asyncio.sleep(REFRESH_INTERVAL_SEC)
        except asyncio.CancelledError:
            logger.info("[feature_flag_runtime] async refresher cancelled")
            raise


def start_async_refresher() -> asyncio.Task:
    """Start the async refresher exactly once per process.

    Idempotent: returns the existing task if already running. Designed to
    be called from FastAPI's lifespan. The returned task should be
    cancelled on shutdown — the lifespan context manager handles that
    automatically when the app stops.
    """
    global _async_task
    if _async_task is not None and not _async_task.done():
        return _async_task
    # Called from FastAPI lifespan (async context) → there IS a running loop.
    # Use get_running_loop instead of the 3.12-deprecated get_event_loop
    # (which 3.14 removes entirely for the "no running loop" branch).
    loop = asyncio.get_running_loop()
    _async_task = loop.create_task(
        _async_refresh_loop(),
        name="feature_flag_async_refresher",
    )
    logger.info(
        "[feature_flag_runtime] async refresher started (interval=%ds)",
        REFRESH_INTERVAL_SEC,
    )
    return _async_task


async def stop_async_refresher() -> None:
    """Cancel the async refresher (lifespan shutdown)."""
    global _async_task
    if _async_task is None or _async_task.done():
        return
    _async_task.cancel()
    try:
        await _async_task
    except asyncio.CancelledError:
        pass
    _async_task = None


# ---------------------------------------------------------------------------
# Sync path (Celery worker_process_init)
# ---------------------------------------------------------------------------

_sync_timer: Optional[threading.Timer] = None
_sync_lock = threading.Lock()


def _sync_refresh_once() -> None:
    """Synchronous version that spins up its own short-lived event loop.

    Celery workers under ``--pool=solo`` have an event loop only inside
    individual tasks. The refresher runs outside any task, so we own the
    loop. We use ``asyncio.run`` (which makes + closes a fresh loop each
    call) to avoid leaking loops across the worker's lifetime.
    """
    try:
        asyncio.run(_async_refresh_once())
    except Exception as ex:
        logger.warning("[feature_flag_runtime] sync refresh failed: %s", ex)


def _sync_refresh_loop() -> None:
    """Re-arm the timer after each fire so it keeps ticking."""
    _sync_refresh_once()
    _arm_sync_timer()


def _arm_sync_timer() -> None:
    """Schedule the next sync refresh."""
    global _sync_timer
    with _sync_lock:
        # Daemon=True so the worker process can exit without waiting on us
        _sync_timer = threading.Timer(REFRESH_INTERVAL_SEC, _sync_refresh_loop)
        _sync_timer.daemon = True
        _sync_timer.start()


def start_sync_refresher() -> None:
    """Start the sync refresher exactly once per Celery worker process.

    Wired to Celery's ``worker_process_init`` signal in
    ``backend.celery_app``. Fires immediately so a fresh worker doesn't
    have to wait the full interval before its cache is warm.
    """
    global _sync_timer
    with _sync_lock:
        if _sync_timer is not None:
            return
    _sync_refresh_once()  # warm cache before first task runs
    _arm_sync_timer()
    logger.info(
        "[feature_flag_runtime] sync refresher started (interval=%ds)",
        REFRESH_INTERVAL_SEC,
    )


def stop_sync_refresher() -> None:
    """Cancel the timer (worker shutdown)."""
    global _sync_timer
    with _sync_lock:
        if _sync_timer is not None:
            _sync_timer.cancel()
            _sync_timer = None
