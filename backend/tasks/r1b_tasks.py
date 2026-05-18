"""Phase 3 R1b ops tasks — Celery wrappers for R1b housekeeping.

Currently hosts:
  - ``run_failure_tree_pruner`` — weekly beat (Sun 04:00 SH) wrapper around
    :func:`backend.tasks.failure_tree_pruner.prune_old_failure_tree_entries`.

Mirrors the style of ``backend/tasks/q10_tasks.py`` (P3-Q10 PR2d): import
guarded, soft-fail on any runtime exception so beat scheduling never breaks
the worker.
"""
from __future__ import annotations

from loguru import logger

from backend.celery_app import celery_app
from backend.tasks import run_async


@celery_app.task(name="backend.tasks.run_failure_tree_pruner")
def run_failure_tree_pruner() -> int:
    """Beat-triggered wrapper around ``prune_old_failure_tree_entries``.

    Reads retention window from
    ``settings.R1B_FAILURE_TREE_RETENTION_DAYS`` (default 90). Returns the
    deletion count (0 on soft-fail). Never raises.
    """
    try:
        from backend.config import settings
        from backend.tasks.failure_tree_pruner import (
            prune_old_failure_tree_entries,
        )
    except Exception as ex:
        logger.error(f"[r1b_failure_tree_pruner] import failed: {ex}")
        return 0

    try:
        days = int(getattr(settings, "R1B_FAILURE_TREE_RETENTION_DAYS", 90))
        return int(run_async(prune_old_failure_tree_entries(days=days)))
    except Exception as ex:
        logger.error(f"[r1b_failure_tree_pruner] runtime failed: {ex}")
        return 0
