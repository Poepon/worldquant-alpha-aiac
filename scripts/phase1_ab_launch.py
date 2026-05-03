"""Phase 1 A/B launcher — split spike tasks between HYPOTHESIS_CENTRIC_LEVEL
0 (legacy single-anchor) and 1 (cross-dataset) for direct comparison.

Plan v5+ §Phase 1 Step A6: e2e validation of cross-dataset hypothesis
generation. Each task carries its variant in `task.config.hypothesis_centric_variant`,
which mining_tasks.run_mining_task reads at run time.

Default: 8 tasks, 50/50 split, T1-only (T2/T3 cross-dataset effect requires
seed pool overlap which Phase 1 doesn't address yet).

Usage:
    python scripts/phase1_ab_launch.py --n 8                  # 50/50
    python scripts/phase1_ab_launch.py --n 8 --tier-mix 100,0,0   # T1 only
    python scripts/phase1_ab_launch.py --n 8 --dry-run

Pre-flight:
  - Worker must be restarted after the Phase 1 commits (47dc208 onward)
  - HYPOTHESIS_CENTRIC_LEVEL is read at task creation time from .env or
    task.config — this script writes the per-task config explicitly so
    you don't need to flip the global flag.
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
    parts = [int(x.strip()) for x in s.split(",")]
    if len(parts) != 3 or sum(parts) != 100:
        raise argparse.ArgumentTypeError("tier-mix must be 3 ints summing to 100")
    return tuple(parts)


def distribute_tiers(n: int, mix: tuple[int, int, int]) -> List[int]:
    raw = [n * pct / 100 for pct in mix]
    floors = [int(x) for x in raw]
    remainders = sorted(((raw[i] - floors[i], i) for i in range(3)), reverse=True)
    short = n - sum(floors)
    counts = list(floors)
    for _, idx in remainders[:short]:
        counts[idx] += 1
    out = []
    for ti, c in enumerate(counts):
        out.extend([ti + 1] * c)
    return out


def agent_mode(tier: int) -> str:
    return {1: "AUTONOMOUS_TIER1", 2: "AUTONOMOUS_TIER2", 3: "AUTONOMOUS_TIER3"}[tier]


def create_task(client: httpx.Client, name: str, region: str, tier: int,
                daily_goal: int, variant: int) -> dict:
    payload = {
        "name": name,
        "region": region,
        "universe": "TOP3000",
        "dataset_strategy": "AUTO",
        "target_datasets": [],
        "agent_mode": agent_mode(tier),
        "daily_goal": daily_goal,
        "config": {
            "phase1_ab": True,
            "hypothesis_centric_variant": variant,  # 0 = legacy, 1 = Phase 1
        },
    }
    r = client.post(f"{BASE_URL}/tasks", json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def start_task(client: httpx.Client, task_id: int) -> dict:
    r = client.post(f"{BASE_URL}/tasks/{task_id}/start", timeout=30)
    r.raise_for_status()
    return r.json()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--tier-mix", type=parse_tier_mix, default=(100, 0, 0),
                    help="T1,T2,T3 percent; default T1-only since Phase 1 "
                         "targets hypothesis routing, not seed pool")
    ap.add_argument("--region", default="USA")
    ap.add_argument("--daily-goal", type=int, default=4)
    ap.add_argument("--prefix", default="ph1ab")
    ap.add_argument("--gap-sec", type=float, default=2.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.n < 4 or args.n % 2 != 0:
        print("WARNING: --n should be ≥ 4 and even for clean 50/50 split")

    tiers = distribute_tiers(args.n, args.tier_mix)
    # Stable interleave: alternate variant 0 / 1 across the list
    variants = [(i % 2) for i in range(args.n)]
    timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M")

    print("=" * 70)
    print(f"Phase 1 A/B launcher — {args.n} tasks (50/50 LEVEL=0 vs LEVEL=1)")
    print("=" * 70)
    print(f"  Region:       {args.region}")
    print(f"  Tier mix:     T1={args.tier_mix[0]}% / T2={args.tier_mix[1]}% / T3={args.tier_mix[2]}%")
    print(f"  Tier counts:  T1={tiers.count(1)} T2={tiers.count(2)} T3={tiers.count(3)}")
    print(f"  Variant 0 (legacy):  {variants.count(0)} tasks")
    print(f"  Variant 1 (Phase 1): {variants.count(1)} tasks")
    print(f"  Daily goal:   {args.daily_goal}")
    print()
    print("Plan:")
    for i, (tier, var) in enumerate(zip(tiers, variants), 1):
        name = f"{args.prefix}-{timestamp}-{i:02}-T{tier}-v{var}"
        print(f"  {i:>2}. {name}  ({agent_mode(tier)}, variant={var})")
    print()

    if args.dry_run:
        print("[dry-run] No changes made.")
        return 0

    client = httpx.Client()
    try:
        r = client.get(f"{BASE_URL}/tasks?limit=1", timeout=5)
        r.raise_for_status()
    except (httpx.ConnectError, httpx.HTTPStatusError):
        print("ERROR: backend not reachable on :8001")
        return 1

    created: List[tuple[int, int]] = []  # (id, variant)
    print("Creating tasks...")
    for i, (tier, var) in enumerate(zip(tiers, variants), 1):
        name = f"{args.prefix}-{timestamp}-{i:02}-T{tier}-v{var}"
        try:
            t = create_task(client, name, args.region, tier, args.daily_goal, var)
            created.append((t["id"], var))
            print(f"  [{i:>2}/{args.n}] var={var} created id={t['id']:<5} {name}")
        except httpx.HTTPStatusError as e:
            print(f"  [{i:>2}/{args.n}] FAILED: {e.response.status_code} {e.response.text[:200]}")

    if not created:
        print("ERROR: no tasks created")
        return 1

    print(f"\nStarting {len(created)} tasks (gap={args.gap_sec}s)...")
    started = 0
    for tid, var in created:
        try:
            r = start_task(client, tid)
            print(f"  task {tid} (var={var}): started run_id={r.get('run_id')}")
            started += 1
            time.sleep(args.gap_sec)
        except httpx.HTTPStatusError as e:
            print(f"  task {tid}: start FAILED — {e.response.status_code}")

    print("=" * 70)
    legacy_ids = [tid for tid, v in created if v == 0]
    p1_ids = [tid for tid, v in created if v == 1]
    print(f"Launched {started}/{len(created)} tasks.")
    print(f"  Variant 0 (legacy):  {','.join(str(x) for x in legacy_ids)}")
    print(f"  Variant 1 (Phase 1): {','.join(str(x) for x in p1_ids)}")
    print("=" * 70)
    print()
    print("Compare with:")
    print(f"  python scripts/phase1_ab_compare.py --legacy-ids {','.join(str(x) for x in legacy_ids)} \\")
    print(f"      --phase1-ids {','.join(str(x) for x in p1_ids)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
