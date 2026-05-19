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
        # MUST run BEFORE db.commit() — new entries are db.add'd but not
        # flushed yet, so the SELECT only sees the prior weeks (would be
        # buggy if moved post-commit).
        await stamp_similarity_to_prev_week(db, entries)

        # INSERT all entries in a single transaction. F2 review fix:
        # Alembic o6d4a8f2c5b7 adds a unique constraint on
        # (distilled_at_week, region, pillar) WHERE retired_at IS NULL.
        # A double-fire of the cron (Celery beat redundancy / operator
        # manual re-run) will hit IntegrityError on the second INSERT,
        # which we catch + log + return without crashing the worker.
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
        try:
            await db.commit()
        except Exception as ex:
            # F2: most likely IntegrityError from the unique constraint —
            # treat as 'already ran this week', surface in result dict.
            await db.rollback()
            logger.warning(
                f"[g10] commit failed (likely double-fire on weekly cron): {ex}"
            )
            return {
                "distilled": 0,
                "error": "duplicate_week_or_constraint_violation",
                "detail": str(ex)[:200],
            }

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
    """Adapter — distill_last_week_pass_alphas expects an
    ``llm.call(prompt) -> {"text", "cost_usd", "model"}`` shape.
    LLMService's real surface is ``call(system_prompt, user_prompt,
    temperature, json_mode, max_tokens) -> LLMResponse`` (see
    ``backend/agents/services/llm_service.py:401``). This shim adapts.

    F1/F3 review fix (Sprint 3 R1+R2+R3): the prior implementation
    invoked a non-existent ``llm.acomplete(...)`` method; the
    AttributeError was swallowed by the broad try/except so every
    weekly distill run silently returned 0 rows when the flag was ON.
    Unit tests mocked the shim's ``call``, so 16/16 PASS hid the bug
    — same lesson as [[feedback_orm_constructor_real_test]].

    Cost estimate: LLMResponse exposes ``tokens_used`` but no
    cost_usd; we estimate via a model-family per-1K-token rate table.
    Conservative default $0.10 / 1K tokens when the model is unknown
    so the cap fires earlier rather than later.
    """

    # Approximate input+output cost per 1K tokens. Operator can tune
    # via env-derived settings.LLM_PRICE_PER_1K_OVERRIDES (defer to
    # fast-follow if precise billing matters; the cap is order-of-
    # magnitude budget, not invoice).
    _DEFAULT_COST_PER_1K = 0.10

    def __init__(self, llm_service: Any):
        self.llm = llm_service

    def _estimate_cost_usd(self, tokens_used: int) -> float:
        if not tokens_used:
            return 0.0
        return float(tokens_used) * self._DEFAULT_COST_PER_1K / 1000.0

    async def call(self, prompt: str) -> Dict[str, Any]:
        try:
            resp = await self.llm.call(
                system_prompt=(
                    "You are a quantitative research distiller. Read the "
                    "list of PASS alpha expressions and produce a terse "
                    "1-3 sentence summary of the common investment logic. "
                    "Do not quote expressions verbatim. Be concrete."
                ),
                user_prompt=prompt,
                temperature=0.3,  # low — consistent summaries, not creative
                json_mode=False,
                max_tokens=300,
                node_key="g10_distill",
            )
        except Exception as ex:
            logger.warning(f"[g10] LLMService.call failed: {ex}")
            return {"text": "", "cost_usd": 0.0, "model": ""}

        if not getattr(resp, "success", True):
            logger.warning(
                f"[g10] LLM call returned success=False model={resp.model!r} "
                f"error={resp.error!r}"
            )
            return {"text": "", "cost_usd": 0.0, "model": resp.model or ""}

        return {
            "text": resp.content or "",
            "cost_usd": self._estimate_cost_usd(int(resp.tokens_used or 0)),
            "model": resp.model or "",
        }
