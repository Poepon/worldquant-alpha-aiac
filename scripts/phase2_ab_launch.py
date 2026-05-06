"""Phase 2 A/B launcher — split tasks between HYPOTHESIS_CENTRIC_LEVEL=1
(Phase 1 baseline) and LEVEL=2 (Phase 2 typed Hypothesis lifecycle).

Plan v5+ §B11: real-data validation that LEVEL=2 doesn't regress key
metrics (PASS rate / can_submit / cross-dataset rate) AND adds the
Phase 2-specific value (hypothesis lifecycle + KB hypothesis-keyed
entries).

Bypass FastAPI backend — dispatch directly via SQL + Celery so we don't
need uvicorn running.

Usage:
    python scripts/phase2_ab_launch.py --n 8                # 4+4 split
    python scripts/phase2_ab_launch.py --n 8 --daily-goal 4
    python scripts/phase2_ab_launch.py --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from backend.models import MiningTask
from backend.celery_app import celery_app


async def main(n: int, daily_goal: int, dry_run: bool) -> int:
    if n % 2 != 0 or n < 4:
        print(f"WARNING: n={n} should be ≥ 4 and even for clean A/B split")

    timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M")

    # Interleave variants 1 / 2 to avoid time-of-day bias in BRAIN simulate
    plan = []
    for i in range(n):
        variant = 1 if (i % 2 == 0) else 2
        plan.append((f"p2ab-{timestamp}-{i+1:02}-v{variant}", variant))

    print("=" * 70)
    print(f"Phase 2 A/B launcher — n={n} (LEVEL=1 vs LEVEL=2 50/50)")
    print("=" * 70)
    print(f"  Region: USA, Universe: TOP3000, Tier: T1")
    print(f"  Daily goal: {daily_goal}, Max iterations: 5")
    print(f"  Variant 1 (Phase 1 baseline): {sum(1 for _, v in plan if v == 1)} tasks")
    print(f"  Variant 2 (Phase 2 typed Hypothesis): {sum(1 for _, v in plan if v == 2)} tasks")
    print()
    for name, variant in plan:
        print(f"  {name}  variant={variant}")

    if dry_run:
        print("\n[dry-run] no tasks created.")
        return 0

    engine = create_async_engine(
        'postgresql+asyncpg://postgres:postgres@localhost:5433/alpha_gpt',
        echo=False,
    )
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    created = []
    async with maker() as s:
        for name, variant in plan:
            t = MiningTask(
                task_name=name,
                region="USA", universe="TOP3000",
                dataset_strategy="AUTO", target_datasets=[],
                agent_mode="AUTONOMOUS_TIER1",
                status="PENDING",
                daily_goal=daily_goal,
                # Plan v5+ §Phase 3 prep: 5 iterations let hypothesis lifecycle
                # truly cross rounds (vs 2 = barely 1 lifecycle transition).
                # B11-style A/B + Phase 3 readiness data both need this.
                max_iterations=5,
                config={
                    "phase2_ab": True,
                    "hypothesis_centric_variant": variant,
                },
            )
            s.add(t)
            await s.flush()
            created.append((t.id, variant, t.task_name))
        await s.commit()

    print()
    print("Tasks created. Dispatching to Celery (gap=2s)...")
    import time
    for tid, variant, name in created:
        result = celery_app.send_task(
            "backend.tasks.run_mining_task",
            args=[tid],
        )
        print(f"  task {tid} (var={variant}): celery_id={result.id}")
        time.sleep(2)

    await engine.dispose()

    print()
    print("=" * 70)
    v1_ids = [t[0] for t in created if t[1] == 1]
    v2_ids = [t[0] for t in created if t[1] == 2]
    print(f"Launched {len(created)} tasks.")
    print(f"  LEVEL=1 (Phase 1): {','.join(map(str, v1_ids))}")
    print(f"  LEVEL=2 (Phase 2): {','.join(map(str, v2_ids))}")
    print()
    print("Compare with:")
    print(f"  python scripts/phase2_ab_compare.py \\")
    print(f"      --phase1-ids {','.join(map(str, v1_ids))} \\")
    print(f"      --phase2-ids {','.join(map(str, v2_ids))}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=8, help="total tasks (50/50 split)")
    ap.add_argument("--daily-goal", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.n, args.daily_goal, args.dry_run)))
