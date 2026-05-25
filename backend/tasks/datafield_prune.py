"""Self-healing prune of stale/invalid data fields (2026-05-22).

BRAIN rejects some fields in our (periodically-synced) datafields catalog as
"Invalid data field <id>" at simulate time — they were valid metadata when
synced but aren't simulatable now (renamed/removed/region-gated). The dataset
bandit steering onto long-dormant datasets surfaces these: pv96's
``pv96_eq_dvd_cash_cg_amt`` alone burned 107 sim failures in a week.

This cron scans recent SIMULATION failures for the "Invalid data field <id>"
signature and sets ``datafields.is_active = False`` so _get_dataset_fields
stops offering them to the LLM. Mirrors monitor_llm_op_hallucinations (the
operator-side equivalent). Deterministic + reversible (re-sync / manual
re-activate). Never raises.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Set

from loguru import logger

from backend.celery_app import celery_app
from backend.tasks import run_async

# BRAIN message: "Invalid data field pv96_eq_dvd_cash_cg_amt. ..."
_INVALID_FIELD_RE = re.compile(r"Invalid data field\s+([A-Za-z0-9_]+)")


def _extract_invalid_fields(messages: List[str]) -> Set[str]:
    """Pull field_ids out of "Invalid data field <id>" error messages (pure)."""
    out: Set[str] = set()
    for msg in messages:
        if not msg:
            continue
        for m in _INVALID_FIELD_RE.finditer(msg):
            out.add(m.group(1))
    return out


@celery_app.task(name="backend.tasks.prune_invalid_datafields")
def prune_invalid_datafields() -> Dict[str, Any]:
    """Beat-triggered self-healing field prune. Never raises."""
    try:
        from backend.config import settings
        window = int(getattr(settings, "DATAFIELD_PRUNE_WINDOW_DAYS", 14))
        cap = int(getattr(settings, "DATAFIELD_PRUNE_MAX_PER_RUN", 500))
    except Exception as ex:  # noqa: BLE001
        logger.error(f"[datafield-prune] settings import failed: {ex}")
        return {"deactivated": 0, "error": str(ex)[:200]}
    try:
        return run_async(_prune_async(window_days=window, cap=cap))
    except Exception as ex:  # noqa: BLE001
        logger.error(f"[datafield-prune] failed: {ex}")
        return {"deactivated": 0, "error": str(ex)[:200]}


async def _prune_async(*, window_days: int, cap: int, session_factory=None) -> Dict[str, Any]:
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select, update

    from backend.models import AlphaFailure, DataField, DataFieldCellStats

    if session_factory is None:
        from backend.database import AsyncSessionLocal as session_factory  # noqa: N813

    lower = (datetime.now(timezone.utc) - timedelta(days=max(0, window_days))).replace(tzinfo=None)

    async with session_factory() as db:
        msgs = (await db.execute(
            select(AlphaFailure.error_message).where(
                AlphaFailure.error_message.like("%Invalid data field%"),
                AlphaFailure.created_at > lower,
            )
        )).scalars().all()
        invalid = _extract_invalid_fields(list(msgs))
        if not invalid:
            logger.info("[datafield-prune] no Invalid-data-field failures in window")
            return {"deactivated": 0, "fields": []}

        # is_active moved to datafield_cell_stats per (universe, delay) — a BRAIN
        # "Invalid data field" error is field-level (the field is gone/renamed),
        # so deactivate ALL cells of the matching field defs. Only flip cells
        # currently active (idempotent); cap on the field defs for safety.
        target_field_ids = (await db.execute(
            select(DataField.field_id)
            .join(DataFieldCellStats, DataFieldCellStats.datafield_ref == DataField.id)
            .where(
                DataField.field_id.in_(sorted(invalid)),
                DataFieldCellStats.is_active.is_(True),
            )
            .distinct()
            .limit(cap)
        )).scalars().all()
        targets = list(dict.fromkeys(target_field_ids))
        if targets:
            ref_subq = select(DataField.id).where(DataField.field_id.in_(targets))
            await db.execute(
                update(DataFieldCellStats)
                .where(DataFieldCellStats.datafield_ref.in_(ref_subq))
                .values(is_active=False)
            )
            await db.commit()

    logger.info(
        f"[datafield-prune] deactivated {len(targets)} invalid field(s) "
        f"(window={window_days}d, seen={len(invalid)}): {targets[:20]}"
    )
    return {"deactivated": len(targets), "fields": targets}
