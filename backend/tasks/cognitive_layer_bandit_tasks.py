"""Phase 4 Tier E E1 — cognitive-layer bandit reward update cron.

Weekly Sunday 04:45 SH beat: aggregate alpha.metrics['_cognitive_layer_
used'] + PASS/FAIL over the trailing window → upsert per-layer
pass_count / fail_count into cognitive_layer_bandit_state. node_hypothesis
loads these so COGNITIVE_LAYER_SELECT_MODE='bandit' samples real Beta
posteriors instead of uniform priors (was DOA — select_layer always got
an empty stats dict).

Cumulative (not windowed-replace): each run ADDS the window's pass/fail
to the existing counts, so the posterior sharpens over time. Operator can
reset by truncating the table.

flag-gated: no-op unless ENABLE_COGNITIVE_LAYER_PROMPT is on (no point
accumulating reward for a feature that isn't firing). Never raises.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from loguru import logger

from backend.celery_app import celery_app
from backend.tasks import run_async


@celery_app.task(name="backend.tasks.run_cognitive_layer_bandit_update")
def run_cognitive_layer_bandit_update() -> Dict[str, Any]:
    """Beat-triggered cognitive-layer bandit reward update. Never raises."""
    try:
        from backend.config import settings
    except Exception as ex:  # noqa: BLE001
        logger.error(f"[r8v3-bandit] settings import failed: {ex}")
        return {"updated_layers": 0, "error": str(ex)[:200]}

    if not bool(getattr(settings, "ENABLE_COGNITIVE_LAYER_PROMPT", False)):
        logger.info("[r8v3-bandit] ENABLE_COGNITIVE_LAYER_PROMPT=OFF — skip")
        return {"updated_layers": 0, "skipped_reason": "flag_off"}

    try:
        return run_async(_update_async(int(getattr(settings, "COGNITIVE_LAYER_BANDIT_WINDOW_DAYS", 7))))
    except Exception as ex:  # noqa: BLE001
        logger.error(f"[r8v3-bandit] update failed: {ex}")
        return {"updated_layers": 0, "error": str(ex)[:200]}


async def _update_async(window_days: int) -> Dict[str, Any]:
    from sqlalchemy import select
    from backend.database import AsyncSessionLocal
    from backend.models import Alpha
    from backend.models.cognitive_layer_bandit import CognitiveLayerBanditState

    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(0, window_days))).replace(tzinfo=None)

    # Pull (metrics, quality_status) for alphas stamped with a layer in window.
    # Python-side aggregation for cross-DB (JSONB ->> differs PG vs SQLite).
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Alpha.metrics, Alpha.quality_status).where(Alpha.created_at >= cutoff)
        )).all()

        # Aggregate window pass/fail per layer
        agg: Dict[str, Dict[str, int]] = {}
        for metrics, status in rows:
            if not isinstance(metrics, dict):
                continue
            layer = metrics.get("_cognitive_layer_used")
            if not layer:
                continue
            bucket = agg.setdefault(str(layer), {"pass": 0, "fail": 0})
            status_str = getattr(status, "value", status)
            if status_str in ("PASS", "PASS_PROVISIONAL"):
                bucket["pass"] += 1
            elif status_str == "FAIL":
                bucket["fail"] += 1

        if not agg:
            logger.info("[r8v3-bandit] no stamped alphas in window — nothing to update")
            return {"updated_layers": 0, "window_days": window_days}

        # Cumulative upsert: ADD the window counts to existing rows.
        updated = 0
        for layer_id, counts in agg.items():
            existing = (await db.execute(
                select(CognitiveLayerBanditState).where(
                    CognitiveLayerBanditState.layer_id == layer_id
                )
            )).scalar_one_or_none()
            if existing is None:
                db.add(CognitiveLayerBanditState(
                    layer_id=layer_id,
                    pass_count=counts["pass"],
                    fail_count=counts["fail"],
                ))
            else:
                existing.pass_count = int(existing.pass_count or 0) + counts["pass"]
                existing.fail_count = int(existing.fail_count or 0) + counts["fail"]
            updated += 1
        await db.commit()

    logger.info(
        f"[r8v3-bandit] updated {updated} layer(s) from {len(rows)} alpha rows "
        f"(window={window_days}d): "
        + ", ".join(f"{k}(+{v['pass']}/{v['fail']})" for k, v in agg.items())
    )
    return {
        "updated_layers": updated,
        "window_days": window_days,
        "by_layer": {k: v for k, v in agg.items()},
    }
