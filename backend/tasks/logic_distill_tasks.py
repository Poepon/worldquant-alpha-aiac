"""A5.1 G10 logic-as-asset weekly distill Celery task (Phase 4 Sprint 3 / plan v5 §6.12).

Sunday 03:00 SH beat-triggered wrapper around
``logic_distill_service.distill_last_week_pass_alphas``.

Behavior:
  - flag-gated (``ENABLE_G10_LOGIC_DISTILL`` default OFF). Off → no-op.
  - reads past 7d PASS alphas grouped by (region, pillar), top-K each
  - calls LLMService once per bucket with a terse distill prompt
  - cost cap LOGIC_DISTILL_MAX_COST_USD_PER_WEEK $5 enforced
  - writes DistilledLogic rows in ONE transaction
  - computes Jaccard token similarity to previous-week entry for diagnostics
  - never raises so beat keeps running on failure

Result dict surfaces to the Redis backend so operator can see what
each weekly run produced via ``celery -A backend.celery_app inspect ...``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from loguru import logger

from backend.celery_app import celery_app
from backend.tasks import run_async


@celery_app.task(name="backend.tasks.run_weekly_logic_distill")
def run_weekly_logic_distill() -> Dict[str, Any]:
    """Sunday 03:00 SH — distill past 7d PASS alphas into logic library.

    Always returns a dict (never raises) so beat scheduling stays alive.
    """
    try:
        from backend.config import settings
    except Exception as ex:
        logger.error(f"[g10] settings import failed: {ex}")
        return {"distilled": 0, "error": str(ex)[:200]}

    if not bool(getattr(settings, "ENABLE_G10_LOGIC_DISTILL", False)):
        logger.info("[g10] ENABLE_G10_LOGIC_DISTILL=OFF — skip weekly distill")
        return {"distilled": 0, "skipped_reason": "flag_off"}

    try:
        return run_async(_distill_async())
    except Exception as ex:
        logger.error(f"[g10] weekly distill failed: {ex}")
        return {"distilled": 0, "error": str(ex)[:200]}


async def _distill_async() -> Dict[str, Any]:
    from backend.config import settings
    from backend.database import AsyncSessionLocal
    from backend.models.distilled_logic import DistilledLogic
    from backend.agents.services.llm_service import LLMService
    from backend.services.logic_distill_service import (
        distill_last_week_pass_alphas,
        stamp_similarity_to_prev_week,
    )

    max_cost = float(getattr(settings, "LOGIC_DISTILL_MAX_COST_USD_PER_WEEK", 5.0))
    top_k = int(getattr(settings, "LOGIC_DISTILL_TOP_K_PER_GROUP", 10))
    min_pass = int(getattr(settings, "LOGIC_DISTILL_MIN_PASS_COUNT", 3))
    lookback = int(getattr(settings, "LOGIC_DISTILL_LOOKBACK_DAYS", 7))

    llm = _DistillLLMShim(LLMService())

    async with AsyncSessionLocal() as db:
        entries = await distill_last_week_pass_alphas(
            db=db, llm=llm,
            max_cost_usd=max_cost,
            top_k_per_group=top_k,
            min_pass_count=min_pass,
            lookback_days=lookback,
        )

        # Stamp Jaccard similarity vs previous-week entry per (region, pillar)
        await stamp_similarity_to_prev_week(db, entries)

        # INSERT all entries in a single transaction
        for e in entries:
            db.add(DistilledLogic(
                logic_text=e.logic_text,
                tokens=e.tokens,
                source_alpha_ids=e.source_alpha_ids,
                pillar=e.pillar,
                region=e.region,
                distilled_at_week=e.distilled_at_week or datetime.now(timezone.utc),
                llm_cost_usd=e.llm_cost_usd,
                similarity_jaccard_to_prev_week=e.similarity_jaccard_to_prev_week,
                llm_model=e.llm_model,
            ))
        await db.commit()

    total_cost = sum((e.llm_cost_usd or 0.0) for e in entries)
    logger.info(
        f"[g10] weekly distill committed: {len(entries)} entries, "
        f"spent ${total_cost:.4f} (cap ${max_cost:.2f})"
    )
    return {
        "distilled": len(entries),
        "spent_usd": round(total_cost, 4),
        "max_cost_usd": max_cost,
        "by_region": _count_by_region(entries),
    }


def _count_by_region(entries: List[Any]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for e in entries:
        out[e.region] = out.get(e.region, 0) + 1
    return out


class _DistillLLMShim:
    """Adapter — distill_last_week_pass_alphas expects an `llm.call(prompt)`
    returning {"text", "cost_usd", "model"}. LLMService has its own API
    surface; this shim isolates the change."""

    def __init__(self, llm_service: Any):
        self.llm = llm_service

    async def call(self, prompt: str) -> Dict[str, Any]:
        try:
            text, cost = await self.llm.acomplete(
                prompt=prompt,
                max_tokens=300,  # 60-word output → ~120 tokens, allow headroom
                temperature=0.3,  # low — we want consistent summaries, not creative
            )
        except Exception as ex:
            logger.warning(f"[g10] LLMService.acomplete failed: {ex}")
            return {"text": "", "cost_usd": 0.0, "model": ""}

        return {
            "text": text or "",
            "cost_usd": float(cost or 0.0),
            "model": getattr(self.llm, "model", None) or "",
        }
