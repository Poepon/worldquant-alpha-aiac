"""Phase 1 A/B comparison — variant 0 (legacy) vs variant 1 (cross-dataset).

Reads task IDs split by variant, aggregates metrics, prints a side-by-side
report and writes docs/phase1_ab_report_<date>.md.

Usage:
    python scripts/phase1_ab_compare.py \
        --legacy-ids 50,52,54,56 \
        --phase1-ids 51,53,55,57

Metrics compared:
  - PASS rate (alpha-level, OS-validated)
  - Cross-dataset rate (alpha.fields_used spans ≥2 dataset_ids)
  - PASS train/test sharpe ratio (V-12 health)
  - Distinct dataset_ids touched (V-13 + Phase 1 effect)
  - Mean BRAIN sim quota / per task
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import List

import psycopg2
import psycopg2.extras


def parse_ids(s: str) -> List[int]:
    return [int(x.strip()) for x in s.split(",") if x.strip()]


def agg_for(conn, ids: List[int]) -> dict:
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    if not ids:
        return {"n_tasks": 0, "alphas_pass": 0, "alphas_fail": 0}

    cur.execute("""
        SELECT
            COUNT(DISTINCT task_id) AS n_tasks_with_alpha,
            COUNT(*) AS pass_alphas,
            COUNT(*) FILTER (WHERE quality_status = 'PASS') AS pass_strict,
            COUNT(*) FILTER (WHERE quality_status = 'PASS_PROVISIONAL') AS pass_prov,
            COUNT(*) FILTER (WHERE quality_status = 'OPTIMIZE') AS optimize,
            ROUND(AVG((metrics->>'sharpe')::float)::numeric, 2) AS train_sharpe_avg,
            ROUND(AVG((metrics->>'test_sharpe')::float)::numeric, 2) AS test_sharpe_avg,
            COUNT(*) FILTER (WHERE (metrics->>'sharpe')::float >= 5
                                 AND ((metrics->>'test_sharpe')::float = 0
                                  OR (metrics->>'test_sharpe')::float IS NULL))
                AS suspected_overfit
        FROM alphas WHERE task_id = ANY(%s)
    """, (ids,))
    row = cur.fetchone()
    s = dict(row) if row else {}

    cur.execute("""
        SELECT COUNT(*) FROM alpha_failures WHERE task_id = ANY(%s)
    """, (ids,))
    s["fail_alphas"] = cur.fetchone()["count"]

    cur.execute("""
        SELECT COUNT(DISTINCT dataset_id) AS n_distinct_dsets
        FROM alphas WHERE task_id = ANY(%s)
    """, (ids,))
    s["distinct_anchor_datasets"] = cur.fetchone()["n_distinct_dsets"]

    # V-18 metric definition fix (Plan v5+ §"Cross_dataset metric 失真"):
    #   OLD: fields_used contains fields from >= 2 datasets → cross-dataset
    #        Misses the common case where alpha uses anchor + universal_pv
    #        supplement (close/returns from pv1 on an option8 anchor).
    #   NEW: fields_used contains ANY non-anchor dataset field → cross-dataset
    #        Captures the actual semantic of "alpha pulls signal from outside
    #        its declared anchor dataset" — which is what Phase 1 was
    #        designed to enable.
    cur.execute("""
        WITH alpha_anchor AS (
            SELECT a.id, a.region,
                   d_anchor.id AS anchor_ds_id
            FROM alphas a
            LEFT JOIN datasets d_anchor
              ON d_anchor.dataset_id = a.dataset_id
             AND d_anchor.region = a.region
            WHERE a.task_id = ANY(%s)
              AND a.fields_used IS NOT NULL
              AND jsonb_typeof(a.fields_used) = 'array'
              AND jsonb_array_length(a.fields_used) > 0
        ),
        alpha_field_dsets AS (
            SELECT
                a.id,
                a.anchor_ds_id,
                COUNT(DISTINCT df.dataset_id) FILTER (WHERE df.dataset_id IS NOT NULL) AS nd_total,
                COUNT(DISTINCT df.dataset_id) FILTER (
                    WHERE df.dataset_id IS NOT NULL AND df.dataset_id <> a.anchor_ds_id
                ) AS nd_non_anchor
            FROM alpha_anchor a
            LEFT JOIN alphas a2 ON a2.id = a.id
            LEFT JOIN LATERAL jsonb_array_elements_text(a2.fields_used) AS f(field_id) ON TRUE
            LEFT JOIN datafields df
              ON df.field_id = f.field_id AND df.region = a.region
            GROUP BY a.id, a.anchor_ds_id
        )
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE nd_total >= 2) AS cross_strict,
            COUNT(*) FILTER (WHERE nd_non_anchor >= 1) AS cross_anchor_aware
        FROM alpha_field_dsets
    """, (ids,))
    cd = cur.fetchone()
    s["cross_dataset_total"] = cd["total"]
    # New default: anchor-aware definition
    s["cross_dataset_alphas"] = cd["cross_anchor_aware"]
    # Keep strict for reference
    s["cross_dataset_strict"] = cd["cross_strict"]

    cur.execute("""
        SELECT COUNT(*) AS cnt FROM mining_tasks WHERE id = ANY(%s)
    """, (ids,))
    s["n_tasks"] = cur.fetchone()["cnt"]

    cur.close()
    return s


def fmt_pct(n: int, d: int) -> str:
    return f"{(n / d * 100):.2f}%" if d else "—"


def report(legacy: dict, phase1: dict) -> str:
    out = []
    out.append("# Phase 1 A/B Report")
    out.append("")
    out.append(f"Generated: {datetime.utcnow().isoformat()}Z")
    out.append("")
    out.append("## Variant comparison")
    out.append("")
    out.append("| Metric | Legacy (v=0) | Phase 1 (v=1) | Δ |")
    out.append("|---|---|---|---|")

    def row(label: str, vL: str, vP: str, delta: str = ""):
        out.append(f"| {label} | {vL} | {vP} | {delta} |")

    row("Tasks launched", str(legacy.get("n_tasks", 0)), str(phase1.get("n_tasks", 0)))
    pl, pp = legacy.get("pass_alphas", 0), phase1.get("pass_alphas", 0)
    fl, fp = legacy.get("fail_alphas", 0), phase1.get("fail_alphas", 0)
    row("PASS alphas", str(pl), str(pp))
    row("FAIL alphas", str(fl), str(fp))
    row(
        "PASS rate",
        fmt_pct(pl, pl + fl),
        fmt_pct(pp, pp + fp),
    )
    row(
        "OS overfit (sharpe≥5, test=0)",
        f"{legacy.get('suspected_overfit', 0)} / {pl}",
        f"{phase1.get('suspected_overfit', 0)} / {pp}",
    )
    row(
        "Cross-dataset alphas",
        f"{legacy.get('cross_dataset_alphas', 0)} / {legacy.get('cross_dataset_total', 0)}",
        f"{phase1.get('cross_dataset_alphas', 0)} / {phase1.get('cross_dataset_total', 0)}",
    )
    row(
        "Cross-dataset rate",
        fmt_pct(legacy.get("cross_dataset_alphas", 0), legacy.get("cross_dataset_total", 1)),
        fmt_pct(phase1.get("cross_dataset_alphas", 0), phase1.get("cross_dataset_total", 1)),
    )
    row(
        "Distinct anchor datasets",
        str(legacy.get("distinct_anchor_datasets", 0)),
        str(phase1.get("distinct_anchor_datasets", 0)),
    )
    row(
        "Train sharpe avg (PASS)",
        str(legacy.get("train_sharpe_avg", "—")),
        str(phase1.get("train_sharpe_avg", "—")),
    )
    row(
        "Test sharpe avg (PASS)",
        str(legacy.get("test_sharpe_avg", "—")),
        str(phase1.get("test_sharpe_avg", "—")),
    )
    row(
        "OS retention (test/train)",
        (f"{(legacy.get('test_sharpe_avg', 0) or 0) / (legacy.get('train_sharpe_avg', 1) or 1):.2f}"
         if legacy.get("train_sharpe_avg") else "—"),
        (f"{(phase1.get('test_sharpe_avg', 0) or 0) / (phase1.get('train_sharpe_avg', 1) or 1):.2f}"
         if phase1.get("train_sharpe_avg") else "—"),
    )

    out.append("")
    out.append("## Interpretation guide")
    out.append("")
    out.append("- **Cross-dataset rate**: Phase 1 should produce noticeably more "
               "cross-dataset alphas (LLM picks fundamental+pv combinations).")
    out.append("- **Distinct anchor datasets**: V-13 RANDOM secondary sort already "
               "spreads anchor selection; Phase 1 should preserve or improve.")
    out.append("- **OS retention**: V-12 + V-12.1 should keep test/train ratio "
               "≥ 0.4 in both variants. If Phase 1 ratio drops, cross-dataset "
               "introduces overfit risk that needs deeper investigation.")
    out.append("- **PASS rate**: marginal change expected on small N — focus "
               "on cross-dataset rate for Phase 1 verdict.")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--legacy-ids", required=True)
    ap.add_argument("--phase1-ids", required=True)
    ap.add_argument("--output", default=None,
                    help="markdown path; default docs/phase1_ab_report_<date>.md")
    args = ap.parse_args()

    legacy_ids = parse_ids(args.legacy_ids)
    phase1_ids = parse_ids(args.phase1_ids)

    conn = psycopg2.connect(
        host="localhost", port=5433,
        user="postgres", password="postgres", dbname="alpha_gpt",
    )
    legacy = agg_for(conn, legacy_ids)
    phase1 = agg_for(conn, phase1_ids)
    conn.close()

    md = report(legacy, phase1)
    print(md)

    out = args.output or f"docs/phase1_ab_report_{datetime.utcnow().strftime('%Y-%m-%d')}.md"
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(md, encoding="utf-8")
    print(f"\nReport written to: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
