"""Serially measure local self_corr for every can_submit=true unsubmitted alpha.

Why serial: get_with_fallback was observed to silently degrade to
(0.0, "unknown") when PnL fetches are run concurrently — _fetch_pnl_series
fails under burst and the result is indistinguishable from "actually
uncorrelated". Serial fetch is slow (~1-3s/alpha) but reliable.

For each alpha:
  1. fetch its PnL from BRAIN
  2. corrwith() against the local OS pool (os_pnls_USA.pkl)
  3. write the measured value back to metrics._self_corr + _self_corr_source
  4. demote can_submit=False when self_corr >= 0.7

Output: a ranked table — safe (<0.7) candidates first.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
from sqlalchemy import text  # noqa: E402

from backend.adapters.brain_adapter import BrainAdapter  # noqa: E402
from backend.database import AsyncSessionLocal  # noqa: E402
from backend.services.correlation_service import (  # noqa: E402
    LOOKBACK_YEARS,
    CorrelationService,
    _series_to_returns,
)

DEMOTE_THRESHOLD = 0.7
REGION = "USA"


async def main() -> int:
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(text("""
            SELECT id, alpha_id, expression, is_sharpe, is_fitness, is_turnover,
                   factor_tier, quality_status
            FROM alphas
            WHERE can_submit = true AND date_submitted IS NULL
              AND region = :region AND alpha_id IS NOT NULL
            ORDER BY is_sharpe DESC NULLS LAST
            LIMIT 500
        """), {"region": REGION})).fetchall()

    print(f"measuring {len(rows)} can_submit alphas (serial)...\n")

    async with BrainAdapter() as brain:
        svc = CorrelationService(brain)
        cache = svc._load_cache(REGION)
        if not cache or not cache.get("alpha_ids"):
            print("no local OS cache — abort")
            return 1
        os_ret = cache["pnls"].apply(lambda c: c - c.ffill().shift(1), axis=0)
        cutoff = os_ret.index.max() - pd.DateOffset(years=LOOKBACK_YEARS)
        os_ret = os_ret[os_ret.index > cutoff]

        measured: list = []
        for r in rows:
            try:
                pnl = await svc._fetch_pnl_series(r.alpha_id)
            except Exception as e:
                measured.append((r, None, f"fetch_err:{e}", None))
                continue
            if pnl.empty:
                measured.append((r, None, "pnl_empty", None))
                continue
            tgt = _series_to_returns(pnl)
            if len(tgt.dropna()) < 60:
                measured.append((r, None, "insufficient_overlap", None))
                continue
            os_local = os_ret.drop(columns=[r.alpha_id], errors="ignore")
            corrs = os_local.corrwith(tgt).dropna()
            if corrs.empty:
                measured.append((r, None, "no_corr", None))
                continue
            mx = float(corrs.max())
            counterpart = str(corrs.idxmax())
            measured.append((r, mx, "local", counterpart))

    # write back + demote
    async with AsyncSessionLocal() as db:
        from sqlalchemy.orm.attributes import flag_modified

        from backend.models import Alpha
        demoted = 0
        written = 0
        for r, mx, src, counterpart in measured:
            if src != "local" or mx is None:
                continue
            alpha = await db.get(Alpha, r.id)
            if not alpha:
                continue
            m = dict(alpha.metrics or {})
            m["_self_corr"] = round(mx, 4)
            m["_self_corr_source"] = "local"
            if counterpart:
                m["_self_corr_counterpart"] = counterpart
            alpha.metrics = m
            flag_modified(alpha, "metrics")
            written += 1
            if mx >= DEMOTE_THRESHOLD and alpha.can_submit:
                alpha.can_submit = False
                demoted += 1
        await db.commit()

    # report
    safe = [(r, mx, cp) for r, mx, src, cp in measured if src == "local" and mx is not None and mx < DEMOTE_THRESHOLD]
    high = [(r, mx, cp) for r, mx, src, cp in measured if src == "local" and mx is not None and mx >= DEMOTE_THRESHOLD]
    unknown = [(r, src) for r, mx, src, cp in measured if src != "local" or mx is None]

    safe.sort(key=lambda x: -(x[0].is_sharpe or 0))
    high.sort(key=lambda x: -(x[1] or 0))

    print(f"=== SAFE (self_corr < {DEMOTE_THRESHOLD}): {len(safe)} ===")
    for r, mx, cp in safe:
        print(f"  #{r.id} {r.alpha_id} T{r.factor_tier} sh={r.is_sharpe} fit={r.is_fitness} "
              f"to={r.is_turnover}  self_corr={mx:.4f} (vs {cp})")
        print(f"      {(r.expression or '')[:100]}")
    print(f"\n=== TOO HIGH (>= {DEMOTE_THRESHOLD}, demoted): {len(high)} ===")
    for r, mx, cp in high:
        print(f"  #{r.id} {r.alpha_id} sh={r.is_sharpe}  self_corr={mx:.4f} (vs {cp})")
    print(f"\n=== UNMEASURABLE: {len(unknown)} ===")
    for r, why in unknown:
        print(f"  #{r.id} {r.alpha_id}  {why}")

    print(f"\nwritten={written} demoted={demoted}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
