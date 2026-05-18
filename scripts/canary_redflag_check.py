"""Canary red-flag check (2026-05-18).

Implements §4 of `docs/production_canary_sop_2026_05_18.md`. Run at T+1h /
T+6h / T+24h against the live DB. Exits non-zero if any red-flag triggers,
with the offending check + suggested rollback flag in stderr.

Usage:
    python scripts/canary_redflag_check.py [--t0 2026-05-18T17:00:00]

`--t0` is the canary start timestamp (ISO). Defaults to now() - 24h, which
suits T+24h sweeps; for T+1h / T+6h checks pass the actual T-0 string so
the windowed queries scope correctly.

Exit codes:
    0  all checks green
    1  ≥1 red-flag triggered
    2  DB error during check
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from backend.database import AsyncSessionLocal  # noqa: E402


_RED_FLAGS: List[Tuple[str, str, str, str]] = [
    # (label, sql, trigger_predicate, suggested_rollback_flag)
    (
        "R1a hook crash rate",
        "SELECT COALESCE((COUNT(*) FILTER (WHERE hook_error IS NOT NULL))::float "
        "/ NULLIF(COUNT(*), 0), 0.0) "
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


async def _check(t0: datetime) -> int:
    """Returns non-zero red-flag count. Uses fresh session per query so a
    single SQL failure doesn't poison the transaction for the rest."""
    fails = 0
    print(f"canary red-flag check, T-0 = {t0.isoformat()}Z")
    print(f"{'check':45s} {'value':>12s}  {'trigger':25s} {'status':10s}")
    print("-" * 100)
    for label, sql, pred, rollback in _RED_FLAGS:
        async with AsyncSessionLocal() as s:
            try:
                r = await s.execute(text(sql), {"t0": t0})
                value = r.scalar()
            except Exception as e:
                print(f"{label:45s} {'DB_ERR':>12s}  {'-':25s} {'ERROR':10s}")
                print(f"  detail: {str(e)[:200]}", file=sys.stderr)
                fails += 1
                continue
        triggered = _eval_pred(pred, value)
        status = "RED" if triggered else "green"
        disp = f"{value:.4f}" if isinstance(value, float) else str(value)
        print(f"{label:45s} {disp:>12s}  {pred:25s} {status:10s}")
        if triggered:
            fails += 1
            print(f"  ▶ rollback target: {rollback}", file=sys.stderr)
    return fails


def _eval_pred(pred: str, value) -> bool:
    try:
        return bool(eval(pred, {"__builtins__": {}}, {"value": value}))
    except Exception:
        return False


def _parse_t0(s: str | None) -> datetime:
    if s is None:
        return datetime.utcnow() - timedelta(hours=24)
    return datetime.fromisoformat(s.replace("Z", "").rstrip())


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--t0", default=None, help="Canary T-0 ISO timestamp (default: now-24h)")
    args = p.parse_args()
    t0 = _parse_t0(args.t0)
    try:
        fails = asyncio.run(_check(t0))
    except Exception as e:
        print(f"check failed: {e}", file=sys.stderr)
        return 2
    if fails:
        print(f"\n[FAIL] {fails} red-flag(s) triggered — see SOP §5 for rollback SQL",
              file=sys.stderr)
        return 1
    print("\n[OK] all red-flag checks green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
