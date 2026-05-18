"""Canary red-flag check CLI (2026-05-18).

Implements §4 of `docs/production_canary_sop_2026_05_18.md`. Operator CLI
wrapping :func:`backend.tasks.canary_redflag.check_redflags`. The same
core helper is invoked by the Celery beat task `run_canary_redflag_check`
every 6h — keep them in sync via the shared module.

Usage:
    python scripts/canary_redflag_check.py [--t0 2026-05-18T17:00:00]

`--t0` is the canary start timestamp (ISO). Defaults to now() - 24h, which
suits T+24h sweeps; for T+1h / T+6h checks pass the actual T-0 string so
the windowed queries scope correctly.

Exit codes:
    0  all checks green
    1  ≥1 red-flag triggered
    2  DB error during check
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.tasks.canary_redflag import (  # noqa: E402
    RED_FLAGS,
    check_redflags,
)


def _parse_t0(s: str | None) -> datetime:
    if s is None:
        return datetime.utcnow() - timedelta(hours=24)
    return datetime.fromisoformat(s.replace("Z", "").rstrip())


def _print_results(results: list[dict]) -> int:
    """Returns red-flag count."""
    print(f"{'check':45s} {'value':>12s}  {'trigger':25s} {'status':10s}")
    print("-" * 100)
    red = 0
    for r in results:
        label = r["label"]
        # match predicate string from RED_FLAGS for display
        pred = next((p for lbl, _, p, _ in RED_FLAGS if lbl == label), "?")
        if "error" in r:
            print(f"{label:45s} {'DB_ERR':>12s}  {'-':25s} {'ERROR':10s}")
            print(f"  detail: {r['error']}", file=sys.stderr)
            red += 1
            continue
        value = r["value"]
        disp = f"{value:.4f}" if isinstance(value, float) else str(value)
        triggered = r["triggered"]
        status = "RED" if triggered else "green"
        print(f"{label:45s} {disp:>12s}  {pred:25s} {status:10s}")
        if triggered:
            red += 1
            print(f"  ▶ rollback target: {r['rollback']}", file=sys.stderr)
    return red


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--t0", default=None, help="Canary T-0 ISO timestamp (default: now-24h)")
    args = p.parse_args()
    t0 = _parse_t0(args.t0)
    print(f"canary red-flag check, T-0 = {t0.isoformat()}Z")
    try:
        results = asyncio.run(check_redflags(t0=t0))
    except Exception as e:
        print(f"check failed: {e}", file=sys.stderr)
        return 2
    fails = _print_results(results)
    if fails:
        print(f"\n[FAIL] {fails} red-flag(s) triggered — see SOP §5 for rollback SQL",
              file=sys.stderr)
        return 1
    print("\n[OK] all red-flag checks green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
