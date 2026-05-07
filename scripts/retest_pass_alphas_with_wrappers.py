"""Re-test the 8 PASS alpha (276-283 batch) with wrapper variants.

Goal: Verify Fix C hypothesis — that group_neutralize / winsorize wrappers
can lift fitness ≥ 1.0 and disperse concentrated weight, making the
alphas BRAIN-submittable. Each base alpha gets 3 variants:

  V1: group_neutralize(<expr>, industry)   neut=INDUSTRY  (residualize for fit)
  V2: winsorize(<expr>, std=4)             neut=NONE      (clip extremes for CW)
  V3: group_neutralize(winsorize(<expr>, std=4), industry)  neut=INDUSTRY

24 simulates total. BRAIN slot=3 concurrent → ~30 min wall-clock.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make backend.* importable when run as a top-level script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.adapters.brain_adapter import BrainAdapter

PASS_PKS = (6570, 6577, 6580, 6583, 6584, 6585, 6589, 6590)


def make_variants(expr: str) -> list[dict]:
    """3 wrapper variants per base expression."""
    return [
        {
            "variant_id": "V1_industry_neutralize",
            "expression": f"group_neutralize({expr}, industry)",
            "neutralization": "INDUSTRY",
            "rationale": "industry residualization for fitness lift",
        },
        {
            "variant_id": "V2_winsorize_4std",
            "expression": f"winsorize({expr}, std=4)",
            "neutralization": "NONE",
            "rationale": "clip extreme weights for CONCENTRATED_WEIGHT",
        },
        {
            "variant_id": "V3_winsorize_then_neutralize",
            "expression": f"group_neutralize(winsorize({expr}, std=4), industry)",
            "neutralization": "INDUSTRY",
            "rationale": "combined: clip + residualize (defensive)",
        },
    ]


async def fetch_pass_alphas() -> list[dict]:
    e = create_async_engine(
        "postgresql+asyncpg://postgres:postgres@localhost:5433/alpha_gpt"
    )
    async with e.begin() as c:
        r = await c.execute(text(f"""
            SELECT id, alpha_id, expression, region, universe, delay, decay, truncation,
                   (metrics->>'sharpe')::float AS sh,
                   (metrics->>'fitness')::float AS fit,
                   (metrics->>'turnover')::float AS to_,
                   metrics->'_brain_failed_checks' AS failed
            FROM alphas WHERE id IN ({','.join(str(x) for x in PASS_PKS)})
            ORDER BY id
        """))
        rows = [dict(row._mapping) for row in r.fetchall()]
    await e.dispose()
    return rows


def extract_brain_metrics(sim_result: dict) -> dict:
    """Pull the comparable fields out of a BRAIN sim_result."""
    m = sim_result or {}
    is_data = m.get("is", {}) or {}
    checks = is_data.get("checks", []) or m.get("checks", []) or []
    failed = [c for c in checks if c.get("result") == "FAIL"]
    pending = [c for c in checks if c.get("result") == "PENDING"]
    can_submit = len(failed) == 0 and m.get("status") in ("COMPLETE", "WARNING")
    return {
        "status": m.get("status"),
        "sharpe": is_data.get("sharpe", m.get("sharpe", 0)),
        "fitness": is_data.get("fitness", m.get("fitness", 0)),
        "turnover": is_data.get("turnover", m.get("turnover", 0)),
        "returns": is_data.get("returns", m.get("returns", 0)),
        "drawdown": is_data.get("drawdown", m.get("drawdown", 0)),
        "can_submit": can_submit,
        "failed_check_names": [c.get("name") for c in failed],
        "pending_check_names": [c.get("name") for c in pending],
    }


async def simulate_one(adapter: BrainAdapter, base_pk: int, variant: dict, settings: dict, sem: asyncio.Semaphore) -> dict:
    async with sem:
        print(f"  → pk={base_pk} {variant['variant_id']}: {variant['expression'][:80]}")
        result = await adapter.simulate_alpha(
            expression=variant["expression"],
            region=settings["region"],
            universe=settings["universe"],
            delay=settings["delay"],
            decay=settings["decay"],
            neutralization=variant["neutralization"],
            truncation=settings["truncation"] or 0.08,
        )
        if not result.get("success"):
            print(f"    ✗ FAIL: {result.get('error', 'unknown')}")
            return {
                "base_pk": base_pk,
                "variant_id": variant["variant_id"],
                "expression": variant["expression"],
                "neutralization": variant["neutralization"],
                "success": False,
                "error": result.get("error"),
            }
        metrics = extract_brain_metrics(result)
        ok = "✓" if metrics["can_submit"] else "✗"
        print(f"    {ok} sh={metrics['sharpe']:.2f} fit={metrics['fitness']:.2f} to={metrics['turnover']:.2f} "
              f"can_submit={metrics['can_submit']} failed={metrics['failed_check_names']}")
        return {
            "base_pk": base_pk,
            "variant_id": variant["variant_id"],
            "expression": variant["expression"],
            "neutralization": variant["neutralization"],
            "rationale": variant["rationale"],
            "success": True,
            "alpha_id": result.get("alpha_id"),
            **metrics,
        }


async def main():
    print("Fetching 8 PASS alpha from DB ...")
    bases = await fetch_pass_alphas()
    print(f"Got {len(bases)} bases.\n")

    print("Generating 3 variants per base = 24 simulates total.")
    print("BRAIN concurrency: 3 slots. Estimated time: ~30 min.\n")

    tasks = []
    sem = asyncio.Semaphore(3)
    async with BrainAdapter() as adapter:
        # Pre-auth so concurrent calls share session
        await adapter.authenticate()
        for base in bases:
            settings = {
                "region": base["region"], "universe": base["universe"],
                "delay": base["delay"], "decay": base["decay"],
                "truncation": base["truncation"],
            }
            for variant in make_variants(base["expression"]):
                tasks.append(simulate_one(adapter, base["id"], variant, settings, sem))

        print(f"Dispatching {len(tasks)} simulates ...\n")
        results = await asyncio.gather(*tasks, return_exceptions=True)

    # Save raw results
    out_dir = Path(__file__).parent.parent / "docs"
    out_dir.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    out_path = out_dir / f"retest_pass_alphas_{timestamp}.json"

    serializable = []
    for r in results:
        if isinstance(r, Exception):
            serializable.append({"error": str(r), "exception_type": type(r).__name__})
        else:
            serializable.append(r)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "bases": [{k: (v if not isinstance(v, list) or k != 'failed' else [c.get('name') for c in (v or [])])
                       for k, v in b.items()} for b in bases],
            "results": serializable,
        }, f, indent=2, default=str)
    print(f"\nRaw results saved to {out_path}")

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY by base alpha")
    print("=" * 70)
    by_base: dict[int, list[dict]] = {}
    for r in serializable:
        if isinstance(r, dict) and "base_pk" in r:
            by_base.setdefault(r["base_pk"], []).append(r)

    submittable_count = 0
    fitness_lift_count = 0
    for base in bases:
        pk = base["id"]
        variants = by_base.get(pk, [])
        print(f"\npk={pk} sh={base['sh']:.2f}→fit={base['fit']:.2f} (orig)")
        print(f"  {base['expression'][:70]}")
        for v in variants:
            if not v.get("success"):
                print(f"  ✗ {v['variant_id']}: {v.get('error', 'sim failed')}")
                continue
            cs = "✓" if v["can_submit"] else "✗"
            fit_delta = v["fitness"] - base["fit"]
            print(f"  {cs} {v['variant_id']}: sh={v['sharpe']:.2f} fit={v['fitness']:.2f} (Δ{fit_delta:+.2f}) "
                  f"to={v['turnover']:.2f} failed={v['failed_check_names']}")
            if v["can_submit"]:
                submittable_count += 1
            if v["fitness"] >= 1.0 and base["fit"] < 1.0:
                fitness_lift_count += 1

    print("\n" + "=" * 70)
    print(f"Total simulates: {len(serializable)}")
    print(f"Submittable variants: {submittable_count}/24 ({100*submittable_count/24:.1f}%)")
    print(f"Variants lifting fitness ≥1.0 from below: {fitness_lift_count}")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
