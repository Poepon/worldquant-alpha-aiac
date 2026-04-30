"""
Feedback Tasks - Background tasks for feedback analysis and learning

Contains tasks for:
- Daily feedback analysis
- Operator statistics updates
- Learning from successful alphas
"""

from sqlalchemy import select
from loguru import logger

from backend.celery_app import celery_app
from backend.database import AsyncSessionLocal
from backend.agents import FeedbackAgent
from backend.tasks import run_async


@celery_app.task(name="backend.tasks.run_daily_feedback")
def run_daily_feedback():
    """
    Run daily feedback analysis (scheduled).
    
    Analyzes recent alpha failures and successes to update
    the knowledge base.
    """
    logger.info("Running daily feedback analysis...")
    
    async def _run():
        async with AsyncSessionLocal() as db:
            feedback_agent = FeedbackAgent(db)
            result = await feedback_agent.run_daily_feedback()
            logger.info(f"Feedback analysis complete: {result}")
            return result
    
    return run_async(_run())


@celery_app.task(name="backend.tasks.update_operator_stats")
def update_operator_stats():
    """
    Update operator usage statistics (scheduled).
    
    Analyzes operator usage patterns and updates success rates.
    """
    logger.info("Updating operator stats...")
    
    async def _run():
        async with AsyncSessionLocal() as db:
            feedback_agent = FeedbackAgent(db)
            result = await feedback_agent.update_operator_stats()
            logger.info(f"Operator stats updated: {len(result)} operators")
            return {"operators_updated": len(result)}
    
    return run_async(_run())


@celery_app.task(name="backend.tasks.learn_from_alpha")
def learn_from_alpha(alpha_id: int, user_feedback: dict = None):
    """
    Learn from an alpha. Supports two trigger modes:

    1. **Automated** (no user_feedback): called when an alpha PASSES the
       hard gate; promotes its expression pattern to SUCCESS_PATTERN with
       confidence derived from the metrics.

    2. **HITL** (user_feedback provided): called by submit_feedback API.
       Branches per plan R3 #1 + W3:
         LIKED + PASS              → SUCCESS_PATTERN, confidence +0.2
         LIKED + PASS_PROVISIONAL  → SUCCESS_PATTERN, confidence +0.1
         LIKED + OPTIMIZE          → meta `user_likes_direction=True`,
                                     no KB write (avoid pollution)
         LIKED + FAIL              → meta only, no KB write
         DISLIKED + any            → existing pattern confidence -0.15
                                     (clamped at 0.1)

    Args:
        alpha_id: Alpha primary key
        user_feedback: dict with keys {rating, comment, quality_status}
    """
    logger.info(f"Learning from alpha {alpha_id} | hitl={user_feedback is not None}")

    async def _run():
        async with AsyncSessionLocal() as db:
            from backend.models import Alpha
            from backend.repositories.knowledge_repository import KnowledgeRepository

            query = select(Alpha).where(Alpha.id == alpha_id)
            result = await db.execute(query)
            alpha = result.scalar_one_or_none()

            if not alpha:
                return {"error": "Alpha not found"}

            feedback_agent = FeedbackAgent(db)

            # Mode 1: automated
            if user_feedback is None:
                return await feedback_agent.learn_from_success(alpha)

            # Mode 2: HITL — user explicitly liked / disliked
            rating = user_feedback.get("rating")
            quality = user_feedback.get("quality_status") or alpha.quality_status

            if rating == "LIKED" and quality in ("PASS", "PASS_PROVISIONAL"):
                # Promote pattern to KB with confidence boost
                confidence_bump = 0.2 if quality == "PASS" else 0.1
                kb_repo = KnowledgeRepository(db)
                # Read current pattern (if any) to compute new confidence
                existing = await kb_repo.find_by_pattern_text(
                    alpha.expression, alpha.region, alpha.dataset_id
                )
                base_conf = 0.5
                if existing and existing.meta_data:
                    base_conf = float(existing.meta_data.get("confidence", 0.5) or 0.5)
                new_conf = max(0.1, min(1.0, base_conf + confidence_bump))
                meta = dict((existing.meta_data if existing else {}) or {})
                meta.update({
                    "source": "hitl",
                    "confidence": new_conf,
                    "expected_sharpe": float(alpha.is_sharpe or 0),
                    "expected_fitness": float(alpha.is_fitness or 0),
                    "user_comment": user_feedback.get("comment"),
                    "regions": [alpha.region] if alpha.region else [],
                })
                await kb_repo.upsert_pattern(
                    entry_type="SUCCESS_PATTERN",
                    pattern_text=alpha.expression,
                    description=f"User-liked alpha (sharpe={alpha.is_sharpe})",
                    meta_data=meta,
                    region=alpha.region,
                    dataset_id=alpha.dataset_id,
                    created_by="HITL",
                )
                return {"action": "promoted", "confidence": new_conf, "quality": quality}

            elif rating == "LIKED":
                # OPTIMIZE/FAIL: do not write KB, just record direction signal
                return {"action": "direction_signal_only", "quality": quality}

            elif rating == "DISLIKED":
                kb_repo = KnowledgeRepository(db)
                existing = await kb_repo.find_by_pattern_text(
                    alpha.expression, alpha.region, alpha.dataset_id
                )
                if existing:
                    base_conf = float((existing.meta_data or {}).get("confidence", 0.5) or 0.5)
                    new_conf = max(0.1, base_conf - 0.15)
                    meta = dict(existing.meta_data or {})
                    meta["confidence"] = new_conf
                    await kb_repo.upsert_pattern(
                        entry_type=existing.entry_type,
                        pattern_text=alpha.expression,
                        description=existing.description,
                        meta_data=meta,
                        region=alpha.region,
                        dataset_id=alpha.dataset_id,
                        created_by="HITL",
                    )
                    return {"action": "decayed", "confidence": new_conf}
                return {"action": "no_existing_pattern"}

            return {"action": "noop", "rating": rating, "quality": quality}

    return run_async(_run())
