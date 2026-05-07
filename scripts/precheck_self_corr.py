"""Self-correlation pre-submit check.

Symptom this prevents: BRAIN's `/alphas/{id}/submit` returns
"Self-correlation X.XXXX is above cutoff of 0.7 ..." after a submission
attempt, wasting daily submission quota. Mining-time `is.checks` only
shows SELF_CORRELATION as PENDING because BRAIN computes it against your
submitted-alpha portfolio at submit-time, not simulate-time.

This script pulls the max self-correlation server-side (or from local
cache when present) for a list of candidate alpha and reports a
go/no-go ranking. Run BEFORE submitting to avoid quota waste.

Usage:
    # By internal pk (looks up alpha_id from DB)
    python scripts/precheck_self_corr.py --pks 6607,2535,1145

    # By BRAIN alpha_id directly
    python scripts/precheck_self_corr.py --alpha-ids 2ra5mmob,RRk5r56j

    # All can_submit=true unsubmitted alpha (default)
    python scripts/precheck_self_corr.py
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

# BRAIN's published cutoff: corr >= 0.7 → submission rejected unless new
# sharpe is materially better (the "10% better than max-corr neighbor" rule).
SELF_CORR_CUTOFF = 0.7


async def fetch_candidates(pks: list[int] | None, alpha_ids: list[str] | None) -> list[dict]:
    """Resolve input to a uniform list of {pk, alpha_id, region, sharpe, fitness, expression}."""
    e = create_async_engine(
        "postgresql+asyncpg://postgres:postgres@localhost:5433/alpha_gpt"
    )
    async with e.begin() as c:
        if pks:
            r = await c.execute(text("""
                SELECT id AS pk, alpha_id, region, expression,
                       (metrics->>'sharpe')::float AS sh,
                       (metrics->>'fitness')::float AS fit,
                       (metrics->>'turnover')::float AS to_,
                       date_submitted
                FROM alphas WHERE id = ANY(:pks) AND alpha_id IS NOT NULL
                ORDER BY id
            """), {"pks": pks})
        elif alpha_ids:
            r = await c.execute(text("""
                SELECT id AS pk, alpha_id, region, expression,
                       (metrics->>'sharpe')::float AS sh,
                       (metrics->>'fitness')::float AS fit,
                       (metrics->>'turnover')::float AS to_,
                       date_submitted
                FROM alphas WHERE alpha_id = ANY(:aids)
                ORDER BY id
            """), {"aids": alpha_ids})
        else:
            # Default: all can_submit=true unsubmitted
            r = await c.execute(text("""
                SELECT id AS pk, alpha_id, region, expression,
                       (metrics->>'sharpe')::float AS sh,
                       (metrics->>'fitness')::float AS fit,
                       (metrics->>'turnover')::float AS to_,
                       date_submitted
                FROM alphas
                WHERE can_submit = true
                  AND date_submitted IS NULL
                  AND alpha_id IS NOT NULL
                ORDER BY (metrics->>'sharpe')::float DESC NULLS LAST
            """))
        rows = [dict(row._mapping) for row in r.fetchall()]
    await e.dispose()
    return rows


async def precheck_one(svc: CorrelationService, c: dict, sem: asyncio.Semaphore) -> dict:
    async with sem:
        try:
            corr, source = await svc.get_with_fallback(
                alpha_id=c["alpha_id"],
                region=c.get("region") or "USA",
            )
        except Exception as e:
            return {**c, "self_corr": None, "source": "error", "error": str(e)[:120]}
        return {**c, "self_corr": corr, "source": source}


def classify(self_corr: float | None, source: str, sharpe: float | None) -> str:
    if self_corr is None:
        return "ERR"
    if source == "unknown":
        return "UNKNOWN"
    if self_corr >= 0.85:
        return "BLOCK_DUP"   # near-duplicate of submitted; hopeless
    if self_corr >= SELF_CORR_CUTOFF:
        # BRAIN's "10% better sharpe" override exists but we can't verify
        # without knowing the most-correlated submitted neighbor's sharpe.
        # Conservative default: BLOCK and let user manually decide.
        return "BLOCK_CORR"
    if self_corr >= 0.5:
        return "RISKY"
    return "SAFE"


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--pks", type=str, default=None,
                   help="Comma-separated internal pk ids")
    p.add_argument("--alpha-ids", type=str, default=None,
                   help="Comma-separated BRAIN alpha_ids")
    p.add_argument("--concurrency", type=int, default=4,
                   help="Parallel BRAIN /correlations/SELF calls (default 4; "
                        "endpoint is rate-limited and slow ~5-30s each)")
    args = p.parse_args()

    pks = [int(x.strip()) for x in args.pks.split(",")] if args.pks else None
    alpha_ids = [x.strip() for x in args.alpha_ids.split(",")] if args.alpha_ids else None

    print("Pulling candidates from DB ...")
    candidates = await fetch_candidates(pks, alpha_ids)
    if not candidates:
        print("No candidates found.")
        return
    print(f"Got {len(candidates)} candidate alpha. Running self-corr precheck "
          f"(concurrency={args.concurrency}) ...\n")

    sem = asyncio.Semaphore(args.concurrency)
    async with BrainAdapter() as adapter:
        await adapter.authenticate()
        svc = CorrelationService(adapter)
        results = await asyncio.gather(
            *[precheck_one(svc, c, sem) for c in candidates],
            return_exceptions=False,
        )

    # Sort: SAFE first (lowest corr), then RISKY, then BLOCK*, ERR last
    rank_order = {"SAFE": 0, "RISKY": 1, "UNKNOWN": 2, "BLOCK_CORR": 3, "BLOCK_DUP": 4, "ERR": 5}
    for r in results:
        r["verdict"] = classify(r.get("self_corr"), r.get("source", "unknown"), r.get("sh"))
    results.sort(key=lambda r: (rank_order.get(r["verdict"], 9),
                                r.get("self_corr") if r.get("self_corr") is not None else 999))

    print(f"{'verdict':>10}  {'pk':>5}  {'brain_id':>10}  {'corr':>5}  {'src':>7}  "
          f"{'sh':>5}  {'fit':>5}  {'to':>5}  expr")
    print("-" * 110)
    for r in results:
        corr_str = f"{r['self_corr']:.3f}" if r.get("self_corr") is not None else "  ?  "
        sh = r.get("sh") or 0
        fit = r.get("fit") or 0
        to = r.get("to_") or 0
        expr = (r.get("expression") or "")[:60]
        print(f"{r['verdict']:>10}  {r.get('pk', '-'):>5}  {r['alpha_id']:>10}  "
              f"{corr_str:>5}  {r.get('source','?'):>7}  "
              f"{sh:>5.2f}  {fit:>5.2f}  {to:>5.2f}  {expr}")

    # Summary by verdict
    print()
    from collections import Counter
    verdicts = Counter(r["verdict"] for r in results)
    for v in ("SAFE", "RISKY", "UNKNOWN", "BLOCK_CORR", "BLOCK_DUP", "ERR"):
        if v in verdicts:
            print(f"  {v}: {verdicts[v]}")

    safe = [r for r in results if r["verdict"] == "SAFE"]
    if safe:
        print(f"\n→ Recommended next submit: pk={safe[0]['pk']} "
              f"(brain_id={safe[0]['alpha_id']}, "
              f"self_corr={safe[0]['self_corr']:.3f}, "
              f"sh={safe[0]['sh']:.2f})")


if __name__ == "__main__":
    asyncio.run(main())
