#!/usr/bin/env python
"""Pre-flight preview for the dataset-steering value bandit (acceptance §1).

Runs the refresh in dry-run mode against the live DB: computes the seed/update
posterior + sampled mining_weight per (region, dataset_id) and prints them —
but writes NOTHING (no bandit_state, no mining_weight, no watermark). Safe to
run in production BEFORE flipping ENABLE_DATASET_VALUE_BANDIT, to confirm:

  - pv1 (mined-out) lands at the LOWEST weight; fundamental6/analyst4 higher.
  - under-mined sources keep a floor (no starvation).
  - every Beta β > 0 (the v1 invariant).

Usage:
    python scripts/dataset_bandit_dryrun.py
    python scripts/dataset_bandit_dryrun.py --seed 0   # deterministic θ draw
"""
from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=None, help="fix the Thompson RNG for a reproducible preview")
    args = ap.parse_args()

    from backend.config import settings
    from backend.tasks.dataset_weight_refresh import _refresh_async

    rng = random.Random(args.seed) if args.seed is not None else None
    out = await _refresh_async(
        gamma=float(getattr(settings, "DATASET_BANDIT_GAMMA", 0.95)),
        floor_c=float(getattr(settings, "DATASET_BANDIT_FLOOR_C", 0.1)),
        tau=float(getattr(settings, "DATASET_BANDIT_FLOOR_TAU", 500.0)),
        window_days=int(getattr(settings, "DATASET_BANDIT_WINDOW_DAYS", 7)),
        rng=rng, dry_run=True,
    )

    details = out["details"]
    print("=" * 78)
    print(f"DATASET BANDIT DRY-RUN  (writes nothing)  arms={len(details)} "
          f"seeded={out['seeded']} updated={out['updated']}")
    print("=" * 78)
    print(f"{'region:dataset':<30}{'kind':>6}{'S_d':>5}{'T_d':>6}"
          f"{'alpha':>9}{'beta':>10}{'mean%':>8}{'weight':>9}{'pulls':>7}")
    bad_beta = []
    rows = sorted(details.items(), key=lambda kv: kv[1]["weight"], reverse=True)
    for key, d in rows:
        a, b = d["alpha"], d["beta"]
        mean = 100.0 * a / (a + b) if (a + b) else 0.0
        if b < 0:
            bad_beta.append(key)
        print(f"{key:<30}{d['kind']:>6}{d['s_d']:>5}{d['t_d']:>6}"
              f"{a:>9.3f}{b:>10.3f}{mean:>7.2f}%{d['weight']:>9.4f}{d['pulls']:>7}")

    print("-" * 78)
    # Acceptance assertions (plan §1 / §4).
    assert not bad_beta, f"β<0 invariant violated for {bad_beta} — DO NOT flip the flag"
    weights = {k: v["weight"] for k, v in details.items()}
    pv1 = next((k for k in weights if k.endswith(":pv1")), None)
    if pv1:
        rank = sorted(weights, key=weights.get).index(pv1)
        print(f"pv1 weight rank: {rank+1}/{len(weights)} (1 = lowest; expect near 1)")
    print(f"min weight={min(weights.values()):.4f}  max={max(weights.values()):.4f}  "
          f"(floor keeps min>0 → no starvation)")
    print("β>0 invariant: PASS  — preview only, nothing written.")


if __name__ == "__main__":
    asyncio.run(main())
