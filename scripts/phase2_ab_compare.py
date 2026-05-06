"""Phase 2 A/B comparison — variant 1 (Phase 1 baseline) vs variant 2
(Phase 2 typed Hypothesis lifecycle).

Reads task IDs split by variant, aggregates Phase 1 inherited metrics
(PASS rate / cross-dataset / OS retention) PLUS Phase 2-specific metrics
(hypothesis lifecycle distribution / KB hypothesis-keyed entries / alpha
hypothesis_id coverage).

Usage:
    python scripts/phase2_ab_compare.py \\
        --phase1-ids 150,152,154,156 \\
        --phase2-ids 151,153,155,157
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg2
import psycopg2.extras


def parse_ids(s: str) -> List[int]:
    return [int(x.strip()) for x in s.split(",") if x.strip()]


def agg_for(conn, ids: List[int]) -> dict:
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    if not ids:
        return {"n_tasks": 0}

    # Phase 1 inherited metrics
    cur.execute("""
        SELECT
            COUNT(DISTINCT task_id) AS n_tasks_with_alpha,
            COUNT(*) AS pass_alphas,
            COUNT(*) FILTER (WHERE quality_status = 'PASS') AS pass_strict,
            COUNT(*) FILTER (WHERE quality_status = 'PASS_PROVISIONAL') AS pass_prov,
            ROUND(AVG((metrics->>'sharpe')::float)::numeric, 2) AS train_sharpe_avg,
            ROUND(AVG((metrics->>'test_sharpe')::float)::numeric, 2) AS test_sharpe_avg,
            COUNT(*) FILTER (WHERE can_submit = true) AS can_submit_n,
            COUNT(*) FILTER (WHERE can_submit IS NOT NULL) AS can_submit_checked
        FROM alphas WHERE task_id = ANY(%s)
    """, (ids,))
    s = dict(cur.fetchone() or {})

    cur.execute("""
        SELECT COUNT(*) FROM alpha_failures WHERE task_id = ANY(%s)
    """, (ids,))
    s["fail_alphas"] = cur.fetchone()["count"]

    # Cross-dataset rate (anchor-aware, V-18 metric)
    cur.execute("""
        WITH alpha_anchor AS (
            SELECT a.id, a.region, d_anchor.id AS anchor_ds_id
            FROM alphas a
            LEFT JOIN datasets d_anchor
              ON d_anchor.dataset_id = a.dataset_id AND d_anchor.region = a.region
            WHERE a.task_id = ANY(%s)
              AND a.fields_used IS NOT NULL
              AND jsonb_typeof(a.fields_used) = 'array'
              AND jsonb_array_length(a.fields_used) > 0
        ),
        alpha_field_dsets AS (
            SELECT a.id, a.anchor_ds_id,
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
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE nd_non_anchor >= 1) AS cross_anchor_aware
        FROM alpha_field_dsets
    """, (ids,))
    cd = cur.fetchone()
    s["cross_dataset_total"] = cd["total"]
    s["cross_dataset_alphas"] = cd["cross_anchor_aware"]

    # Phase 2-specific: alpha.hypothesis_id coverage
    cur.execute("""
        SELECT
            COUNT(*) FILTER (WHERE hypothesis_id IS NOT NULL) AS with_hid,
            COUNT(*) AS total
        FROM alphas WHERE task_id = ANY(%s)
    """, (ids,))
    h = cur.fetchone()
    s["alpha_with_hypothesis_id"] = h["with_hid"]
    s["alpha_total"] = h["total"]

    # Phase 2-specific: hypothesis lifecycle distribution
    cur.execute("""
        SELECT h.status, COUNT(*) AS n
        FROM hypotheses h
        WHERE EXISTS (
            SELECT 1 FROM alphas a WHERE a.hypothesis_id = h.id AND a.task_id = ANY(%s)
        )
        GROUP BY h.status
    """, (ids,))
    s["hypothesis_status_dist"] = {row["status"]: row["n"] for row in cur.fetchall()}

    # Phase 2-specific: distinct hypotheses linked
    cur.execute("""
        SELECT COUNT(DISTINCT hypothesis_id) AS n
        FROM alphas WHERE task_id = ANY(%s) AND hypothesis_id IS NOT NULL
    """, (ids,))
    s["distinct_hypotheses_linked"] = cur.fetchone()["n"]

    # Task count
    cur.execute("SELECT COUNT(*) AS cnt FROM mining_tasks WHERE id = ANY(%s)", (ids,))
    s["n_tasks"] = cur.fetchone()["cnt"]

    cur.close()
    return s


def kb_hypothesis_tagged(conn, ids: List[int], variant_str: str) -> dict:
    """Phase 2 KB metric: how many KB entries created during this batch
    have hypothesis_id tagged. Filtered to entries created during the
    batch's time window (when first task started → 6h after).
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    if not ids:
        cur.close()
        return {"success_total": 0, "success_with_hid": 0,
                "failure_total": 0, "failure_with_hid": 0}

    # Time window = task creation
    cur.execute("""
        SELECT MIN(created_at) AS min_t, MAX(created_at) + INTERVAL '6 hours' AS max_t
        FROM mining_tasks WHERE id = ANY(%s)
    """, (ids,))
    times = cur.fetchone() or {}
    if not times.get("min_t"):
        cur.close()
        return {"success_total": 0, "success_with_hid": 0,
                "failure_total": 0, "failure_with_hid": 0}

    # T03 fix (2026-05-06): scope `with_hid` to entries from THIS variant
    # only. Pre-fix used a time-window query that overlapped between v=1
    # and v=2 (interleaved A/B batch) so both variants saw identical
    # with_hid counts — misleading because Phase 1 (v=1) shouldn't tag any
    # KB entry with hypothesis_id (LEVEL=1 doesn't populate it).
    cur.execute("""
        SELECT entry_type, COUNT(*) AS n,
               COUNT(*) FILTER (
                   WHERE meta_data->>'hypothesis_id' IS NOT NULL
                     AND meta_data->>'experiment_variant' = %s
               ) AS with_hid,
               COUNT(*) FILTER (WHERE meta_data->>'experiment_variant' = %s) AS with_variant
        FROM knowledge_entries
        WHERE created_at >= %s AND created_at <= %s
          AND entry_type IN ('SUCCESS_PATTERN', 'FAILURE_PITFALL')
        GROUP BY entry_type
    """, (variant_str, variant_str, times["min_t"], times["max_t"]))
    out = {"success_total": 0, "success_with_hid": 0, "success_with_variant": 0,
           "failure_total": 0, "failure_with_hid": 0, "failure_with_variant": 0}
    for row in cur.fetchall():
        if row["entry_type"] == "SUCCESS_PATTERN":
            out["success_total"] = row["n"]
            out["success_with_hid"] = row["with_hid"]
            out["success_with_variant"] = row["with_variant"]
        elif row["entry_type"] == "FAILURE_PITFALL":
            out["failure_total"] = row["n"]
            out["failure_with_hid"] = row["with_hid"]
            out["failure_with_variant"] = row["with_variant"]
    cur.close()
    return out


def fmt_pct(n: int, d: int) -> str:
    return f"{(n / d * 100):.2f}%" if d else "—"


def report(p1: dict, p2: dict, kb_p1: dict, kb_p2: dict) -> str:
    out = []
    out.append("# Phase 2 A/B Report")
    out.append("")
    out.append(f"Generated: {datetime.utcnow().isoformat()}Z")
    out.append("")
    out.append("Comparing **HYPOTHESIS_CENTRIC_LEVEL=1** (Phase 1 baseline) "
               "vs **LEVEL=2** (Phase 2 typed Hypothesis lifecycle).")
    out.append("")
    out.append("## Task & alpha-level metrics")
    out.append("")
    out.append("| Metric | Phase 1 (v=1) | Phase 2 (v=2) |")
    out.append("|---|---|---|")

    def row(label, vL, vP):
        out.append(f"| {label} | {vL} | {vP} |")

    row("Tasks", str(p1.get("n_tasks", 0)), str(p2.get("n_tasks", 0)))
    p1_pass = p1.get("pass_alphas", 0)
    p2_pass = p2.get("pass_alphas", 0)
    p1_fail = p1.get("fail_alphas", 0)
    p2_fail = p2.get("fail_alphas", 0)
    row("PASS alphas", str(p1_pass), str(p2_pass))
    row("FAIL alphas", str(p1_fail), str(p2_fail))
    row("PASS rate (alpha-level)",
        fmt_pct(p1_pass, p1_pass + p1_fail),
        fmt_pct(p2_pass, p2_pass + p2_fail))
    row("can_submit rate",
        fmt_pct(p1.get("can_submit_n", 0), p1.get("can_submit_checked", 1) or 1),
        fmt_pct(p2.get("can_submit_n", 0), p2.get("can_submit_checked", 1) or 1))
    row("Cross-dataset rate (anchor-aware)",
        fmt_pct(p1.get("cross_dataset_alphas", 0), p1.get("cross_dataset_total", 1) or 1),
        fmt_pct(p2.get("cross_dataset_alphas", 0), p2.get("cross_dataset_total", 1) or 1))
    row("Train sharpe avg (PASS)",
        str(p1.get("train_sharpe_avg", "—")),
        str(p2.get("train_sharpe_avg", "—")))
    row("Test sharpe avg (PASS)",
        str(p1.get("test_sharpe_avg", "—")),
        str(p2.get("test_sharpe_avg", "—")))
    p1_train = p1.get("train_sharpe_avg") or 0
    p2_train = p2.get("train_sharpe_avg") or 0
    row("OS retention (test/train)",
        f"{(p1.get('test_sharpe_avg') or 0) / p1_train:.2f}" if p1_train else "—",
        f"{(p2.get('test_sharpe_avg') or 0) / p2_train:.2f}" if p2_train else "—")

    out.append("")
    out.append("## Phase 2-specific metrics")
    out.append("")
    out.append("| Metric | Phase 1 (v=1) | Phase 2 (v=2) |")
    out.append("|---|---|---|")

    row("alpha.hypothesis_id coverage",
        f"{p1.get('alpha_with_hypothesis_id', 0)} / {p1.get('alpha_total', 0)}",
        f"{p2.get('alpha_with_hypothesis_id', 0)} / {p2.get('alpha_total', 0)}")
    row("Distinct hypotheses linked",
        str(p1.get("distinct_hypotheses_linked", 0)),
        str(p2.get("distinct_hypotheses_linked", 0)))

    p1_dist = p1.get("hypothesis_status_dist", {})
    p2_dist = p2.get("hypothesis_status_dist", {})
    for status in ("PROPOSED", "ACTIVE", "PROMOTED", "ABANDONED", "SUPERSEDED"):
        row(f"  {status}",
            str(p1_dist.get(status, 0)),
            str(p2_dist.get(status, 0)))

    out.append("")
    out.append("## KB hypothesis-keyed entries")
    out.append("")
    out.append("Phase 2 B8 should produce KB entries with `meta_data.hypothesis_id` set. "
               "Phase 1 (v=1) should produce 0 since hypothesis_id is only populated at LEVEL≥2.")
    out.append("")
    out.append("| Metric | Phase 1 (v=1) | Phase 2 (v=2) |")
    out.append("|---|---|---|")
    row("SUCCESS_PATTERN total in window", str(kb_p1["success_total"]), str(kb_p2["success_total"]))
    row("SUCCESS_PATTERN with hypothesis_id", str(kb_p1["success_with_hid"]), str(kb_p2["success_with_hid"]))
    row("SUCCESS_PATTERN with variant tag", str(kb_p1.get("success_with_variant", 0)), str(kb_p2.get("success_with_variant", 0)))
    row("FAILURE_PITFALL total in window", str(kb_p1["failure_total"]), str(kb_p2["failure_total"]))
    row("FAILURE_PITFALL with hypothesis_id", str(kb_p1["failure_with_hid"]), str(kb_p2["failure_with_hid"]))
    row("FAILURE_PITFALL with variant tag", str(kb_p1.get("failure_with_variant", 0)), str(kb_p2.get("failure_with_variant", 0)))

    out.append("")
    out.append("## Interpretation")
    out.append("")
    out.append("- **PASS rate / can_submit / cross-dataset**: Phase 2 should NOT regress")
    out.append("  vs Phase 1. Plan v5+ §V-1 灰度 criterion: within 30% margin or improved.")
    out.append("- **alpha.hypothesis_id coverage**: Phase 2 expected ≈ 100% of PASS+PROV;")
    out.append("  Phase 1 expected 0% (hypothesis_id only populated at LEVEL≥2).")
    out.append("- **Hypothesis lifecycle**: Phase 2 should show non-zero PROMOTED + possibly")
    out.append("  ABANDONED (if any 3-round failure streak triggered B6); Phase 1 expected 0.")
    out.append("- **KB hypothesis-keyed**: Phase 2 SUCCESS_PATTERN/FAILURE_PITFALL should")
    out.append("  carry `meta_data.hypothesis_id`; Phase 1 expected 0.")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--phase1-ids", required=True)
    ap.add_argument("--phase2-ids", required=True)
    ap.add_argument("--output", default=None)
    args = ap.parse_args()

    p1_ids = parse_ids(args.phase1_ids)
    p2_ids = parse_ids(args.phase2_ids)

    conn = psycopg2.connect(
        host="localhost", port=5433,
        user="postgres", password="postgres", dbname="alpha_gpt",
    )
    p1 = agg_for(conn, p1_ids)
    p2 = agg_for(conn, p2_ids)
    kb_p1 = kb_hypothesis_tagged(conn, p1_ids, "1")
    kb_p2 = kb_hypothesis_tagged(conn, p2_ids, "2")
    conn.close()

    md = report(p1, p2, kb_p1, kb_p2)
    print(md)

    out = args.output or f"docs/phase2_ab_report_{datetime.utcnow().strftime('%Y-%m-%d_%H%M')}.md"
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(md, encoding="utf-8")
    print(f"\nReport written to: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
