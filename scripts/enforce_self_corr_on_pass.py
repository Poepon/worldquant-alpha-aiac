"""Enforce local self-correlation check on PASS alpha.

User rule: "不通过的状态不能是 PASS" — if an alpha's local self-correlation
against the user's OS portfolio is ≥ 0.7, its quality_status should NOT
remain PASS. Downgrade to PASS_PROVISIONAL (not FAIL — alpha still meets
internal sharpe/fitness/turnover gate, only fails the corr requirement).

Skips:
- alpha already submitted (date_submitted IS NOT NULL) — those passed
  BRAIN's actual submit gate, including server-side self-corr; local
  cache is just an estimate and BRAIN trumps it.

Strategy:
- Concurrency=2 to stay under BRAIN rate limits and not interfere with
  active mining.
- Uses CorrelationService.get_with_fallback (local cache → BRAIN API).
- "unknown" source treated as inconclusive — leaves alpha at PASS.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.adapters.brain_adapter import BrainAdapter
from backend.services.correlation_service import CorrelationService

CORR_CUTOFF = 0.7
DB_URL = "postgresql+asyncpg://postgres:postgres@localhost:5433/alpha_gpt"


async def fetch_pass_alpha() -> list[dict]:
    e = create_async_engine(DB_URL)
    try:
        async with e.begin() as c:
            r = await c.execute(text("""
                SELECT id, alpha_id, region, expression
                FROM alphas
                WHERE quality_status = 'PASS'
                  AND alpha_id IS NOT NULL
                  AND date_submitted IS NULL
                ORDER BY id
            """))
            return [dict(row._mapping) for row in r.fetchall()]
    finally:
        await e.dispose()


async def downgrade(alpha_pk: int, corr: float) -> None:
    e = create_async_engine(DB_URL)
    try:
        async with e.begin() as c:
            await c.execute(text("""
                UPDATE alphas
                SET quality_status = 'PASS_PROVISIONAL',
                    metrics = jsonb_set(metrics, '{_self_corr_check}', to_jsonb(cast(:corr AS float))),
                    updated_at = NOW()
                WHERE id = :pk
            """), {"pk": alpha_pk, "corr": float(corr)})
    finally:
        await e.dispose()


async def check_one(svc: CorrelationService, alpha: dict, sem: asyncio.Semaphore) -> dict:
    async with sem:
        try:
            corr, src = await svc.get_with_fallback(
                alpha_id=alpha["alpha_id"],
                region=alpha.get("region") or "USA",
            )
        except Exception as e:
            return {**alpha, "corr": None, "src": "error", "err": str(e)[:80]}
        return {**alpha, "corr": corr, "src": src}


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--cutoff", type=float, default=CORR_CUTOFF)
    p.add_argument("--dry-run", action="store_true",
                   help="Compute but do not downgrade")
    args = p.parse_args()

    print(f"Fetching PASS alpha (excluding already submitted) ...")
    pass_alpha = await fetch_pass_alpha()
    print(f"Got {len(pass_alpha)} PASS candidates to check\n")

    sem = asyncio.Semaphore(args.concurrency)
    async with BrainAdapter() as adapter:
        await adapter.authenticate()
        svc = CorrelationService(adapter)

        # Process in chunks to print progress
        results = []
        chunk = 20
        for i in range(0, len(pass_alpha), chunk):
            batch = pass_alpha[i:i + chunk]
            print(f"Processing {i+1}-{i+len(batch)} of {len(pass_alpha)} ...")
            chunk_results = await asyncio.gather(
                *[check_one(svc, a, sem) for a in batch],
                return_exceptions=False,
            )
            results.extend(chunk_results)

    # Categorize
    safe = [r for r in results if r.get("src") == "local" and r.get("corr") is not None and r["corr"] < args.cutoff]
    block = [r for r in results if r.get("src") == "local" and r.get("corr") is not None and r["corr"] >= args.cutoff]
    unknown = [r for r in results if r.get("src") in ("unknown", "empty")]
    error = [r for r in results if r.get("src") == "error"]

    print()
    print(f"=== Summary ===")
    print(f"  SAFE (corr<{args.cutoff}, leave PASS):       {len(safe)}")
    print(f"  BLOCK (corr≥{args.cutoff}, downgrade PROV):  {len(block)}")
    print(f"  UNKNOWN (cache miss, leave PASS):            {len(unknown)}")
    print(f"  ERROR:                                       {len(error)}")

    if block:
        print(f"\nBLOCK details (top 10 by corr):")
        block.sort(key=lambda r: -r["corr"])
        for r in block[:10]:
            print(f"  pk={r['id']:>5} corr={r['corr']:.3f} {(r.get('expression') or '')[:80]}")

    if not args.dry_run and block:
        print(f"\nDowngrading {len(block)} alpha PASS → PASS_PROVISIONAL ...")
        for r in block:
            await downgrade(r["id"], r["corr"])
        print(f"Done.")
    elif args.dry_run:
        print(f"\n--dry-run: no DB changes")


if __name__ == "__main__":
    asyncio.run(main())
