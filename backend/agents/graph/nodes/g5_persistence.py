"""G5 Phase A: cross-round persistence for trajectory crossover (2026-05-19).

Mirrors r1b_persistence.py mechanism: offspring expression(s) produced by
``llm_crossover_alpha`` at round end are stashed on ``MiningTask.config`` so
the NEXT round's _run_one_round_inline can consume + inject them as
``g5_offspring_candidates`` into MiningState. node_code_gen then prepends
them to ``pending_alphas`` so they walk the full validate → simulate →
evaluate → save_results pipeline alongside fresh LLM-generated alphas.

Soft-fail: every helper swallows exceptions + logs warn so a DB hiccup
NEVER blocks the mining round.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


CONFIG_KEY_PENDING_OFFSPRING = "g5_pending_offspring"


async def persist_offspring_after_round(
    task: Any,
    db: Any,
    offspring: List[Dict[str, Any]],
) -> bool:
    """Stash this round's crossover offspring on task.config for next round.

    Each offspring dict carries: expression, combination_strategy, rationale,
    parent_a_id, parent_b_id, parent_a_sharpe, parent_b_sharpe.

    Returns True on commit success, False on any failure (NEVER raises).
    """
    if task is None or db is None or not offspring:
        return False
    try:
        cfg = dict(getattr(task, "config", None) or {})
        # Keep only well-formed entries with a non-empty expression
        clean = [
            o for o in offspring
            if isinstance(o, dict) and (o.get("expression") or "").strip()
        ]
        if not clean:
            return False
        cfg[CONFIG_KEY_PENDING_OFFSPRING] = clean
        task.config = cfg
        try:
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(task, "config")
        except Exception:
            pass
        try:
            await db.commit()
        except Exception as ex:
            logger.warning(f"[g5_persist] commit failed: {ex}")
            try:
                await db.rollback()
            except Exception:
                pass
            return False
        logger.info(
            f"[g5_persist] stashed {len(clean)} offspring for next round "
            f"(task={getattr(task, 'id', '?')})"
        )
        return True
    except Exception as ex:
        logger.warning(f"[g5_persist] persist_offspring_after_round failed: {ex}")
        return False


async def consume_pending_offspring(
    task: Any,
    db: Any,
) -> Optional[List[Dict[str, Any]]]:
    """Pop ``g5_pending_offspring`` from MiningTask.config. Returns the list
    or None. Clears the config slot atomically so next round starts fresh.

    Called by ``mining_tasks._run_one_round_inline`` at the top of each round;
    the returned list is then passed into the initial MiningState as
    ``g5_offspring_candidates`` so node_code_gen can prepend them.

    NEVER raises.
    """
    if task is None or db is None:
        return None
    try:
        cfg = dict(getattr(task, "config", None) or {})
        pending = cfg.pop(CONFIG_KEY_PENDING_OFFSPRING, None)
        if not isinstance(pending, list) or not pending:
            return None
        # Filter again on read to defend against legacy / malformed stashed data
        clean = [
            o for o in pending
            if isinstance(o, dict) and (o.get("expression") or "").strip()
        ]
        if not clean:
            return None
        task.config = cfg
        try:
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(task, "config")
        except Exception:
            pass
        try:
            await db.commit()
        except Exception as ex:
            logger.warning(f"[g5_persist] consume commit failed: {ex}")
            try:
                await db.rollback()
            except Exception:
                pass
            return None
        logger.info(
            f"[g5_persist] consumed {len(clean)} pending offspring for "
            f"task={getattr(task, 'id', '?')}"
        )
        return clean
    except Exception as ex:
        logger.warning(f"[g5_persist] consume_pending_offspring failed: {ex}")
        return None


__all__ = [
    "CONFIG_KEY_PENDING_OFFSPRING",
    "persist_offspring_after_round",
    "consume_pending_offspring",
]
