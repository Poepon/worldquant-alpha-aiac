"""Canary red-flag check core (2026-05-18).

Implements §4 of ``docs/production_canary_sop_2026_05_18.md``. Pure async
helper exposed to two callers:

- ``scripts/canary_redflag_check.py`` — operator CLI
- ``backend/tasks/canary_tasks.py:run_canary_redflag_check`` — Celery beat
  task wired into ``celery_beat_schedule`` every 6h

Soft-fail: each SQL check uses a fresh session, so a single failure
doesn't poison the rest. The helper never raises; on any unexpected
exception it logs and returns an empty result so the Celery beat task
can't crash the worker.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional, Tuple

from loguru import logger
from sqlalchemy import text


RED_FLAGS: List[Tuple[str, str, str, str]] = [
    # (label, sql, trigger_predicate, suggested_rollback_flag)
    (
        "R1a hook crash rate",
        # Filter out test-injected error rows (e.g. R1A_TEST_BOOM from
        # test_r1a_hook_failure_does_not_break_node) so canary signal-to-noise
        # stays high. Tests should rollback their INSERTs but until that's
        # fixed (separate scope), this filter prevents test runs from
        # triggering operator alerts on every */6:15 beat fire.
        "SELECT COALESCE((COUNT(*) FILTER ("
        "WHERE hook_error IS NOT NULL "
        "AND COALESCE(hook_error, '') NOT LIKE '%TEST%'"
        "))::float / NULLIF(COUNT(*), 0), 0.0) "
        "FROM r1a_attribution_log WHERE created_at > :t0",
        "value > 0.10",
        "ENABLE_R1A_HOOK",
    ),
    (
        "R1b cumulative LLM cost since T-0 (USD)",
        "SELECT COALESCE(SUM(llm_cost_usd), 0.0) "
        "FROM r1b_retry_log WHERE created_at > :t0",
        "value > 5.0",
        "ENABLE_R1A_HOOK",
    ),
    (
        "R8 failure-tree elevation pct",
        "SELECT COALESCE((COUNT(*) FILTER (WHERE had_failure_tree_elevation = true))::float "
        "/ NULLIF(COUNT(*), 0), 0.0) "
        "FROM r8_query_log WHERE created_at > :t0",
        "value > 0.50",
        "ENABLE_HIERARCHICAL_RAG",
    ),
    (
        "Simulation cache wrong-hit rows",
        "SELECT COUNT(*) FROM alphas "
        "WHERE (metrics->>'_sim_cache_hit')::bool = true "
        "  AND (metrics->>'sharpe') IS NULL "
        "  AND created_at > :t0",
        "value >= 1",
        "ENABLE_SIMULATION_CACHE",
    ),
    (
        "Mining task FAILED pct in window",
        "SELECT COALESCE((COUNT(*) FILTER (WHERE status='FAILED'))::float "
        "/ NULLIF(COUNT(*), 0), 0.0) "
        "FROM mining_tasks WHERE created_at > :t0",
        "value > 0.30",  # window-local threshold, see SOP §4
        "manual review",  # multi-flag escalation tree
    ),
]


def eval_predicate(pred: str, value: Any) -> bool:
    """Evaluate ``pred`` string with ``value`` bound. Returns False on any
    parse/eval failure — predicates are author-controlled constants in
    ``RED_FLAGS`` so this is a safety net, not user input."""
    try:
        return bool(eval(pred, {"__builtins__": {}}, {"value": value}))
    except Exception:
        return False


async def check_redflags(t0: datetime) -> List[dict]:
    """Run every entry in ``RED_FLAGS`` against the live DB scoped to ``t0``.

    Returns a list of dicts in declaration order. Each dict has keys:
        label, value (or None if DB error), triggered (bool),
        rollback (str), error (str, optional)

    Uses a fresh session per query so a failed query rolls back its own
    transaction without aborting the rest. Never raises.
    """
    try:
        from backend.database import AsyncSessionLocal
    except Exception as ex:
        logger.warning(f"[canary_redflag] database import failed: {ex}")
        return []

    out: List[dict] = []
    for label, sql, pred, rollback in RED_FLAGS:
        entry: dict = {"label": label, "rollback": rollback}
        try:
            async with AsyncSessionLocal() as s:
                r = await s.execute(text(sql), {"t0": t0})
                value = r.scalar()
            entry["value"] = value
            entry["triggered"] = eval_predicate(pred, value)
        except Exception as ex:
            entry["value"] = None
            entry["triggered"] = False
            entry["error"] = str(ex)[:200]
            logger.warning(f"[canary_redflag] check failed {label!r}: {ex}")
        out.append(entry)
    return out


def summarize(results: List[dict]) -> Tuple[int, Optional[str]]:
    """Returns ``(red_count, first_rollback_target)``. ``first_rollback_target``
    is None when no checks are red — useful for Celery task return shape."""
    red = [r for r in results if r.get("triggered")]
    if not red:
        return 0, None
    return len(red), red[0]["rollback"]
