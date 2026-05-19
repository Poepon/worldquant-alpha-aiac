"""Spike batch task launcher — Plan v5+ V-2 (N_task = 20-25).

Creates and starts N mining tasks via the backend HTTP API. Distributes by
region (USA-only by default; baseline showed only USA datafields synced) +
factor tier (T1/T2/T3 mix proportional to current PASS pool sizes).

Prerequisites:
  1. Backend running on :8001 (uvicorn backend.main:app --reload --port 8001)
  2. Celery worker running (celery -A backend.celery_app worker --pool=solo)
  3. Postgres aiac-db + Redis containers up
  4. BRAIN credentials configured in .env

Usage:
    python scripts/spike_launch.py --n 20                 # default mix
    python scripts/spike_launch.py --n 20 --tier-mix 50,30,20  # T1/T2/T3 %
    python scripts/spike_launch.py --n 5 --start-only --task-ids 100,101,...
    python scripts/spike_launch.py --dry-run              # show plan only

Note: 20 tasks × ~10 alphas/task = 200 alpha. With MAX_SIMULATIONS_PER_DAY=100
the run spans 2-3 days. Tasks queue up; Celery dispatches as the rate-limit
window allows.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from typing import List

import httpx


BASE_URL = "http://localhost:8001/api/v1"


def parse_tier_mix(s: str) -> tuple[int, int, int]:
    """Parse '50,30,20' → (50, 30, 20). Must sum to 100."""
    parts = [int(x.strip()) for x in s.split(",")]
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("tier-mix must be 3 comma-separated ints")
    if sum(parts) != 100:
        raise argparse.ArgumentTypeError(f"tier-mix must sum to 100, got {sum(parts)}")
    return tuple(parts)


def distribute_tiers(n: int, mix: tuple[int, int, int]) -> List[int]:
    """Convert n + (T1%, T2%, T3%) into a list of factor_tier values.

    Use largest-remainder method so the totals match n exactly.
    """
    raw = [n * pct / 100 for pct in mix]
    floors = [int(x) for x in raw]
    remainders = sorted(
        ((raw[i] - floors[i], i) for i in range(3)), reverse=True
    )
    short = n - sum(floors)
    counts = list(floors)
    for _, idx in remainders[:short]:
        counts[idx] += 1
    out = []
    for tier_idx, count in enumerate(counts):
        tier = tier_idx + 1
        out.extend([tier] * count)
    return out


def agent_mode_for_tier(tier: int) -> str:
    return {1: "AUTONOMOUS_TIER1", 2: "AUTONOMOUS_TIER2", 3: "AUTONOMOUS_TIER3"}[tier]


def create_one_task(
    client: httpx.Client,
    name: str,
    region: str,
    tier: int,
    daily_goal: int,
) -> dict:
    payload = {
        "name": name,
        "region": region,
        "universe": "TOP3000",
        "dataset_strategy": "AUTO",
        "target_datasets": [],
        "agent_mode": agent_mode_for_tier(tier),
        "daily_goal": daily_goal,
        "config": {"spike_run": True, "plan_version": "v5+"},
    }
    r = client.post(f"{BASE_URL}/tasks", json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def start_one_task(client: httpx.Client, task_id: int) -> dict:
    r = client.post(f"{BASE_URL}/tasks/{task_id}/start", timeout=30)
    r.raise_for_status()
    return r.json()


def health_check(client: httpx.Client) -> bool:
    try:
        r = client.get(f"{BASE_URL}/tasks?limit=1", timeout=5)
        return r.status_code == 200
    except (httpx.ConnectError, httpx.ReadTimeout):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=20, help="number of tasks")
    parser.add_argument(
        "--tier-mix", type=parse_tier_mix, default=(50, 30, 20),
        help="T1,T2,T3 percentage split (sums to 100)",
    )
    parser.add_argument("--region", default="USA", help="BRAIN region")
    parser.add_argument("--daily-goal", type=int, default=4)
    parser.add_argument("--prefix", default="spike", help="task name prefix")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--start-only", action="store_true",
        help="skip create, only start tasks listed in --task-ids",
    )
    parser.add_argument("--task-ids", default="", help="comma-separated IDs for --start-only")
    parser.add_argument("--gap-sec", type=float, default=2.0,
                        help="seconds between task starts to spread Celery load")
    args = parser.parse_args()

    tier_assignment = distribute_tiers(args.n, args.tier_mix)
    timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M")

    print("=" * 70)
    print(f"Spike batch launch — Plan v5+ V-2 ({args.n} tasks)")
    print("=" * 70)
    print(f"  Region:      {args.region}")
    print(f"  Tier mix:    T1={args.tier_mix[0]}% / T2={args.tier_mix[1]}% / T3={args.tier_mix[2]}%")
    print(f"  Daily goal:  {args.daily_goal}")
    print(f"  Tier breakdown: T1={tier_assignment.count(1)}, "
          f"T2={tier_assignment.count(2)}, T3={tier_assignment.count(3)}")
    print(f"  Expected alpha: ~{args.n * args.daily_goal * 2}-{args.n * args.daily_goal * 3} candidates")
    print(f"  Estimated days: {max(1, args.n * args.daily_goal * 2 // 100)} (with MAX_SIMULATIONS_PER_DAY=100)")
    print()

    if args.dry_run:
        print("[dry-run] would create:")
        for i, tier in enumerate(tier_assignment, 1):
            print(f"  {i:2}. {args.prefix}-{timestamp}-{i:02}-T{tier} ({agent_mode_for_tier(tier)})")
        return 0

    client = httpx.Client()
    if not health_check(client):
        print("ERROR: backend not reachable at http://localhost:8001/api/v1")
        print("  Start it with: uvicorn backend.main:app --reload --port 8001")
        return 1

    if args.start_only:
        task_ids = [int(x) for x in args.task_ids.split(",") if x.strip()]
        if not task_ids:
            print("ERROR: --start-only requires --task-ids")
            return 1
        print(f"Starting {len(task_ids)} pre-created tasks...")
        for tid in task_ids:
            try:
                result = start_one_task(client, tid)
                print(f"  task {tid}: started run_id={result.get('run_id')}")
                time.sleep(args.gap_sec)
            except httpx.HTTPStatusError as e:
                print(f"  task {tid}: FAILED — {e.response.status_code} {e.response.text}")
        return 0

    created_ids: List[int] = []
    failed_creations = 0
    print("Creating tasks...")
    for i, tier in enumerate(tier_assignment, 1):
        name = f"{args.prefix}-{timestamp}-{i:02}-T{tier}"
        try:
            t = create_one_task(client, name, args.region, tier, args.daily_goal)
            created_ids.append(t["id"])
            print(f"  [{i:2}/{args.n}] T{tier} created: id={t['id']:>4} {name}")
        except httpx.HTTPStatusError as e:
            failed_creations += 1
            print(f"  [{i:2}/{args.n}] T{tier} FAILED: {e.response.status_code} {e.response.text[:200]}")

    if not created_ids:
        print("ERROR: no tasks created; abort.")
        return 1

    if failed_creations:
        print(f"\n{failed_creations} task creations failed. "
              f"Continuing to start the {len(created_ids)} that succeeded.")

    print(f"\nStarting {len(created_ids)} tasks (gap={args.gap_sec}s between starts)...")
    started = 0
    for tid in created_ids:
        try:
            result = start_one_task(client, tid)
            print(f"  task {tid}: started run_id={result.get('run_id')}")
            started += 1
            time.sleep(args.gap_sec)
        except httpx.HTTPStatusError as e:
            print(f"  task {tid}: start FAILED — {e.response.status_code} {e.response.text[:200]}")

    print()
    print("=" * 70)
    print(f"Launched {started}/{len(created_ids)} tasks. Created IDs:")
    print(f"  {','.join(str(x) for x in created_ids)}")
    print("=" * 70)
    print()
    print("Monitor with:")
    print(f"  python scripts/spike_baseline_query.py")
    print(f"  watch -n 60 'docker exec aiac-db psql -U postgres -d alpha_gpt -c \"SELECT status, COUNT(*) FROM mining_tasks WHERE id IN ({','.join(str(x) for x in created_ids)}) GROUP BY status\"'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
