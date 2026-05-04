"""V-17 backfill — populate alpha.fields_used from expression for mining
output rows.

Plan v5+ §V-17 (2026-05-04 spike-discovered):
Mining pipeline (workflow.py / persistence.py) never set Alpha.fields_used
when writing rows, so every alpha produced via mining has
fields_used=[] (default). Cross-dataset analytics silently relied on
BRAIN-synced rows (task_id=1, ~3881 alphas), masking the fact that
spike output was 0% cross-dataset by construction — even after Phase 1
A2-A5 + C-architecture + D1 prompt changes.

This script extracts fields from expression via
AlphaSemanticValidator._extract_fields and writes them back. Idempotent:
re-runs skip rows with non-empty fields_used unless --force.

Usage:
    python scripts/v17_backfill_fields_used.py --dry-run
    python scripts/v17_backfill_fields_used.py
    python scripts/v17_backfill_fields_used.py --task-min 22 --task-max 71
    python scripts/v17_backfill_fields_used.py --force          # re-extract all
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg2
import psycopg2.extras

from backend.alpha_semantic_validator import AlphaSemanticValidator


def extract_fields(expression: str) -> list[str]:
    if not expression:
        return []
    try:
        v = AlphaSemanticValidator(
            fields=[], operators=None,
            strict_field_check=False, strict_type_check=False,
        )
        return list(v.validate(expression).used_fields)
    except Exception:
        return []


def find_targets(conn, task_min, task_max, force):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    where = ["expression IS NOT NULL", "expression <> ''"]
    params = []
    if task_min is not None:
        where.append("task_id >= %s"); params.append(task_min)
    if task_max is not None:
        where.append("task_id <= %s"); params.append(task_max)
    if not force:
        where.append("(fields_used IS NULL OR jsonb_array_length(fields_used) = 0)")
    sql = f"""
        SELECT id, task_id, alpha_id, expression
        FROM alphas
        WHERE {' AND '.join(where)}
        ORDER BY id
    """
    cur.execute(sql, params)
    rows = cur.fetchall()
    cur.close()
    return rows


def update_one(conn, alpha_id_int: int, fields: list) -> None:
    cur = conn.cursor()
    try:
        cur.execute(
            """
            UPDATE alphas SET fields_used = %s::jsonb, updated_at = NOW()
            WHERE id = %s
            """,
            (json.dumps(fields), alpha_id_int),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--task-min", type=int, default=None)
    ap.add_argument("--task-max", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    conn = psycopg2.connect(
        host="localhost", port=5433,
        user="postgres", password="postgres", dbname="alpha_gpt",
    )
    rows = find_targets(conn, args.task_min, args.task_max, args.force)
    if not rows:
        print("No alphas to backfill.")
        conn.close()
        return 0

    print("=" * 70)
    print(f"V-17 fields_used backfill — {len(rows)} alphas")
    print("=" * 70)
    plan = []
    extracted_zero = 0
    for r in rows:
        fields = extract_fields(r["expression"])
        if not fields:
            extracted_zero += 1
            continue
        plan.append((r, fields))

    print(f"  Extractable:        {len(plan)}")
    print(f"  Zero fields (skip): {extracted_zero}")
    print()
    print("First 8 affected:")
    for r, f in plan[:8]:
        aid = r["alpha_id"] or "(none)"
        task = r["task_id"] if r["task_id"] is not None else "?"
        print(f"  id={r['id']:>5} alpha_id={aid:<10} task={task}  fields={f[:5]}{'...' if len(f) > 5 else ''}")
    if len(plan) > 8:
        print(f"  ... and {len(plan) - 8} more")
    print()

    if args.dry_run:
        print("[dry-run] No changes.")
        conn.close()
        return 0

    print("Updating...")
    success = 0
    failed = 0
    for r, f in plan:
        try:
            update_one(conn, r["id"], f)
            success += 1
        except Exception as e:
            print(f"  FAILED id={r['id']}: {e}")
            failed += 1
    conn.close()
    print(f"Done. {success} updated / {failed} failed.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
