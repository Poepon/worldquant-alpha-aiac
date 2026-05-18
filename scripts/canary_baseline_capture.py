"""Canary baseline snapshot capture (2026-05-18).

Implements §2 of `docs/production_canary_sop_2026_05_18.md`. Run at T-0
(pre-canary) and again at T+24h, then diff the two JSON outputs.

Usage:
    python scripts/canary_baseline_capture.py --label T-0
    python scripts/canary_baseline_capture.py --label T+24h
    python scripts/canary_baseline_capture.py --diff docs/canary_T-0_2026-05-18.json

Output:
    docs/canary_<label>_<YYYY-MM-DD>.json with the 9 baseline metrics + ISO
    timestamp + git HEAD. Exit code 0 on capture success, 1 on DB error.

Designed to be run from cron or by hand; no API token, direct DB read.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from backend.database import AsyncSessionLocal  # noqa: E402


_QUERIES: Dict[str, str] = {
    "flag_count_on": (
        "SELECT COUNT(*) FROM feature_flag_overrides WHERE flag_value='true'"
    ),
    "tasks_failed_pct_7d": (
        "SELECT COALESCE((COUNT(*) FILTER (WHERE status='FAILED'))::float "
        "/ NULLIF(COUNT(*), 0), 0.0) "
        "FROM mining_tasks WHERE created_at > now() - interval '7 day'"
    ),
    "alphas_passed_24h": (
        "SELECT COUNT(*) FROM alphas WHERE created_at > now() - interval '24 hour' "
        "AND (metrics->>'is_passed')::bool = true"
    ),
    "kb_total_entries": (
        "SELECT COUNT(*) FROM knowledge_entries WHERE is_active"
    ),
    "r1a_attribution_rows_24h": (
        "SELECT COUNT(*) FROM r1a_attribution_log "
        "WHERE created_at > now() - interval '24 hour'"
    ),
    "r1b_retry_rows_24h": (
        "SELECT COUNT(*) FROM r1b_retry_log "
        "WHERE created_at > now() - interval '24 hour'"
    ),
    "r8_query_rows_24h": (
        "SELECT COUNT(*) FROM r8_query_log "
        "WHERE created_at > now() - interval '24 hour'"
    ),
    "brain_sim_count_24h": (
        "SELECT COUNT(DISTINCT alpha_id) FROM alphas "
        "WHERE created_at > now() - interval '24 hour'"
    ),
    "mining_tasks_running_now": (
        "SELECT COUNT(*) FROM mining_tasks WHERE status='RUNNING'"
    ),
}


async def _capture() -> Dict[str, object]:
    snap: Dict[str, object] = {
        "captured_at": datetime.utcnow().isoformat() + "Z",
        "git_head": _git_head(),
        "metrics": {},
    }
    async with AsyncSessionLocal() as s:
        for k, sql in _QUERIES.items():
            try:
                r = await s.execute(text(sql))
                snap["metrics"][k] = r.scalar()
            except Exception as e:
                snap["metrics"][k] = {"error": str(e)[:200]}
    return snap


def _git_head() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        return "unknown"


def _print_diff(prior_path: Path) -> None:
    prior = json.loads(prior_path.read_text(encoding="utf-8"))
    current = asyncio.run(_capture())
    print(f"baseline: {prior_path.name}  ({prior.get('captured_at')})")
    print(f"current : in-memory       ({current['captured_at']})")
    print(f"{'metric':35s} {'baseline':>15s} {'current':>15s} {'delta':>15s}")
    print("-" * 85)
    for k in _QUERIES:
        b = prior["metrics"].get(k)
        c = current["metrics"].get(k)
        try:
            delta = float(c) - float(b)  # type: ignore[arg-type]
            print(f"{k:35s} {b!s:>15s} {c!s:>15s} {delta:>+15.3f}")
        except (TypeError, ValueError):
            print(f"{k:35s} {b!s:>15s} {c!s:>15s} {'n/a':>15s}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--label", default="T-0",
                   help="Snapshot label (e.g. T-0, T+24h). Used in filename.")
    p.add_argument("--out-dir", default="docs",
                   help="Output directory (default: docs)")
    p.add_argument("--diff", metavar="PRIOR_JSON",
                   help="Run capture and print diff vs prior snapshot, no save.")
    args = p.parse_args()

    if args.diff:
        prior = Path(args.diff)
        if not prior.exists():
            print(f"prior snapshot not found: {prior}", file=sys.stderr)
            return 1
        _print_diff(prior)
        return 0

    snap = asyncio.run(_capture())
    out = Path(args.out_dir) / (
        f"canary_{args.label}_{datetime.utcnow().strftime('%Y-%m-%d')}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snap, indent=2), encoding="utf-8")
    print(f"snapshot saved: {out}")
    for k, v in snap["metrics"].items():
        print(f"  {k:35s} = {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
