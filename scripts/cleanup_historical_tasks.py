"""Cleanup historical mining tasks that produced no PASS alphas (preserve
those that did + any RUNNING task + all alphas).

Modes:
  --preview (default)  read-only summary of what would be deleted
  --confirm            actually run the cleanup in a single transaction

What gets deleted (for tasks NOT in keep set):
  - alpha_failures rows
  - trace_steps rows
  - experiment_runs rows
  - mining_tasks rows themselves

What's preserved:
  - All alphas (their task_id / trace_step_id / run_id are nulled out for
    deleted-task references; factor_tier / metrics / quality_status untouched)
  - All knowledge_entries
  - All alpha_status_transitions
  - Tasks that produced ≥1 PASS alpha
  - Tasks currently in RUNNING status (defensive — you might be testing one)
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from typing import List, Set

from sqlalchemy import text


async def collect_keep_set(db) -> Set[int]:
    """task_ids to keep = (produced PASS alpha) ∪ (RUNNING)."""
    pass_q = text(
        "SELECT DISTINCT task_id FROM alphas "
        "WHERE quality_status = 'PASS' AND task_id IS NOT NULL"
    )
    pass_ids = {row[0] for row in (await db.execute(pass_q)).all()}

    running_q = text("SELECT id FROM mining_tasks WHERE status = 'RUNNING'")
    running_ids = {row[0] for row in (await db.execute(running_q)).all()}

    return pass_ids | running_ids


async def preview() -> None:
    from backend.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        keep = await collect_keep_set(db)

        all_tasks_q = text(
            "SELECT id, task_name, status, agent_mode, region, created_at "
            "FROM mining_tasks ORDER BY id"
        )
        all_tasks = (await db.execute(all_tasks_q)).all()
        keep_rows = [t for t in all_tasks if t[0] in keep]
        del_rows = [t for t in all_tasks if t[0] not in keep]

        print("=" * 78)
        print("Historical task cleanup — PREVIEW")
        print("=" * 78)
        print(f"Total tasks: {len(all_tasks)}")
        print(f"Tasks to KEEP: {len(keep_rows)}")
        for tid, name, status, mode, region, created in keep_rows:
            reason = "RUNNING" if status == "RUNNING" else "produced PASS alpha"
            print(f"  #{tid:<3} {(name or '')[:32]:<32} {status:<14} ({reason})")

        print()
        print(f"Tasks to DELETE: {len(del_rows)}")
        for tid, name, status, mode, region, created in del_rows:
            print(f"  #{tid:<3} {(name or '')[:32]:<32} {status:<14} {mode}")

        if not del_rows:
            print("  (none)")
            return

        del_ids = [r[0] for r in del_rows]

        for label, sql in (
            ("trace_steps", "SELECT COUNT(*) FROM trace_steps WHERE task_id = ANY(:ids)"),
            ("alpha_failures", "SELECT COUNT(*) FROM alpha_failures WHERE task_id = ANY(:ids)"),
            ("experiment_runs", "SELECT COUNT(*) FROM experiment_runs WHERE task_id = ANY(:ids)"),
            ("alphas (will detach, NOT delete)", "SELECT COUNT(*) FROM alphas WHERE task_id = ANY(:ids)"),
        ):
            n = (await db.execute(text(sql), {"ids": del_ids})).scalar()
            print(f"  {label}: {n}")


async def apply() -> None:
    from backend.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        keep = await collect_keep_set(db)

        all_ids_q = text("SELECT id FROM mining_tasks")
        all_ids = [row[0] for row in (await db.execute(all_ids_q)).all()]
        del_ids = [tid for tid in all_ids if tid not in keep]

        if not del_ids:
            print("Nothing to delete.")
            return

        print(f"Deleting {len(del_ids)} tasks: {sorted(del_ids)}")

        # Find experiment_run ids and trace_step ids about to die — alphas
        # that reference them must be detached first to honor FK constraints.
        run_ids_q = text("SELECT id FROM experiment_runs WHERE task_id = ANY(:ids)")
        run_ids = [row[0] for row in (await db.execute(run_ids_q), {"ids": del_ids})[0].all()] \
            if False else [row[0] for row in (await db.execute(run_ids_q, {"ids": del_ids})).all()]
        step_ids_q = text("SELECT id FROM trace_steps WHERE task_id = ANY(:ids)")
        step_ids = [row[0] for row in (await db.execute(step_ids_q, {"ids": del_ids})).all()]

        # 1. Detach alphas: nullify task_id / run_id / trace_step_id refs.
        #    alphas table itself is preserved (factor_tier and metrics intact).
        await db.execute(
            text("UPDATE alphas SET task_id = NULL WHERE task_id = ANY(:ids)"),
            {"ids": del_ids},
        )
        if run_ids:
            await db.execute(
                text("UPDATE alphas SET run_id = NULL WHERE run_id = ANY(:ids)"),
                {"ids": run_ids},
            )
        if step_ids:
            await db.execute(
                text("UPDATE alphas SET trace_step_id = NULL WHERE trace_step_id = ANY(:ids)"),
                {"ids": step_ids},
            )

        # 2. Delete alpha_failures (depends on tasks/runs/steps)
        af_result = await db.execute(
            text("DELETE FROM alpha_failures WHERE task_id = ANY(:ids)"),
            {"ids": del_ids},
        )
        # 3. Delete trace_steps
        ts_result = await db.execute(
            text("DELETE FROM trace_steps WHERE task_id = ANY(:ids)"),
            {"ids": del_ids},
        )
        # 4. Delete experiment_runs
        er_result = await db.execute(
            text("DELETE FROM experiment_runs WHERE task_id = ANY(:ids)"),
            {"ids": del_ids},
        )
        # 5. Delete mining_tasks themselves
        mt_result = await db.execute(
            text("DELETE FROM mining_tasks WHERE id = ANY(:ids)"),
            {"ids": del_ids},
        )

        await db.commit()

        print(f"Deleted: alpha_failures={af_result.rowcount} "
              f"trace_steps={ts_result.rowcount} "
              f"experiment_runs={er_result.rowcount} "
              f"mining_tasks={mt_result.rowcount}")


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", action="store_true",
                        help="Actually delete (default is dry-run preview)")
    args = parser.parse_args()
    try:
        if args.confirm:
            asyncio.run(apply())
        else:
            asyncio.run(preview())
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    cli()
