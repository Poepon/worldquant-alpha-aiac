"""Backfill: rerun should_optimize() for alphas whose _optimize_reason was
polluted by the pre-fix Bug A (OS data unavailable but reason claimed
"OOS 偏弱" / "IS→OOS 衰减" / "IS/OOS 均为负").

Does NOT touch quality_status — that's gated by hard_gate_pass which is
independent of should_optimize. This script only cleans the reason field
to remove misleading text from KB learning samples and the alpha-detail UI.

Run:
  python scripts/cleanup_optimize_reason.py            # dry-run preview
  python scripts/cleanup_optimize_reason.py --confirm  # write
"""
from __future__ import annotations

import asyncio
import sys
from typing import Any, Dict, List

from sqlalchemy import text

from backend.alpha_scoring import should_optimize
from backend.database import AsyncSessionLocal


POLLUTED_REASONS = (
    "IS达标但OOS偏弱，做稳健性优化",
    "IS→OOS衰减明显：优先加平滑/增大窗口/提高decay",
    "IS/OOS均为负，淘汰",
)


def _build_sim_result(metrics: Dict[str, Any]) -> Dict[str, Any]:
    """Mirror evaluation.py:313-338 — build the same sim_result shape that
    should_optimize was originally fed."""
    train_sharpe_val = metrics.get("train_sharpe")
    train_fitness_val = metrics.get("train_fitness")
    test_sharpe_val = metrics.get("test_sharpe")
    test_fitness_val = metrics.get("test_fitness")
    return {
        "train": {
            "sharpe": train_sharpe_val if train_sharpe_val is not None else metrics.get("sharpe", 0),
            "fitness": train_fitness_val if train_fitness_val is not None else metrics.get("fitness", 0),
            "turnover": metrics.get("turnover", 0),
            "returns": metrics.get("returns", 0),
        },
        "test": {
            "sharpe": test_sharpe_val if test_sharpe_val is not None else metrics.get("sharpe", 0) * 0.8,
            "fitness": test_fitness_val if test_fitness_val is not None else metrics.get("fitness", 0),
        },
        "is": {
            "sharpe": metrics.get("sharpe", 0),
            "fitness": metrics.get("fitness", 0),
            "turnover": metrics.get("turnover", 0),
        },
        "riskNeutralized": metrics.get("riskNeutralized", {}),
        "investabilityConstrained": metrics.get("investabilityConstrained", {}),
    }


async def main(confirm: bool):
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(text(
            """
            SELECT id, alpha_id, factor_tier, quality_status, metrics
            FROM alphas
            WHERE metrics->>'_optimize_reason' = ANY(:reasons)
              AND (
                metrics->>'os_sharpe' IS NULL
                OR (
                  COALESCE((metrics->>'test_sharpe')::float, 0) = 0
                  AND COALESCE((metrics->>'test_fitness')::float, 0) = 0
                )
              )
            ORDER BY id
            """
        ), {"reasons": list(POLLUTED_REASONS)})).mappings().all()

        print(f"Found {len(rows)} polluted alpha(s).\n")
        changes: List[Dict] = []
        for row in rows:
            old_reason = row["metrics"].get("_optimize_reason")
            old_opt = row["metrics"].get("_should_optimize")
            new_opt, new_reason = should_optimize(_build_sim_result(row["metrics"]))
            changes.append({
                "id": row["id"],
                "alpha_id": row["alpha_id"],
                "tier": row["factor_tier"],
                "status": row["quality_status"],
                "old_opt": old_opt,
                "old_reason": old_reason,
                "new_opt": new_opt,
                "new_reason": new_reason,
            })

        for c in changes:
            mark = " " if c["old_reason"] == c["new_reason"] else "*"
            print(f"{mark} #{c['id']:<5} {c['alpha_id']:<10} tier={c['tier']} {c['status']:<22}")
            print(f"     old: opt={c['old_opt']!s:<5} reason={c['old_reason']!r}")
            print(f"     new: opt={c['new_opt']!s:<5} reason={c['new_reason']!r}")

        diff_count = sum(1 for c in changes if c["old_reason"] != c["new_reason"])
        print(f"\nWill update {diff_count} row(s). (* marks rows that change.)")

        if not confirm:
            print("\nDry-run only. Re-run with --confirm to apply.")
            return

        if diff_count == 0:
            print("\nNo changes to apply.")
            return

        for c in changes:
            if c["old_reason"] == c["new_reason"]:
                continue
            await s.execute(text(
                """
                UPDATE alphas
                SET metrics = jsonb_set(
                                 jsonb_set(metrics, '{_optimize_reason}', to_jsonb(cast(:reason AS text))),
                                 '{_should_optimize}', to_jsonb(cast(:opt AS boolean))
                             )
                WHERE id = :id
                """
            ), {"id": c["id"], "reason": c["new_reason"], "opt": c["new_opt"]})
        await s.commit()
        print(f"\nApplied {diff_count} update(s).")


if __name__ == "__main__":
    confirm = "--confirm" in sys.argv
    asyncio.run(main(confirm))
