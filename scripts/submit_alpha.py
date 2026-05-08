"""Submit a single alpha to BRAIN for evaluation.

Usage:
    python scripts/submit_alpha.py --pk 1134
    python scripts/submit_alpha.py --alpha-id bloYwrer

Mirrors ace_lib.py:submit_alpha() pattern:
  POST /alphas/{id}/submit  → kicks off submission
  if Retry-After header     → poll via GET /alphas/{id}/submit
  status 200 + no Retry     → terminal (success or rejection in body)

On success, writes alpha.date_submitted in the DB.

Pre-flight checks:
  1. alpha_id present
  2. can_submit=true (per latest refresh)
  3. date_submitted IS NULL (not already submitted)
  4. self_corr precheck: if --skip-precheck not set, runs CorrelationService
     and refuses to submit when local corr ≥ 0.7 (would waste quota).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.adapters.brain_adapter import BrainAdapter
from backend.services.correlation_service import CorrelationService

CORR_HARD_BLOCK = 0.7   # BRAIN's published cutoff; precheck refuses


async def fetch_alpha(pk: int | None, alpha_id: str | None) -> dict | None:
    e = create_async_engine(
        "postgresql+asyncpg://postgres:postgres@localhost:5433/alpha_gpt"
    )
    try:
        async with e.begin() as c:
            if pk is not None:
                r = await c.execute(text("""
                    SELECT id, alpha_id, region, expression, can_submit, date_submitted,
                           (metrics->>'sharpe')::float AS sh,
                           (metrics->>'fitness')::float AS fit,
                           (metrics->>'turnover')::float AS to_
                    FROM alphas WHERE id = :pk
                """), {"pk": pk})
            else:
                r = await c.execute(text("""
                    SELECT id, alpha_id, region, expression, can_submit, date_submitted,
                           (metrics->>'sharpe')::float AS sh,
                           (metrics->>'fitness')::float AS fit,
                           (metrics->>'turnover')::float AS to_
                    FROM alphas WHERE alpha_id = :aid
                """), {"aid": alpha_id})
            row = r.fetchone()
            return dict(row._mapping) if row else None
    finally:
        await e.dispose()


async def mark_submitted(pk: int) -> None:
    e = create_async_engine(
        "postgresql+asyncpg://postgres:postgres@localhost:5433/alpha_gpt"
    )
    try:
        async with e.begin() as c:
            await c.execute(text("""
                UPDATE alphas SET date_submitted = NOW(), updated_at = NOW()
                WHERE id = :pk
            """), {"pk": pk})
    finally:
        await e.dispose()


async def submit(adapter: BrainAdapter, alpha_id: str, max_polls: int = 60) -> dict:
    """POST /alphas/{id}/submit then poll until terminal.

    Returns: {success: bool, status_code: int, body: dict|str, polls: int}
    """
    print(f"  POST /alphas/{alpha_id}/submit ...")
    resp = await adapter._safe_api_call("POST", f"/alphas/{alpha_id}/submit")
    polls = 0

    while polls < max_polls:
        retry_after = resp.headers.get("retry-after") or resp.headers.get("Retry-After")
        if retry_after:
            wait_s = float(retry_after)
            print(f"  poll {polls+1}: 202 Retry-After={wait_s}s ...")
            await asyncio.sleep(wait_s)
            resp = await adapter._safe_api_call("GET", f"/alphas/{alpha_id}/submit")
            polls += 1
            continue
        break

    body_text = resp.text or ""
    try:
        body_json = resp.json() if body_text else {}
    except Exception:
        body_json = body_text

    return {
        "success": resp.status_code == 200,
        "status_code": resp.status_code,
        "body": body_json,
        "polls": polls,
    }


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--pk", type=int, default=None)
    p.add_argument("--alpha-id", type=str, default=None, dest="alpha_id")
    p.add_argument("--skip-precheck", action="store_true",
                   help="Skip self-corr precheck (NOT recommended)")
    p.add_argument("--force", action="store_true",
                   help="Submit even if can_submit=false or already submitted")
    args = p.parse_args()

    if args.pk is None and args.alpha_id is None:
        p.error("--pk or --alpha-id required")

    print(f"Resolving alpha (pk={args.pk}, alpha_id={args.alpha_id}) ...")
    alpha = await fetch_alpha(args.pk, args.alpha_id)
    if alpha is None:
        print("✗ Not found in DB.")
        return 1

    print(f"  pk={alpha['id']}  alpha_id={alpha['alpha_id']}  region={alpha['region']}")
    print(f"  sh={alpha['sh']:.2f}  fit={alpha['fit']:.2f}  to={alpha['to_']:.2f}")
    print(f"  can_submit={alpha['can_submit']}  date_submitted={alpha['date_submitted']}")
    print(f"  expr: {alpha['expression']}")

    # Pre-flight gates
    if not alpha.get("alpha_id"):
        print("✗ No BRAIN alpha_id — cannot submit.")
        return 1
    if alpha.get("date_submitted") and not args.force:
        print(f"✗ Already submitted at {alpha['date_submitted']}. Use --force to re-submit.")
        return 1
    if not alpha.get("can_submit") and not args.force:
        print("✗ can_submit=false in DB. Use --force to override.")
        return 1

    async with BrainAdapter() as adapter:
        await adapter.authenticate()

        if not args.skip_precheck:
            print("\nRunning self-corr precheck ...")
            svc = CorrelationService(adapter)
            corr, src = await svc.get_with_fallback(
                alpha_id=alpha["alpha_id"], region=alpha["region"] or "USA",
            )
            print(f"  self_corr={corr:.3f}  source={src}")
            if src != "unknown" and corr >= CORR_HARD_BLOCK:
                print(f"✗ self_corr {corr:.3f} >= {CORR_HARD_BLOCK} — refusing to submit "
                      f"(BRAIN would reject; would waste quota). Use --skip-precheck to override.")
                return 1
            if src == "unknown":
                print("  (UNKNOWN: precheck inconclusive; proceeding but BRAIN may still reject)")

        print(f"\nSubmitting alpha_id={alpha['alpha_id']} to BRAIN ...")
        result = await submit(adapter, alpha["alpha_id"])

        if result["success"]:
            # Post-submit hook (2026-05-08): refresh OS PnL cache so future
            # precheck reflects the just-submitted alpha as a corr neighbor.
            # Note: BRAIN's PnL recordset endpoint may take hours-1day to
            # populate for a newly-submitted alpha, so this refresh may be
            # a no-op now and the actual incorporation happens on the next
            # refresh (e.g. weekly Celery beat or next submit-triggered call).
            print("\nRefreshing OS PnL cache (incremental) ...")
            try:
                svc_post = CorrelationService(adapter)
                new_n, total_n = await svc_post.refresh_os_alpha_cache(
                    region=alpha["region"] or "USA", incremental=True,
                )
                print(f"  cache: {new_n} new PnL series, {total_n} total")
                if new_n == 0 and alpha.get("alpha_id"):
                    print(f"  (note: BRAIN PnL for {alpha['alpha_id']} not yet populated; "
                          f"retry refresh in 24-48h)")
            except Exception as e:
                print(f"  cache refresh failed (non-fatal): {e}")

            # P2 portfolio skeletons cache (2026-05-08): used by T1 strategy
            # prompt to discourage LLM from re-generating same-skeleton alpha.
            # DB-only refresh (~10ms), no BRAIN dependency.
            print("Refreshing portfolio skeletons cache ...")
            try:
                from backend.agents.seed_pool.portfolio_skeletons import (
                    refresh_portfolio_from_db,
                )
                n = await refresh_portfolio_from_db(region=alpha["region"] or "USA")
                print(f"  cache: {n} submitted alpha skeletons")
            except Exception as e:
                print(f"  skeleton refresh failed (non-fatal): {e}")

            # #2 field-fitness stats (2026-05-08): used by T1 strategy prompt
            # to nudge LLM toward field families with historical fit ≥ 1.0.
            # DB-only refresh.
            print("Refreshing field-fitness cache ...")
            try:
                from backend.agents.seed_pool.field_fitness_stats import (
                    refresh_field_fitness_cache,
                )
                n = await refresh_field_fitness_cache(region=alpha["region"] or "USA")
                print(f"  cache: {n} high-fit fields")
            except Exception as e:
                print(f"  field-fitness refresh failed (non-fatal): {e}")

    print(f"\n=== RESULT ===")
    print(f"  status_code: {result['status_code']}")
    print(f"  polls: {result['polls']}")
    print(f"  body: {str(result['body'])[:400]}")

    if result["success"]:
        print(f"\n✅ SUBMIT SUCCESS  pk={alpha['id']}  alpha_id={alpha['alpha_id']}")
        await mark_submitted(alpha["id"])
        print(f"  DB: alpha.date_submitted = NOW()")
        return 0
    else:
        print(f"\n✗ SUBMIT FAILED  status={result['status_code']}")
        # Try to parse the BRAIN rejection reason
        body = result["body"]
        if isinstance(body, dict):
            msg = body.get("message") or body.get("error") or str(body)
            print(f"  reason: {msg}")
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
