"""Phase 4 Sprint 0 — production baseline spike (2026-05-19).

Output: docs/sprint0_baseline_spike_<sh-date>.md (overwritten on re-run).

Purpose
-------
Calibrate two thresholds the Phase 4 plan needs BEFORE Sprint 1 PR ship:

  1. **R12 author baseline PASS rate (last 30d)** — the GO-gate target for
     R12 LLM_MODE=assistant: assistant mode must keep PASS rate within
     -10% of author baseline (bootstrap effect size) over the 30d obs
     window.  Without a baseline number the GO gate is unanchored — plan
     v5 §6.1 + §6.0.7 GO gate references "author baseline".

  2. **R14 task_stop_loss PASS_RATE_FLOOR (5th percentile)** — the per-task
     round-level PASS-rate floor below which R14 auto-pauses the task.
     A naive 5% default risks immediate auto-pause if the production p5
     is already below 5%; this spike grounds the default in current data.

Both numbers go into plan v5 §6.0.7 + the next sprint's flag/threshold
review.

Usage
-----
::

    python scripts/sprint0_baseline_spike.py

Reads ``DATABASE_URL`` from ``backend.config.settings``. No writes; only
SELECTs against the ``alphas`` table. Safe to run anytime.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running as a top-level script: add the repo root to sys.path.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlalchemy import text
from backend.database import AsyncSessionLocal


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

# (1) Global PASS rate for the last 30d — R12 GO-gate author baseline.
# Counts only finalized alphas (quality_status set). Excludes alphas with
# NULL quality_status (still in-flight).
SQL_AUTHOR_PASS_RATE = text("""
    SELECT
        COUNT(*) FILTER (WHERE quality_status = 'PASS') AS pass_n,
        COUNT(*) FILTER (WHERE quality_status IS NOT NULL) AS finalized_n,
        ROUND(
            COUNT(*) FILTER (WHERE quality_status = 'PASS')::numeric
            / NULLIF(COUNT(*) FILTER (WHERE quality_status IS NOT NULL), 0),
            4
        ) AS author_pass_rate_30d
    FROM alphas
    WHERE created_at > NOW() - INTERVAL '30 days'
""")


# (2) Round-level PASS rate per (task, round) for the last 30d, then
# 5th percentile across rounds — R14 PASS_RATE_FLOOR default candidate.
SQL_R14_FLOOR = text("""
    WITH round_stats AS (
        SELECT
            task_id,
            round_num,
            COUNT(*) FILTER (WHERE quality_status = 'PASS') AS pass_n,
            COUNT(*) AS total_n
        FROM alphas
        WHERE created_at > NOW() - INTERVAL '30 days'
          AND round_num IS NOT NULL
        GROUP BY task_id, round_num
        HAVING COUNT(*) >= 3  -- discard rounds with <3 alphas (not meaningful)
    )
    SELECT
        COUNT(*) AS round_n,
        ROUND(
            percentile_cont(0.05) WITHIN GROUP (
                ORDER BY pass_n::numeric / NULLIF(total_n, 0)
            )::numeric,
            4
        ) AS r14_floor_p5,
        ROUND(
            percentile_cont(0.10) WITHIN GROUP (
                ORDER BY pass_n::numeric / NULLIF(total_n, 0)
            )::numeric,
            4
        ) AS r14_floor_p10,
        ROUND(
            percentile_cont(0.50) WITHIN GROUP (
                ORDER BY pass_n::numeric / NULLIF(total_n, 0)
            )::numeric,
            4
        ) AS r14_floor_p50
    FROM round_stats
""")


# (3) Sentinel-stamp keys currently observed in alphas.metrics — verifies
# Sprint 0 PR0.6 stamp backfill (after at least one round runs post-deploy).
SQL_SENTINEL_STAMP_PRESENCE = text("""
    SELECT
        COUNT(*) FILTER (WHERE metrics ? '_r10_family_cap_dropped') AS r10_stamp_n,
        COUNT(*) FILTER (WHERE metrics ? '_g3_ast_originality_blocked') AS g3_stamp_n,
        COUNT(*) FILTER (WHERE metrics ? '_g5_crossover_parent_ids') AS g5_stamp_n,
        COUNT(*) FILTER (WHERE metrics ? '_r1b_mutation_triggered') AS r1b_stamp_n,
        COUNT(*) FILTER (WHERE metrics ? '_hypothesis_forest_reference') AS forest_stamp_n,
        COUNT(*) FILTER (WHERE metrics ? '_simulation_cache_hit') AS cache_stamp_n,
        COUNT(*) AS total_alpha_n
    FROM alphas
    WHERE created_at > NOW() - INTERVAL '7 days'
""")


async def _run() -> dict:
    out: dict = {
        "run_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    async with AsyncSessionLocal() as db:
        # (1) author baseline
        try:
            row = (await db.execute(SQL_AUTHOR_PASS_RATE)).first()
            out["author"] = {
                "pass_n": int(row.pass_n or 0),
                "finalized_n": int(row.finalized_n or 0),
                "author_pass_rate_30d": float(row.author_pass_rate_30d or 0.0),
            }
        except Exception as ex:
            out["author"] = {"error": str(ex)}

        # (2) R14 floor
        try:
            row = (await db.execute(SQL_R14_FLOOR)).first()
            out["r14"] = {
                "round_n": int(row.round_n or 0),
                "p5": float(row.r14_floor_p5 or 0.0),
                "p10": float(row.r14_floor_p10 or 0.0),
                "p50": float(row.r14_floor_p50 or 0.0),
            }
        except Exception as ex:
            out["r14"] = {"error": str(ex)}

        # (3) Sentinel stamp presence
        try:
            row = (await db.execute(SQL_SENTINEL_STAMP_PRESENCE)).first()
            out["stamps_7d"] = {
                "r10_family_cap_dropped": int(row.r10_stamp_n or 0),
                "g3_ast_originality_blocked": int(row.g3_stamp_n or 0),
                "g5_crossover_parent_ids": int(row.g5_stamp_n or 0),
                "r1b_mutation_triggered": int(row.r1b_stamp_n or 0),
                "hypothesis_forest_reference": int(row.forest_stamp_n or 0),
                "simulation_cache_hit": int(row.cache_stamp_n or 0),
                "total_alpha": int(row.total_alpha_n or 0),
            }
        except Exception as ex:
            out["stamps_7d"] = {"error": str(ex)}

    return out


def _to_markdown(data: dict) -> str:
    lines = [
        "# Sprint 0 Baseline Spike Report",
        "",
        f"**Run at (UTC)**: {data['run_at_utc']}",
        "",
        "## 1. R12 author baseline PASS rate (last 30d)",
        "",
        "```",
    ]
    a = data.get("author", {})
    if "error" in a:
        lines.append(f"ERROR: {a['error']}")
    else:
        lines += [
            f"finalized_n       = {a['finalized_n']:,}",
            f"pass_n            = {a['pass_n']:,}",
            f"author_pass_rate  = {a['author_pass_rate_30d']:.4f}  ({a['author_pass_rate_30d']*100:.2f}%)",
        ]
    lines += ["```", "", "## 2. R14 PASS_RATE_FLOOR calibration (last 30d round-level)", "", "```"]
    r = data.get("r14", {})
    if "error" in r:
        lines.append(f"ERROR: {r['error']}")
    else:
        lines += [
            f"round_n         = {r['round_n']:,}",
            f"p5  (R14 floor) = {r['p5']:.4f}  ({r['p5']*100:.2f}%)",
            f"p10             = {r['p10']:.4f}  ({r['p10']*100:.2f}%)",
            f"p50 (median)    = {r['p50']:.4f}  ({r['p50']*100:.2f}%)",
        ]
    lines += ["```", "", "## 3. Sentinel stamp presence (last 7d, PR0.6 verification)", "", "```"]
    s = data.get("stamps_7d", {})
    if "error" in s:
        lines.append(f"ERROR: {s['error']}")
    else:
        for k in (
            "r10_family_cap_dropped",
            "g3_ast_originality_blocked",
            "g5_crossover_parent_ids",
            "r1b_mutation_triggered",
            "hypothesis_forest_reference",
            "simulation_cache_hit",
        ):
            lines.append(f"{k:30s} = {s.get(k, 0):,}")
        lines.append(f"{'total_alpha':30s} = {s.get('total_alpha', 0):,}")
    lines += [
        "```",
        "",
        "## Action items",
        "",
        "- **R12 GO gate (plan v5 §6.1)**: `assistant_pass_rate >= author_pass_rate * 0.90` ",
        "  for 30d obs (bootstrap effect-size CI 不跨 0).",
        "- **R14 PASS_RATE_FLOOR (plan v5 §6.2 / config TASK_STOP_LOSS_PASS_RATE_FLOOR)**: set to ",
        "  `p5` value above (currently default 0.05 → may need adjustment).",
        "- **PR0.6 verification**: ALL 6 sentinel stamp counts above MUST be > 0 within 7d ",
        "  of Sprint 1 R12 ship; if `r1b_mutation_triggered` / `hypothesis_forest_reference` / ",
        "  `simulation_cache_hit` stay at 0, the corresponding source-of-truth path is broken.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    try:
        data = asyncio.run(_run())
    except Exception as ex:
        print(f"FATAL: spike failed before SQL stage: {ex}", file=sys.stderr)
        return 1

    md = _to_markdown(data)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = _REPO_ROOT / "docs" / f"sprint0_baseline_spike_{today}.md"
    out_path.write_text(md, encoding="utf-8")
    print(md)
    print()
    print(f"Report written to: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
