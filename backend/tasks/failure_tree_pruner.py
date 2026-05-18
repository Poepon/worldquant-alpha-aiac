"""Phase 3 R1b.3 review LOW: 90-day FAILURE_PITFALL failure_tree pruner.

R1b.3 review flagged: ``record_failure_tree`` writes ``FAILURE_PITFALL``
``KnowledgeEntry`` rows with ``meta_data["failure_tree"]`` populated. UPSERT
dedupes on root_skeleton (200 chars), but at scale (50 alpha/round × N rounds
× multi-root mutations) the table grows linearly with no TTL.

This task DELETEs rows older than ``R1B_FAILURE_TREE_RETENTION_DAYS``
(default 90 days). It targets ONLY rows that have
``meta_data->'failure_tree'`` populated — other FAILURE_PITFALL rows
(e.g. from ``negative_knowledge_extract``) are out of scope and untouched.

Wrapped by ``backend.tasks.r1b_tasks.run_failure_tree_pruner`` and scheduled
weekly (Sunday 04:00 Asia/Shanghai) via ``backend/celery_app.py:beat_schedule``.

CLI:
  python -m backend.tasks.failure_tree_pruner [days]
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import text


async def prune_old_failure_tree_entries(days: int = 90) -> int:
    """Delete FAILURE_PITFALL entries with failure_tree older than ``days``.

    Uses Postgres JSONB key-existence operator ``?`` on
    ``meta_data->'failure_tree'`` and a parameterised cutoff so the predicate
    is index-friendly and SQL-injection-safe.

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
        logger.warning(f"[failure_tree_pruner] database import failed: {ex}")
        return 0

    days_int = int(days)
    cutoff_utc = datetime.now(timezone.utc) - timedelta(days=days_int)
    logger.info(
        f"[failure_tree_pruner] pruning FAILURE_PITFALL+failure_tree rows "
        f"created before {cutoff_utc.isoformat()} (retention={days_int}d)"
    )

    try:
        async with AsyncSessionLocal() as db:
            try:
                # JSONB key-existence: meta_data ? 'failure_tree' is the
                # Postgres operator. SQLAlchemy text() preserves the literal
                # `?` (no positional-param interpretation in text()).
                result = await db.execute(
                    text(
                        "DELETE FROM knowledge_entries "
                        "WHERE entry_type = 'FAILURE_PITFALL' "
                        "AND meta_data ? 'failure_tree' "
                        "AND created_at < :cutoff"
                    ),
                    {"cutoff": cutoff_utc},
                )
                deleted = int(result.rowcount or 0)
                await db.commit()
                logger.info(
                    f"[failure_tree_pruner] deleted {deleted} row(s) "
                    f"(cutoff={cutoff_utc.isoformat()}, retention={days_int}d)"
                )
                return deleted
            except Exception as ex:
                logger.warning(
                    f"[failure_tree_pruner] DELETE failed, rolling back: {ex}"
                )
                try:
                    await db.rollback()
                except Exception:
                    pass
                return 0
    except Exception as ex:
        # AsyncSessionLocal init failure (PG unreachable etc.)
        logger.warning(f"[failure_tree_pruner] session open failed: {ex}")
        return 0


if __name__ == "__main__":
    _days = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    _deleted = asyncio.run(prune_old_failure_tree_entries(days=_days))
    print(f"deleted={_deleted} retention_days={_days}")
