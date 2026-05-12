"""Pairwise self-correlation among submission candidates.

precheck_self_corr.py checks each candidate vs the ALREADY-submitted
portfolio. This script checks pairwise correlation BETWEEN candidates
to identify which combinations BRAIN's SELF_CORR check would reject if
all submitted at once.

Algorithm:
  1. Fetch BRAIN /alphas/{id}/recordsets/pnl for each candidate
  2. Convert to daily returns (pct_change of cumulative PnL)
  3. Pearson correlation matrix
  4. Print NxN heatmap + identify clusters (|corr| >= 0.7 = redundant)
  5. Recommend a diversified subset using greedy max-coverage:
     - Sort candidates by external metric (Δscore from IQC audit)
     - Iterate top-down; accept if max corr to already-accepted < 0.7

Usage:
  python scripts/pairwise_self_corr.py --alpha-ids 9qAEOpVq,6XanRvlp,...
  python scripts/pairwise_self_corr.py --from-audit docs/iqc_audit/audit_2026-05-11_1231.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.adapters.brain_adapter import BrainAdapter


CORR_CUTOFF = 0.7   # BRAIN's documented threshold


async def fetch_pnl_series(brain: BrainAdapter, alpha_id: str) -> pd.Series | None:
    """Fetch alpha's IS PnL series from BRAIN; return as pd.Series indexed by date."""
    try:
        data = await brain.get_alpha_pnl(alpha_id)
    except Exception as e:
        print(f"  {alpha_id} PnL fetch error: {e}")
        return None
    if not data:
        return None
    # BRAIN returns {records: [[date, pnl, ...], ...], schema: {properties: [...]}}
    records = data.get("records") or data.get("is", {}).get("pnl", {}).get("records")
    if not records:
        # Try recordsets shape
        records = (
            data.get("recordsets", {}).get("pnl", {}).get("records")
        )
    if not records:
        print(f"  {alpha_id} no records in PnL response (keys={list(data.keys())[:5]})")
        return None
    rows = []
    for r in records:
        if not isinstance(r, list) or len(r) < 2:
            continue
        # records[i] = [date, pnl]
        rows.append((r[0], float(r[1])))
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["date", "pnl"])
    df["date"] = pd.to_datetime(df["date"])
    df.set_index("date", inplace=True)
    df.sort_index(inplace=True)
    return df["pnl"]


async def main(alpha_ids: list[str], rankings: dict[str, float] | None) -> None:
    print(f"=== Pairwise self-correlation check ({len(alpha_ids)} candidates) ===\n")
    pnl_series: dict[str, pd.Series] = {}

    async with BrainAdapter() as brain:
        for i, aid in enumerate(alpha_ids):
            print(f"  [{i+1}/{len(alpha_ids)}] fetching PnL: {aid}...")
            series = await fetch_pnl_series(brain, aid)
            if series is not None:
                pnl_series[aid] = series
            await asyncio.sleep(0.5)  # be nice to BRAIN

    print()
    if len(pnl_series) < 2:
        print(f"Only {len(pnl_series)} series fetched, can't compute pairwise. Exiting.")
        return

    # Build aligned PnL DataFrame
    df = pd.DataFrame(pnl_series)
    df = df.dropna(how="all").fillna(method="ffill")
    print(f"Aligned PnL: {df.shape[0]} dates × {df.shape[1]} alphas")

    # Daily returns (pct_change of cumulative PnL)
    returns = df.diff().dropna()
    if returns.empty:
        print("Daily returns empty — PnL may not be cumulative. Trying raw correlation.")
        returns = df

    # Pearson corr matrix
    corr = returns.corr()
    print()
    print("=== Pearson correlation matrix (daily returns) ===")
    print(corr.round(2).to_string())

    # Identify clusters: high-corr pairs
    print()
    print(f"=== High-correlation pairs (|corr| >= {CORR_CUTOFF}) ===")
    pairs = []
    cols = list(corr.columns)
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            c = corr.iloc[i, j]
            if abs(c) >= CORR_CUTOFF:
                pairs.append((cols[i], cols[j], c))
    if not pairs:
        print("  (none — all candidates are diversified)")
    else:
        for a, b, c in sorted(pairs, key=lambda x: -abs(x[2])):
            print(f"  {a:12s} ↔ {b:12s} corr={c:+.3f}")

    # Greedy diversified subset
    print()
    print("=== Greedy diversified submission set ===")
    if rankings:
        # Order candidates by ranking (descending = best first)
        ordered = sorted(
            [a for a in cols if a in rankings],
            key=lambda a: -rankings[a],
        )
    else:
        ordered = cols

    accepted: list[str] = []
    rejected_by: dict[str, str] = {}
    for cand in ordered:
        ok = True
        for acc in accepted:
            if abs(corr.loc[cand, acc]) >= CORR_CUTOFF:
                rejected_by[cand] = acc
                ok = False
                break
        if ok:
            accepted.append(cand)

    print(f"Submit (in order, total {len(accepted)}):")
    for i, a in enumerate(accepted, 1):
        score = rankings.get(a) if rankings else None
        score_str = f"Δscore={score:+}" if isinstance(score, (int, float)) else ""
        print(f"  {i}. {a}  {score_str}")

    if rejected_by:
        print(f"\nRejected as redundant ({len(rejected_by)}):")
        for cand, acc in rejected_by.items():
            c = corr.loc[cand, acc]
            score = rankings.get(cand) if rankings else None
            score_str = f"(Δscore={score:+})" if isinstance(score, (int, float)) else ""
            print(f"  {cand} {score_str} — too similar to {acc} (corr={c:+.3f})")

    # Save report
    out_dir = Path("docs/pairwise_corr")
    out_dir.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    out_path = out_dir / f"pairwise_{stamp}.json"
    out_path.write_text(json.dumps({
        "alpha_ids": alpha_ids,
        "corr_matrix": corr.round(4).to_dict(),
        "high_corr_pairs": [{"a": a, "b": b, "corr": float(c)} for a, b, c in pairs],
        "accepted": accepted,
        "rejected_by": rejected_by,
        "rankings": rankings,
    }, indent=2), encoding="utf-8")
    print(f"\nReport: {out_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--alpha-ids", type=str, default=None,
                    help="Comma-separated BRAIN alpha_ids")
    ap.add_argument("--from-audit", type=str, default=None,
                    help="Path to IQC audit JSON; auto-extract positive Δscore alphas + use as rankings")
    args = ap.parse_args()

    rankings = None
    if args.from_audit:
        data = json.loads(Path(args.from_audit).read_text(encoding="utf-8"))
        rankings = {}
        ids = []
        for r in data["results"]:
            ds = r["deltas"].get("score")
            if isinstance(ds, (int, float)) and ds > 0:
                ids.append(r["brain_id"])
                rankings[r["brain_id"]] = ds
        print(f"From audit: extracted {len(ids)} positive-Δscore alphas")
        alpha_ids = ids
    elif args.alpha_ids:
        alpha_ids = [x.strip() for x in args.alpha_ids.split(",") if x.strip()]
    else:
        print("Need --alpha-ids or --from-audit")
        sys.exit(1)

    asyncio.run(main(alpha_ids, rankings))
