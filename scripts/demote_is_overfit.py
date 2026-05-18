"""V-12 backfill — demote PASS alphas with IS-only overfit signature.

Spike (2026-05-02 → 03) revealed alphas with train_sharpe up to 16.2 paired
with test_sharpe=0 — pure IS overfit. New evaluation.py:hard_gate_pass adds
_check_is_os_consistency to reject these going forward, but historical PASS
rows need a one-time backfill.

Rules (mirror evaluation.py:_check_is_os_consistency):
  is_sharpe < 2:                no OS check (passes)
  2 <= is_sharpe < 5:           require os_sharpe > 0 AND os/is >= 0.3
  is_sharpe >= 5:               require os_sharpe > 0 AND os/is >= 0.4

Demotes PASS / PASS_PROVISIONAL → OPTIMIZE so they re-enter
mining_agent's optimization candidate pool (mining_agent.py:622).

Usage:
    python scripts/demote_is_overfit.py --dry-run     # preview
    python scripts/demote_is_overfit.py               # execute
    python scripts/demote_is_overfit.py --task-min 22 --task-max 41   # spike only
"""
from __future__ import annotations

import argparse
import sys

import psycopg2
import psycopg2.extras


def is_overfit(is_sh: float, os_sh: float) -> tuple[bool, str]:
    """Return (is_overfit, reason)."""
    is_sh = is_sh or 0
    os_sh = os_sh or 0
    if is_sh < 2:
        return False, ""
    if os_sh <= 0:
        return True, f"is_sharpe={is_sh:.2f} but no positive OS evidence (os={os_sh})"
    ratio = os_sh / is_sh
    threshold = 0.4 if is_sh >= 5 else 0.3
    if ratio < threshold:
        return True, f"is_sharpe={is_sh:.2f} os_sharpe={os_sh:.2f} ratio={ratio:.2f} < {threshold}"
    return False, ""


def find_affected_alphas(conn, task_min=None, task_max=None) -> list:
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    where_extra = ""
    params = []
    if task_min is not None:
        where_extra += " AND a.task_id >= %s"
        params.append(task_min)
    if task_max is not None:
        where_extra += " AND a.task_id <= %s"
        params.append(task_max)
    cur.execute(f"""
        SELECT
            a.id,
            a.alpha_id,
            a.task_id,
            a.quality_status,
            (a.metrics->>'sharpe')::float AS is_sh,
            COALESCE(
                (a.metrics->>'os_sharpe')::float,
                (a.metrics->>'test_sharpe')::float,
                0
            ) AS os_sh
        FROM alphas a
        WHERE a.quality_status IN ('PASS', 'PASS_PROVISIONAL')
          {where_extra}
        ORDER BY a.id
    """, params)
    rows = cur.fetchall()
    cur.close()

    affected = []
    for r in rows:
        overfit, reason = is_overfit(r["is_sh"], r["os_sh"])
        if overfit:
            r["overfit_reason"] = reason
            affected.append(r)
    return affected


def demote_alpha(conn, alpha_id: int, prev_status: str, reason: str) -> None:
    cur = conn.cursor()
    reason_short = reason[:200]
    try:
        cur.execute(
            """
            UPDATE alphas
            SET quality_status = 'OPTIMIZE',
                updated_at = NOW()
            WHERE id = %s AND quality_status = %s
            """,
            (alpha_id, prev_status),
        )
        affected = cur.rowcount
        cur.execute(
            """
            INSERT INTO alpha_status_transitions
                (alpha_id, old_status, new_status, reason, source, transitioned_at)
            VALUES (%s, %s, 'OPTIMIZE', %s, 'backfill_v12_overfit', NOW())
            """,
            (alpha_id, prev_status, reason_short),
        )
        if affected != 1:
            raise RuntimeError(f"alpha id={alpha_id} update affected {affected} rows")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--task-min", type=int, default=None)
    parser.add_argument("--task-max", type=int, default=None)
    args = parser.parse_args()

    conn = psycopg2.connect(
        host="localhost", port=5433,
        user="postgres", password="postgres", dbname="alpha_gpt",
    )

    rows = find_affected_alphas(conn, args.task_min, args.task_max)
    if not rows:
        print("No IS-overfit alphas found.")
        conn.close()
        return 0

    print("=" * 70)
    print(f"V-12 IS-overfit backfill — {len(rows)} alphas to demote")
    print("=" * 70)
    by_status = {}
    for r in rows:
        s = r["quality_status"]
        by_status[s] = by_status.get(s, 0) + 1
    print("By status:")
    for s, n in sorted(by_status.items()):
        print(f"  {s:<20} → OPTIMIZE: {n}")
    print()

    print("First 10 affected:")
    for r in rows[:10]:
        aid = r["alpha_id"] or "(none)"
        task = r["task_id"] if r["task_id"] is not None else "?"
        print(f"  id={r['id']:>5} alpha_id={aid:<10} task={task}  "
              f"status={r['quality_status']:<18}  {r['overfit_reason']}")
    if len(rows) > 10:
        print(f"  ... and {len(rows) - 10} more")
    print()

    if args.dry_run:
        print("[dry-run] No changes made.")
        conn.close()
        return 0

    print(f"Demoting {len(rows)} alphas to OPTIMIZE...")
    success = 0
    failed = 0
    for r in rows:
        try:
            demote_alpha(conn, r["id"], r["quality_status"],
                         f"v12_is_overfit: {r['overfit_reason']}")
            success += 1
        except Exception as e:
            print(f"  FAILED id={r['id']}: {e}")
            failed += 1

    conn.close()
    print(f"Done. {success} demoted / {failed} failed.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
