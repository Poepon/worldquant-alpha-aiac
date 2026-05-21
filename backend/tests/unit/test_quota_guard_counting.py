"""quota_guard counting tests (2026-05-20, Bug A family).

Regression guard: the BRAIN daily-quota guard must count only alphas that
actually consumed today's BRAIN simulate quota — NOT sync-imported historical
alphas (task_id NULL, created_at = insert-time). A sync of ~1040 historical
rows otherwise spiked today_alpha_count and would false-pause live mining.
"""
from __future__ import annotations

from datetime import datetime

import pytest


@pytest.mark.asyncio
async def test_quota_guard_excludes_sync_imported_alphas(async_engine, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import sessionmaker
    from backend.models import Alpha

    sm = sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    now = datetime.utcnow()
    async with sm() as db:
        # 2 mining-direct (task_id set = consumed today's BRAIN quota)
        for i in range(2):
            db.add(Alpha(
                alpha_id=f"mine{i}", task_id=99,
                expression=f"rank(close_{i})", region="USA", universe="TOP3000",
                quality_status="FAIL", created_at=now,
            ))
        # 3 sync-imported (task_id NULL = historical, no quota today)
        for i in range(3):
            db.add(Alpha(
                alpha_id=f"sync{i}", task_id=None,
                expression=f"rank(vol_{i})", region="USA", universe="TOP3000",
                quality_status="PENDING", created_at=now,
            ))
        await db.commit()

    monkeypatch.setattr("backend.tasks.session_watchdog.AsyncSessionLocal", sm)

    from backend.tasks.session_watchdog import _quota_guard_async
    result = await _quota_guard_async()

    # Only the 2 mining-direct alphas count; the 3 sync-imported are excluded.
    assert result["today_alpha_count"] == 2
    assert result["paused_count"] == 0  # well under threshold


@pytest.mark.asyncio
async def test_quota_guard_excludes_presim_and_dedup_skips(async_engine, monkeypatch):
    """fail_cnt must exclude PRESIM_SKIP + DEDUP_SKIP (no BRAIN slot consumed)."""
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import sessionmaker
    from backend.models import AlphaFailure

    sm = sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    now = datetime.utcnow()
    async with sm() as db:
        db.add(AlphaFailure(task_id=99, expression="x1", error_type="SIMULATION_ERROR", created_at=now))
        db.add(AlphaFailure(task_id=99, expression="x2", error_type="QUALITY_CHECK_FAILED", created_at=now))
        db.add(AlphaFailure(task_id=99, expression="x3", error_type="PRESIM_SKIP", created_at=now))
        db.add(AlphaFailure(task_id=99, expression="x4", error_type="DEDUP_SKIP", created_at=now))
        db.add(AlphaFailure(task_id=99, expression="x5", error_type=None, created_at=now))  # NULL still counts
        await db.commit()

    monkeypatch.setattr("backend.tasks.session_watchdog.AsyncSessionLocal", sm)

    from backend.tasks.session_watchdog import _quota_guard_async
    result = await _quota_guard_async()

    # SIMULATION_ERROR + QUALITY_CHECK_FAILED + NULL = 3 counted; PRESIM/DEDUP excluded
    assert result["today_failure_count"] == 3
