"""V-16 audit — flag existing PASS alphas with sharpe>3.0 against the
6-risk suspicion checklist.

Plan v5+ §V-16 implementation is forward-looking (gates new alphas at
evaluation time). For historical PASS rows accumulated before V-16
landed, this script scans them and:

  - Annotates metrics["_v16_suspicion_flags"] with the flag list
  - When hard flags are present, demotes PASS → PASS_PROVISIONAL
    so they exit the submission queue and re-enter optimization

Idempotent: re-runs skip alphas already annotated unless --force.

Usage:
    python scripts/v16_audit_existing.py --dry-run
    python scripts/v16_audit_existing.py
    python scripts/v16_audit_existing.py --task-min 42 --task-max 47
    python scripts/v16_audit_existing.py --force                 # re-scan all
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow importing backend.* when run as a script outside the project root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg2
import psycopg2.extras

from backend.agents.graph.nodes.evaluation import (
    V16_SUSPICION_THRESHOLD,
    _run_suspicion_checks,
)


def find_audit_targets(conn, task_min, task_max, force):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    where = ["quality_status IN ('PASS', 'PASS_PROVISIONAL')",
             "(metrics->>'sharpe')::float > %s"]
    params = [V16_SUSPICION_THRESHOLD]
    if task_min is not None:
        where.append("task_id >= %s")
        params.append(task_min)
    if task_max is not None:
        where.append("task_id <= %s")
        params.append(task_max)
    if not force:
        where.append("NOT (metrics ? '_v16_suspicion_flags')")
    sql = f"""
        SELECT id, alpha_id, task_id, factor_tier, quality_status,
               expression, metrics
        FROM alphas
        WHERE {' AND '.join(where)}
        ORDER BY id
    """
    cur.execute(sql, params)
    rows = cur.fetchall()
    cur.close()
    return rows


def apply_audit(conn, alpha_id_int: int, prev_status: str,
                metrics: dict, flags: list, downgrade: bool) -> None:
    cur = conn.cursor()
    new_metrics = dict(metrics or {})
    new_metrics["_v16_suspicion_flags"] = flags
    new_status = "PASS_PROVISIONAL" if downgrade else prev_status

    try:
        cur.execute(
            """
            UPDATE alphas SET metrics = %s::jsonb,
                              quality_status = %s,
                              updated_at = NOW()
            WHERE id = %s
            """,
            (json.dumps(new_metrics), new_status, alpha_id_int),
        )
        if downgrade and prev_status != new_status:
            reason = "v16_suspicion: " + ",".join(
                f["check"] for f in flags if f.get("severity") == "hard"
            )
            cur.execute(
                """
                INSERT INTO alpha_status_transitions
                    (alpha_id, old_status, new_status, reason, source, transitioned_at)
                VALUES (%s, %s, %s, %s, 'backfill_v16', NOW())
                """,
                (alpha_id_int, prev_status, new_status, reason[:200]),
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
    ap.add_argument("--force", action="store_true",
                    help="re-scan even already-annotated rows")
    args = ap.parse_args()

    conn = psycopg2.connect(
        host="localhost", port=5433,
        user="postgres", password="postgres", dbname="alpha_gpt",
    )
    rows = find_audit_targets(conn, args.task_min, args.task_max, args.force)

    if not rows:
        print("No alphas above V-16 threshold to audit.")
        conn.close()
        return 0

    annotate_count = 0
    downgrade_count = 0
    skip_count = 0
    plan = []
    for r in rows:
        flags = _run_suspicion_checks(r["metrics"] or {}, r["expression"] or "")
        if not flags:
            skip_count += 1
            continue
        annotate_count += 1
        hard = [f for f in flags if f.get("severity") == "hard"]
        will_downgrade = bool(hard) and r["quality_status"] == "PASS"
        if will_downgrade:
            downgrade_count += 1
        plan.append((r, flags, will_downgrade))

    print("=" * 70)
    print(f"V-16 audit — {len(rows)} alphas above sharpe>{V16_SUSPICION_THRESHOLD}")
    print("=" * 70)
    print(f"  Will annotate flags:  {annotate_count}")
    print(f"  Will downgrade PASS:  {downgrade_count}")
    print(f"  No flags / skip:      {skip_count}")
    print()
    print("First 10 affected:")
    for r, flags, dg in plan[:10]:
        aid = r["alpha_id"] or "(none)"
        tier = r["factor_tier"] if r["factor_tier"] is not None else "?"
        names = [f["check"] for f in flags]
        sev = ",".join(sorted({f["severity"] for f in flags}))
        marker = "→OPT" if dg else "annotate"
        print(f"  id={r['id']:>5} alpha_id={aid:<10} T{tier} {marker:<8}"
              f" sharpe={(r['metrics'] or {}).get('sharpe', '?'):>6} "
              f"flags={names} sev=[{sev}]")
    if len(plan) > 10:
        print(f"  ... and {len(plan) - 10} more")
    print()

    if args.dry_run:
        print("[dry-run] No changes.")
        conn.close()
        return 0

    print(f"Applying...")
    success = 0
    failed = 0
    for r, flags, dg in plan:
        try:
            apply_audit(conn, r["id"], r["quality_status"], r["metrics"], flags, dg)
            success += 1
        except Exception as e:
            print(f"  FAILED id={r['id']}: {e}")
            failed += 1
    conn.close()
    print(f"Done. {success} annotated ({downgrade_count} downgraded) / {failed} failed.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
