"""Phase 3 R1b.2c: cross-round persistence for the CoSTEER loop.

Plan: ~/.claude/plans/phase3-r1b-costeer-loop-2026-05-18.md v1.3 §3.5 + §4.2.

The R1b loop counters live in ``MiningState`` and naturally reset per
LangGraph invocation. Two pieces of state need to survive across rounds:

  1. ``r1b_pending_new_hypothesis`` — produced by ``node_hypothesis_mutate``
     and consumed by next round's hypothesis_propose / rag_query path
  2. ``r1b_loop_budget_consumed`` — optional ledger for observability
     (sum of retries + mutations per task across all rounds)

Both are stored on ``MiningTask.config`` JSONB so they survive the
``pipeline round`` boundary in ``mining_tasks.py``.

Soft-fail: every helper swallows exceptions + logs warn so a DB hiccup
NEVER blocks the mining round.

Wiring points (for R1b.2c+):
  - ``persist_after_round(state, task, db)`` — called from persistence
    node / mining_tasks.pipeline round post-graph-invoke
  - ``consume_pending_hypothesis(task, db)`` — called from
    mining_tasks.pipeline round pre-graph-invoke; returns the
    pending dict and clears the config slot so next round starts fresh
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


CONFIG_KEY_PENDING_HYP = "r1b_pending_new_hypothesis"
CONFIG_KEY_BUDGET_CONSUMED = "r1b_loop_budget_consumed"


async def persist_after_round(state: Any, task: Any, db: Any) -> bool:
    """Write per-round R1b state to ``MiningTask.config``. Returns True on success.

    NEVER raises — DB failure is logged + soft-fall.
    """
    if state is None or task is None or db is None:
        return False
    try:
        cfg = dict(getattr(task, "config", None) or {})
        wrote = False
        # 1. Pending hypothesis — only persist if non-None and non-empty
        pending = getattr(state, "r1b_pending_new_hypothesis", None)
        if isinstance(pending, dict) and pending.get("statement"):
            cfg[CONFIG_KEY_PENDING_HYP] = pending
            wrote = True
        # 2. Budget ledger — accumulate counters across rounds
        retries = int(getattr(state, "r1b_retries_attempted_this_alpha", 0) or 0)
        mutations = int(getattr(state, "r1b_mutations_attempted_this_cycle", 0) or 0)
        cost = float(getattr(state, "r1b_token_cost_this_alpha", 0.0) or 0.0)
        if retries or mutations or cost > 0:
            ledger = dict(cfg.get(CONFIG_KEY_BUDGET_CONSUMED) or {})
            ledger["retries_total"] = int(ledger.get("retries_total", 0)) + retries
            ledger["mutations_total"] = int(ledger.get("mutations_total", 0)) + mutations
            ledger["cost_usd_total"] = round(
                float(ledger.get("cost_usd_total", 0.0)) + cost, 6,
            )
            cfg[CONFIG_KEY_BUDGET_CONSUMED] = ledger
            wrote = True
        if not wrote:
            return False
        # Apply + flag_modified for SQLAlchemy JSONB tracking
        task.config = cfg
        try:
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(task, "config")
        except Exception:
            pass
        try:
            await db.commit()
        except Exception as ex:
            logger.warning(f"[r1b_persist] commit failed: {ex}")
            try:
                await db.rollback()
            except Exception:
                pass
            return False
        return True
    except Exception as ex:
        logger.warning(f"[r1b_persist] persist_after_round failed (round unaffected): {ex}")
        return False


async def consume_pending_hypothesis(task: Any, db: Any) -> Optional[Dict[str, Any]]:
    """Pop ``r1b_pending_new_hypothesis`` from MiningTask.config; returns the
    dict or None. Clears the config slot so next round starts fresh.

    Called by ``mining_tasks.pipeline round`` at the top of each
    round; the returned dict is then passed into the initial MiningState
    (or directly to hypothesis_propose) so the mutated hypothesis drives
    the next round's alpha generation.

    NEVER raises.
    """
    if task is None or db is None:
        return None
    try:
        cfg = dict(getattr(task, "config", None) or {})
        pending = cfg.pop(CONFIG_KEY_PENDING_HYP, None)
        if not isinstance(pending, dict) or not pending.get("statement"):
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
            logger.warning(f"[r1b_persist] consume commit failed: {ex}")
            try:
                await db.rollback()
            except Exception:
                pass
            return None
        logger.info(
            f"[r1b_persist] consumed pending hypothesis for task={getattr(task, 'id', '?')}: "
            f"{pending.get('statement', '')[:80]}"
        )
        return pending
    except Exception as ex:
        logger.warning(f"[r1b_persist] consume_pending_hypothesis failed: {ex}")
        return None


def get_budget_ledger(task: Any) -> Dict[str, Any]:
    """Synchronous read of the cross-round R1b budget ledger.

    Returns ``{retries_total, mutations_total, cost_usd_total}`` or empty
    dict. Used by ops dashboards / telemetry queries that want a
    per-task running total without going to ``r1b_retry_log``.
    """
    try:
        cfg = getattr(task, "config", None) or {}
        ledger = cfg.get(CONFIG_KEY_BUDGET_CONSUMED) or {}
        return dict(ledger)
    except Exception:
        return {}


__all__ = [
    "CONFIG_KEY_PENDING_HYP",
    "CONFIG_KEY_BUDGET_CONSUMED",
    "persist_after_round",
    "consume_pending_hypothesis",
    "get_budget_ledger",
]
