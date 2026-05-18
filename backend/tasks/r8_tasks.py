"""Phase 3 R8 ops tasks — Celery wrappers for R8 housekeeping.

Currently hosts:
  - ``run_r8_query_log_pruner`` — weekly beat (Sun 04:30 SH, staggered
    30min after ``r1b-failure-tree-pruner`` at 04:00 SH to avoid DB
    contention) wrapper around
    :func:`backend.tasks.r8_query_log_pruner.prune_old_r8_query_log_entries`.

Mirrors the style of ``backend/tasks/r1b_tasks.py``: import guarded,
soft-fail on any runtime exception so beat scheduling never breaks the
worker.
"""
from __future__ import annotations

from loguru import logger

from backend.celery_app import celery_app
from backend.tasks import run_async


@celery_app.task(name="backend.tasks.run_r8_query_log_pruner")
def run_r8_query_log_pruner() -> int:
    """Beat-triggered wrapper around ``prune_old_r8_query_log_entries``.

    Reads retention window from
    ``settings.R8_QUERY_LOG_RETENTION_DAYS`` (default 90). Returns the
    deletion count (0 on soft-fail). Never raises.
    """
    try:
        from backend.config import settings
        from backend.tasks.r8_query_log_pruner import (
            prune_old_r8_query_log_entries,
        )
    except Exception as ex:
        logger.error(f"[r8_query_log_pruner] import failed: {ex}")
        return 0

    try:
        days = int(getattr(settings, "R8_QUERY_LOG_RETENTION_DAYS", 90))
        return int(run_async(prune_old_r8_query_log_entries(days=days)))
    except Exception as ex:
        logger.error(f"[r8_query_log_pruner] runtime failed: {ex}")
        return 0
