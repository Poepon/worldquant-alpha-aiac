"""Backlog-drain ranker — turn the can_submit backlog into a ready-to-submit shortlist.

Competitive analysis v3 (docs/competitive_analysis_v3_2026-05-26.md) verdict: we are
SELECTION-limited (11 submitted vs 121 can_submit un-submitted). The binding gate is
self-correlation < 0.7 vs OUR OWN submitted pool (BRAIN's same-region rule), then the
portfolio-marginal scorecard. This drains the backlog by running BOTH gates + a greedy
mutual-orthogonality pass, producing an ORDERED submit shortlist.

Gate 1 (can_submit, no BRAIN FAIL) — already true for the backlog by construction.
Gate 2 (self-corr) — fetch each candidate's PnL, diff to DAILY returns, max-corr vs the
        CURRENT submitted pool's daily returns (fetched fresh, not the maybe-stale OS cache).
        Keep self_corr < SELF_CORR_MAX (0.7).
Gate 3 (marginal) — AlphaService.get_marginal_contribution (BRAIN before-and-after) →
        composite_score + SUBMIT/NEUTRAL/SKIP (the AlphaDetail scorecard). Only run on
        Gate-2 survivors (saves BRAIN calls).
Greedy set — among (Gate2 ∧ recommendation==SUBMIT), sort by composite desc and greedily
        admit a candidate only if its DAILY-return corr vs every already-admitted candidate
        is < 0.7 (the CrisperX "mutually-orthogonal set you can submit together" problem).

RECOMMEND-ONLY: prints + writes docs/backlog_drain_<date>.{md,json}. Never submits
(submission is irreversible + user-gated). PnL/marginal fetches don't consume sim slots.

Usage:
  venv/Scripts/python.exe scripts/backlog_drain_rank.py [--region USA] [--limit N] [--team deLkl06]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import select

sys.path.insert(0, ".")

from backend.adapters.brain_adapter import BrainAdapter
from backend.database import AsyncSessionLocal
from backend.models import Alpha
from backend.services.alpha_service import AlphaService
from backend.services.correlation_service import CorrelationService, _series_to_returns

SELF_CORR_MAX = 0.7


async def _pool_returns(svc: CorrelationService, pool_ids):
    """Daily returns of the current submitted pool, fetched fresh (not the OS cache)."""
    out = {}
    for aid in pool_ids:
        s = await svc._fetch_pnl_series(aid)
        if s is not None and len(s) > 0:
            r = _series_to_returns(s).dropna()
            if len(r) > 0:
                out[aid] = r
    return out


def _max_corr(target_returns: pd.Series, pool: dict):
    """Max |daily-return corr| of target vs each pool series; None if unmeasurable."""
    best = None
    for aid, pr in pool.items():
        j = pd.concat([target_returns.rename("t"), pr.rename("s")], axis=1).dropna()
        if len(j) > 60:
            c = abs(j["t"].corr(j["s"]))
            if pd.notna(c):
                best = c if best is None else max(best, c)
    return best


async def main(region, limit, competition, team_id):
    competition = (competition or "").strip() or None
    team_id = (team_id or "").strip() or None
    if competition is None and team_id is None:
        from backend.config import settings
        competition, team_id = settings.iqc_audit_scope()

    async with AsyncSessionLocal() as db:
        pool_rows = (await db.execute(
            select(Alpha.alpha_id).where(
                Alpha.date_submitted.isnot(None), Alpha.alpha_id.isnot(None),
                Alpha.region == region,
            )
        )).scalars().all()
        backlog_stmt = (
            select(Alpha).where(
                Alpha.can_submit == True,  # noqa: E712
                Alpha.date_submitted.is_(None), Alpha.task_id.isnot(None),
                Alpha.alpha_id.isnot(None), Alpha.region == region,
            ).order_by(Alpha.is_sharpe.desc().nullslast())
        )
        if limit:
            backlog_stmt = backlog_stmt.limit(limit)
        backlog = (await db.execute(backlog_stmt)).scalars().all()
        print(f"=== backlog-drain | region={region} | pool(submitted)={len(pool_rows)} | backlog(can_submit,unsubmitted,AIAC)={len(backlog)} ===", flush=True)

        sem = asyncio.Semaphore(4)
        async with BrainAdapter() as brain:
            svc = CorrelationService(brain)
            asvc = AlphaService(db)
            pool = await _pool_returns(svc, pool_rows)
            print(f"fetched {len(pool)}/{len(pool_rows)} submitted-pool PnL series", flush=True)

            # ---- Gate 2: self-corr vs pool (concurrent PnL fetch) ----
            async def g2(a):
                async with sem:
                    s = await svc._fetch_pnl_series(a.alpha_id)
                if s is None or len(s) == 0:
                    return a, None, None  # unmeasurable
                tr = _series_to_returns(s).dropna()
                if len(tr) < 60:
                    return a, None, None
                return a, _max_corr(tr, pool), tr

            g2_res = await asyncio.gather(*[g2(a) for a in backlog])
            cands = []  # (alpha, self_corr, daily_returns)
            for a, sc, tr in g2_res:
                tag = "UNMEAS" if sc is None else ("PASS" if sc < SELF_CORR_MAX else "FAIL")
                print(f"  [G2 {tag:6}] {a.alpha_id} {a.dataset_id or '?':<12} self_corr={('%.3f'%sc) if sc is not None else 'NA'} sh={a.is_sharpe}", flush=True)
                if sc is not None and sc < SELF_CORR_MAX:
                    cands.append({"a": a, "self_corr": sc, "ret": tr})
            print(f"\nGate-2 survivors (self_corr<{SELF_CORR_MAX}): {len(cands)}/{len(backlog)}", flush=True)

            # ---- Gate 3: marginal scorecard on G2 survivors ----
            for c in cands:
                a = c["a"]
                try:
                    r = await asvc.get_marginal_contribution(alpha_pk=a.id, competition=competition, team_id=team_id, brain_adapter=brain)
                    an = (r or {}).get("analysis") or {}
                    c["composite"] = an.get("composite_score")
                    c["rec"] = an.get("recommendation")
                    c["deltas"] = (r or {}).get("deltas") or {}
                except Exception as e:  # noqa: BLE001
                    c["composite"], c["rec"], c["deltas"] = None, f"ERR:{str(e)[:40]}", {}
                cs = c["composite"]
                print(f"  [G3] {a.alpha_id} rec={c['rec']} comp={('%+.3f'%cs) if isinstance(cs,(int,float)) else '—'} self_corr={c['self_corr']:.3f}", flush=True)

    # ---- rank + greedy mutually-orthogonal submit set ----
    def _ck(c):
        return c["composite"] if isinstance(c.get("composite"), (int, float)) else -9e9
    cands.sort(key=_ck, reverse=True)
    submit_ready = [c for c in cands if c.get("rec") == "SUBMIT" or (isinstance(c.get("composite"), (int, float)) and c["composite"] > 0)]

    greedy = []
    for c in submit_ready:
        ok = True
        for g in greedy:
            j = pd.concat([c["ret"].rename("t"), g["ret"].rename("s")], axis=1).dropna()
            if len(j) > 60 and pd.notna(j["t"].corr(j["s"])) and abs(j["t"].corr(j["s"])) >= SELF_CORR_MAX:
                ok = False
                break
        if ok:
            greedy.append(c)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    out = Path("docs"); out.mkdir(exist_ok=True)
    jdat = {"region": region, "pool_submitted": len(pool_rows), "backlog": len(backlog),
            "gate2_survivors": len(cands), "submit_ready": len(submit_ready), "greedy_set": len(greedy),
            "submit_order": [{"brain_id": c["a"].alpha_id, "dataset": c["a"].dataset_id,
                              "self_corr": round(c["self_corr"], 4), "composite": c.get("composite"),
                              "rec": c.get("rec"), "is_sharpe": float(c["a"].is_sharpe or 0),
                              "is_margin": float(c["a"].is_margin or 0), "expr": (c["a"].expression or "")[:160]}
                             for c in greedy]}
    (out / f"backlog_drain_{today}.json").write_text(json.dumps(jdat, indent=2), encoding="utf-8")

    lines = [f"# Backlog-drain submit shortlist — {today}", "",
             f"region **{region}** | submitted pool **{len(pool_rows)}** | backlog (can_submit, unsubmitted, AIAC) **{len(backlog)}**",
             f"Gate-2 (self_corr<{SELF_CORR_MAX}) survivors: **{len(cands)}** | Gate-3 SUBMIT-ready: **{len(submit_ready)}** | greedy mutually-orthogonal set: **{len(greedy)}**", "",
             "## READY TO SUBMIT (in this order — mutually orthogonal, marginal-positive)", "",
             "| # | brain_id | dataset | self_corr | composite | rec | is_sharpe | margin(bps) | expr |",
             "|---|---|---|---|---|---|---|---|---|"]
    for i, c in enumerate(greedy, 1):
        a = c["a"]; cs = c.get("composite")
        mb = (float(a.is_margin or 0) * 10000)
        lines.append(f"| {i} | `{a.alpha_id}` | {a.dataset_id or '?'} | {c['self_corr']:.3f} | "
                     f"{('%+.3f'%cs) if isinstance(cs,(int,float)) else '—'} | {c.get('rec') or '—'} | "
                     f"{a.is_sharpe} | {mb:.0f} | `{(a.expression or '')[:60]}` |")
    lines += ["", "## All Gate-2 survivors (incl. NEUTRAL/SKIP, ranked by composite)", "",
              "| brain_id | dataset | self_corr | composite | rec |", "|---|---|---|---|---|"]
    for c in cands:
        a = c["a"]; cs = c.get("composite")
        lines.append(f"| `{a.alpha_id}` | {a.dataset_id or '?'} | {c['self_corr']:.3f} | "
                     f"{('%+.3f'%cs) if isinstance(cs,(int,float)) else '—'} | {c.get('rec') or '—'} |")
    (out / f"backlog_drain_{today}.md").write_text("\n".join(lines), encoding="utf-8")

    print(f"\n=== DONE ===\n  Gate-2 survivors {len(cands)} / submit-ready {len(submit_ready)} / greedy set {len(greedy)}")
    print(f"  -> docs/backlog_drain_{today}.md (+ .json)")
    if greedy:
        print("  READY TO SUBMIT (order):", ", ".join(c["a"].alpha_id for c in greedy))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="USA")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--competition", default=None)
    ap.add_argument("--team", default=None)
    args = ap.parse_args()
    asyncio.run(main(args.region, args.limit, args.competition, args.team))
