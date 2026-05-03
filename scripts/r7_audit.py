"""R7 audit script (Plan v5+ Pre-coding) — operator + datafield snapshots.

R7-0: BRAIN operators registered in this project's operators table — name,
      category, definition, description.
R7-1: Datafields by region × dataset — coverage, type, count.

Outputs:
  docs/operators_snapshot_v1.md
  docs/datafields_snapshot_v1.md

These power Layer 1 hypothesis pool construction (Phase 1) and the
plan_op_audit cross-check ('does each plan-mentioned operator exist with
the expected signature?').
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path

import psycopg2
import psycopg2.extras


DOCS = Path("docs")


def _conn():
    return psycopg2.connect(
        host="localhost", port=5433,
        user="postgres", password="postgres", dbname="alpha_gpt",
    )


def _operators_snapshot() -> str:
    conn = _conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT name, category, definition, description, is_active
        FROM operators
        WHERE is_active = true
        ORDER BY category, name
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()

    out = []
    out.append("# Operators Snapshot v1 (R7-0)")
    out.append("")
    out.append(f"Generated: {datetime.utcnow().isoformat()}Z")
    out.append(f"Total active operators: {len(rows)}")
    out.append("")
    out.append("## By category")
    out.append("")
    by_cat = defaultdict(list)
    for r in rows:
        by_cat[r["category"]].append(r)

    for cat in sorted(by_cat.keys()):
        ops = by_cat[cat]
        out.append(f"### {cat} ({len(ops)})")
        out.append("")
        out.append("| Name | Definition | Description |")
        out.append("|---|---|---|")
        for r in ops:
            name = r["name"]
            defn = (r["definition"] or "").replace("\n", " ").replace("|", "\\|")
            desc = (r["description"] or "").replace("\n", " ").replace("|", "\\|")
            if len(desc) > 120:
                desc = desc[:120] + "..."
            out.append(f"| `{name}` | `{defn}` | {desc} |")
        out.append("")

    # Plan-mentioned operators cross-check (Plan v5+ §R7-0 list)
    plan_ops = {
        # Arithmetic
        "add", "subtract", "multiply", "divide", "signed_power",
        "min", "abs",
        # Cross-sectional
        "rank", "zscore", "normalize", "quantile", "winsorize", "scale",
        # Time series
        "ts_rank", "ts_zscore", "ts_mean", "ts_std_dev", "ts_delay",
        "ts_delta", "ts_sum", "ts_corr", "ts_decay_linear",
        "ts_arg_max", "ts_arg_min", "ts_av_diff", "ts_count_nans",
        "ts_product", "ts_scale", "ts_step", "ts_regression",
        "ts_covariance", "ts_backfill", "ts_quantile",
        "ts_max", "ts_min",
        # Group
        "group_neutralize", "group_rank", "group_zscore",
        "group_mean", "group_scale",
        "group_demean", "group_normalize",
        # Event
        "trade_when",
        # Logical/comparison
        "less", "greater", "if_else", "equal",
    }
    actual_names = {r["name"] for r in rows}
    missing = sorted(plan_ops - actual_names)
    extra = sorted(actual_names - plan_ops)

    out.append("## Plan v5+ §R7-0 cross-check")
    out.append("")
    out.append(f"- Plan-mentioned operators: {len(plan_ops)}")
    out.append(f"- Present in DB: {len(plan_ops & actual_names)}")
    out.append(f"- Missing in DB: {len(missing)}")
    out.append(f"- Available in DB beyond plan list: {len(extra)}")
    out.append("")
    if missing:
        out.append("### ❌ Missing operators (plan references these but not in DB)")
        out.append("")
        for op in missing:
            out.append(f"- `{op}`")
        out.append("")
    if extra:
        out.append("### Available operators not in plan list")
        out.append("")
        for op in extra:
            out.append(f"- `{op}`")
        out.append("")
    return "\n".join(out)


def _datafields_snapshot() -> str:
    conn = _conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT
            d.region, d.universe, d.dataset_id, d.category, d.subcategory,
            COUNT(f.id) AS field_count,
            COUNT(*) FILTER (WHERE f.field_type = 'MATRIX') AS matrix_count,
            COUNT(*) FILTER (WHERE f.field_type = 'VECTOR') AS vector_count
        FROM datasets d
        LEFT JOIN datafields f ON f.dataset_id = d.id
                              AND f.region = d.region
                              AND f.universe = d.universe
        WHERE d.is_active = true
        GROUP BY d.region, d.universe, d.dataset_id, d.category, d.subcategory
        ORDER BY d.region, d.universe, d.dataset_id
    """)
    ds_rows = cur.fetchall()

    cur.execute("""
        SELECT region, COUNT(DISTINCT dataset_id) AS n_datasets,
               COUNT(*) AS n_fields,
               COUNT(*) FILTER (WHERE field_type='MATRIX') AS n_matrix,
               COUNT(*) FILTER (WHERE field_type='VECTOR') AS n_vector
        FROM datafields
        GROUP BY region ORDER BY region
    """)
    region_rows = cur.fetchall()

    cur.close()
    conn.close()

    out = []
    out.append("# Datafields Snapshot v1 (R7-1)")
    out.append("")
    out.append(f"Generated: {datetime.utcnow().isoformat()}Z")
    out.append("")
    out.append("## Region × universe summary")
    out.append("")
    out.append("| Region | N datasets | N fields | MATRIX | VECTOR |")
    out.append("|---|---|---|---|---|")
    for r in region_rows:
        out.append(
            f"| {r['region']} | {r['n_datasets']} | {r['n_fields']} | "
            f"{r['n_matrix']} | {r['n_vector']} |"
        )
    out.append("")

    out.append("## Datasets by region/universe")
    out.append("")
    out.append("| Region | Universe | Dataset | Category | Subcategory | Fields | MATRIX | VECTOR |")
    out.append("|---|---|---|---|---|---|---|---|")
    for r in ds_rows:
        out.append(
            f"| {r['region']} | {r['universe']} | `{r['dataset_id']}` | "
            f"{r['category'] or ''} | {r['subcategory'] or ''} | "
            f"{r['field_count']} | {r['matrix_count']} | {r['vector_count']} |"
        )
    out.append("")

    # Plan v5+ key fields cross-check (R7-1)
    plan_field_aliases = {
        "close", "open", "high", "low", "volume", "vwap", "returns",
        "cap", "amount", "open_interest",
        # Quasi-T1 fields
        "eps", "book_value_per_share", "ebit", "ev",
        "cfo", "net_income", "sales", "total_assets",
        "total_debt", "total_equity",
    }
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT field_id FROM datafields
        WHERE region = 'USA' AND universe = 'TOP3000'
          AND field_id = ANY(%s)
    """, (sorted(plan_field_aliases),))
    found_in_usa = {row[0] for row in cur.fetchall()}
    cur.close()
    conn.close()

    missing_in_usa = sorted(plan_field_aliases - found_in_usa)

    out.append("## Plan v5+ §R7-1 USA cross-check (Quasi-T1 fields + universal PV)")
    out.append("")
    out.append(f"- Plan-mentioned aliases: {len(plan_field_aliases)}")
    out.append(f"- Present in USA/TOP3000: {len(found_in_usa)}")
    out.append(f"- Missing (need synthesis or different real name): {len(missing_in_usa)}")
    out.append("")
    if missing_in_usa:
        out.append("### ❌ Missing aliases (plan uses these names but USA/TOP3000 doesn't have them)")
        out.append("")
        for f in missing_in_usa:
            out.append(f"- `{f}`")
        out.append("")
        out.append(
            "These need either (a) BRAIN real-name mapping in field_adapter "
            "(e.g. `eps` → `fnd6_newa2v1300_eps_per_share`) or (b) synthesis "
            "via available fields (e.g. `eps` → `divide(fnd6_..._ni, shares)`)."
        )

    return "\n".join(out)


def main():
    DOCS.mkdir(exist_ok=True)
    op_md = _operators_snapshot()
    df_md = _datafields_snapshot()
    (DOCS / "operators_snapshot_v1.md").write_text(op_md, encoding="utf-8")
    (DOCS / "datafields_snapshot_v1.md").write_text(df_md, encoding="utf-8")
    print(f"Wrote {DOCS / 'operators_snapshot_v1.md'} ({len(op_md)} chars)")
    print(f"Wrote {DOCS / 'datafields_snapshot_v1.md'} ({len(df_md)} chars)")


if __name__ == "__main__":
    main()
