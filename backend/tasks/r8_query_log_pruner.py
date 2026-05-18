"""Phase 3 R8 query telemetry review LOW: 90-day r8_query_log pruner.

R8 query telemetry review flagged: ``r8_query_log`` (added by commit
``39c1924``) has no TTL pruner. With ``ENABLE_R8_QUERY_LOG=False`` default
this is OK today, but promotion to long-term ON would let the table grow
unbounded (one row per ``query_hierarchical`` call).

This task DELETEs rows older than ``R8_QUERY_LOG_RETENTION_DAYS``
(default 90 days). Pure TTL — entire table is in scope, no entry_type
or meta predicate.

Wrapped by ``backend.tasks.r8_tasks.run_r8_query_log_pruner`` and scheduled
weekly (Sunday 04:30 Asia/Shanghai — staggered 30min after
``r1b-failure-tree-pruner`` at 04:00 SH so they don't fight for DB
resources) via ``backend/celery_app.py:beat_schedule``.

CLI:
  python -m backend.tasks.r8_query_log_pruner [days]
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import text


async def prune_old_r8_query_log_entries(days: int = 90) -> int:
    """Delete r8_query_log rows older than ``days``.

    Uses a parameterised cutoff so the predicate is index-friendly
    (``ix_r8q_created_at``) and SQL-injection-safe.

    Wraps the work in an explicit transaction (commit on success, rollback on
    error). On any DB exception logs a warning and returns 0 — never re-raises,
    so the Celery beat task can't crash the worker on transient PG issues.

    Args:
        days: retention window in days. Rows with ``created_at`` older than
              ``now() - interval 'N days'`` are deleted.

    Returns:
        int: number of rows deleted (0 on soft-fail).
    """
    try:
        from backend.database import AsyncSessionLocal
    except Exception as ex:
        logger.warning(f"[r8_query_log_pruner] database import failed: {ex}")
        return 0

    days_int = int(days)
    cutoff_utc = datetime.now(timezone.utc) - timedelta(days=days_int)
    logger.info(
        f"[r8_query_log_pruner] pruning r8_query_log rows "
        f"created before {cutoff_utc.isoformat()} (retention={days_int}d)"
    )

    try:
        async with AsyncSessionLocal() as db:
            try:
                result = await db.execute(
                    text(
                        "DELETE FROM r8_query_log "
                        "WHERE created_at < :cutoff"
                    ),
                    {"cutoff": cutoff_utc},
                )
                deleted = int(result.rowcount or 0)
                await db.commit()
                logger.info(
                    f"[r8_query_log_pruner] deleted {deleted} row(s) "
                    f"(cutoff={cutoff_utc.isoformat()}, retention={days_int}d)"
                )
                return deleted
            except Exception as ex:
                logger.warning(
                    f"[r8_query_log_pruner] DELETE failed, rolling back: {ex}"
                )
                try:
                    await db.rollback()
                except Exception:
                    pass
                return 0
    except Exception as ex:
        # AsyncSessionLocal init failure (PG unreachable etc.)
        logger.warning(f"[r8_query_log_pruner] session open failed: {ex}")
        return 0


if __name__ == "__main__":
    _days = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    _deleted = asyncio.run(prune_old_r8_query_log_entries(days=_days))
    print(f"deleted={_deleted} retention_days={_days}")
