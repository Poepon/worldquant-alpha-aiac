"""BrainRoleSwitchService — manual switch for BRAIN Consultant mode.

P3-Brain (2026-05-16). User receives a BRAIN upgrade email and manually
flips the flag in ops dashboard. No auto-detection.

Side effects on activate:
  * Set ENABLE_BRAIN_CONSULTANT_MODE = True in FeatureFlagOverride
    (carries audit trail + cross-process Redis invalidation hint)
  * Shorten multi-sim latch TTL to 5min + delete EVER key (gives BRAIN's
    async permission grant a buffer before retry; immediate DEL would race
    with stale latches set just before the upgrade email arrived)
  * Enqueue sync_datasets with explicit regions kwarg (FastAPI process
    resolves CONSULTANT_REGION_UNIVERSES, bypasses worker 60s flag cache)

deactivate only flips the flag — does NOT touch multi-sim latch (R1-M-1:
manually SETing latch creates a 24h invisible perf cliff if BRAIN actually
still has Consultant access; let brain_adapter's own fallback handle it).

async-only — do NOT call from sync celery tasks (uses asyncio.get_running_loop()
via BrainAdapter._get_slot_redis).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.feature_flag_service import FeatureFlagService


_FLAG_NAME = "ENABLE_BRAIN_CONSULTANT_MODE"


def _iso_utc(dt: Optional[datetime]) -> Optional[str]:
    """Append 'Z' marker so frontend dayjs parses as UTC.

    FeatureFlagOverride.updated_at is `DateTime` (NOT timezone=True);
    Postgres func.now() returns UTC. Python isoformat() yields a naive
    string without tz suffix, which dayjs/Date() parses as local time
    → 8h skew for GMT+8 users. Adding 'Z' is the zero-migration fix.
    """
    if dt is None:
        return None
    return dt.isoformat() + "Z"


class BrainRoleSwitchService:
    """Manual BRAIN Consultant mode switch (P3-Brain)."""

    def __init__(self, db: AsyncSession, flag_service: FeatureFlagService):
        self.db = db
        self.flag_service = flag_service

    async def activate_consultant_mode(
        self,
        *,
        actor: str,
        note: str = "用户确认收到 BRAIN 升级邮件",
    ) -> Dict[str, Any]:
        """Flip flag → True, clean BRAIN latch, kick off global sync."""
        # 1) Flip flag (audit + Redis bump via FeatureFlagService.set)
        await self.flag_service.set(_FLAG_NAME, True, actor=actor, note=note)

        # 2) Multi-sim latch cleanup — shorten TTL not DEL (avoid race with
        #    BRAIN's async permission grant; R2-M-2)
        try:
            from backend.adapters.brain_adapter import BrainAdapter
            redis = await BrainAdapter._get_slot_redis()
            await redis.expire(BrainAdapter._NO_MULTISIM_KEY, 300)
            # EVER key is "permanent — account was 403'd ≥ once". Must DEL
            # explicitly so brain_adapter doesn't short-circuit reprobe
            # (brain_adapter.py:663 reads this key before any multi-sim attempt).
            await redis.delete(BrainAdapter._NO_MULTISIM_EVER_KEY)
        except Exception as ex:
            logger.warning(f"[role_switch] redis latch cleanup failed (ignored): {ex}")

        # 3) Trigger global dataset sync — pass regions explicitly so worker
        #    doesn't have to wait for its 60s flag cache refresh (R2-M-5).
        try:
            from backend.tasks.sync_tasks import sync_datasets
            from backend.config import settings
            sync_datasets.delay(
                regions=list(settings.CONSULTANT_REGION_UNIVERSES.keys()),
            )
            sync_enqueued = True
        except Exception as ex:
            logger.warning(f"[role_switch] sync_datasets enqueue failed: {ex}")
            sync_enqueued = False

        return {
            "mode": "CONSULTANT",
            "sync_enqueued": sync_enqueued,
            "note": note,
            "actor": actor,
        }

    async def deactivate_consultant_mode(
        self,
        *,
        actor: str,
        note: str = "手动回退",
    ) -> Dict[str, Any]:
        """Flip flag → False (clear override). Does NOT touch multi-sim latch."""
        await self.flag_service.clear_override(_FLAG_NAME, actor=actor, note=note)
        return {"mode": "USER", "note": note, "actor": actor}

    async def get_state(self) -> Dict[str, Any]:
        """Return mode + effective_* + running_tasks_count + last_switched_at/by."""
        from backend.config import settings
        from backend.models import MiningTask

        flag_state = await self.flag_service.get_one(_FLAG_NAME)
        last_switched_at = flag_state.updated_at if flag_state else None
        last_switched_by = flag_state.updated_by if flag_state else None

        running_count = (
            await self.db.execute(
                select(func.count(MiningTask.id)).where(MiningTask.status == "RUNNING")
            )
        ).scalar() or 0

        return {
            "mode": "CONSULTANT" if settings.ENABLE_BRAIN_CONSULTANT_MODE else "USER",
            "effective_default_test_period": settings.effective_default_test_period,
            "effective_sharpe_submit_min": settings.effective_sharpe_submit_min,
            "effective_region_universes": settings.effective_region_universes,
            "running_tasks_count": running_count,
            "last_switched_at": _iso_utc(last_switched_at),
            "last_switched_by": last_switched_by,
        }
