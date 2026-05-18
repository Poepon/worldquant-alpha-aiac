"""Backfill demote — alphas with PASS/PASS_PROVISIONAL but BRAIN check FAIL.

User decision (2026-05-02): CONCENTRATED_WEIGHT and LOW_SUB_UNIVERSE_SHARPE
FAILing must NOT keep PASS — alpha must enter optimization iteration.

evaluation.py:hard_gate already enforces this at creation time, but BRAIN
checks often arrive PENDING when fresh alphas are evaluated, then flip to
FAIL on the next BRAIN sync. This script catches the historical residue —
existing alphas whose PASS status was set before checks completed.

Demotes such alphas: PASS / PASS_PROVISIONAL → OPTIMIZE so they re-enter
mining_agent's optimization candidate pool (mining_agent.py:622).

Usage:
    python scripts/demote_brain_check_fails.py --dry-run      # preview
    python scripts/demote_brain_check_fails.py                # execute
"""
from __future__ import annotations

import argparse
import asyncio
import sys

import psycopg2
import psycopg2.extras


HARD_DEMOTE_CHECKS = ("CONCENTRATED_WEIGHT", "LOW_SUB_UNIVERSE_SHARPE")


def find_affected_alphas(conn) -> list:
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        WITH affected AS (
            SELECT
                a.id,
                a.alpha_id,
                a.task_id,
                a.quality_status,
                array_agg(c->>'name') FILTER (
                    WHERE c->>'result' = 'FAIL'
                      AND c->>'name' = ANY(%s)
                ) AS hard_fails
            FROM alphas a
            LEFT JOIN LATERAL jsonb_array_elements(a.metrics->'checks') c ON TRUE
            WHERE a.quality_status IN ('PASS', 'PASS_PROVISIONAL')
            GROUP BY a.id
        )
        SELECT * FROM affected
        WHERE hard_fails IS NOT NULL AND array_length(hard_fails, 1) > 0
        ORDER BY id
    """, (list(HARD_DEMOTE_CHECKS),))
    rows = cur.fetchall()
    cur.close()
    return rows


def demote_alpha(conn, alpha_id: int, fails: list, prev_status: str) -> None:
    """Demote one alpha; commits per-row so a single failure doesn't abort the batch."""
    cur = conn.cursor()
    # 200-char limit on reason column — truncate fail list to fit.
    reason = f"backfill_brain_check_fails: {','.join(fails)}"[:200]
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
            VALUES (%s, %s, 'OPTIMIZE', %s, 'backfill_demote', NOW())
            """,
            (alpha_id, prev_status, reason),
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
    parser.add_argument("--task-min", type=int, default=None,
                        help="restrict to task_id >= N (e.g. spike batch only)")
    parser.add_argument("--task-max", type=int, default=None)
    args = parser.parse_args()

    conn = psycopg2.connect(
        host="localhost", port=5433,
        user="postgres", password="postgres", dbname="alpha_gpt",
    )

    rows = find_affected_alphas(conn)
    if args.task_min is not None:
        rows = [r for r in rows if r["task_id"] >= args.task_min]
    if args.task_max is not None:
        rows = [r for r in rows if r["task_id"] <= args.task_max]

    if not rows:
        print("No affected alphas found.")
        conn.close()
        return 0

    print("=" * 70)
    print(f"Backfill demote — {len(rows)} alphas with PASS/PROVISIONAL + BRAIN check FAIL")
    print("=" * 70)

    by_status = {}
    for r in rows:
        by_status.setdefault(r["quality_status"], 0)
        by_status[r["quality_status"]] += 1

    print(f"By status:")
    for s, n in sorted(by_status.items()):
        print(f"  {s:20} → OPTIMIZE: {n}")
    print()

    print("First 15 affected:")
    for r in rows[:15]:
        aid = r["alpha_id"] or "(none)"
        task = r["task_id"] if r["task_id"] is not None else "?"
        print(f"  id={r['id']:>5} alpha_id={aid:<10} task={task} "
              f"status={r['quality_status']:<20} fails={r['hard_fails']}")
    if len(rows) > 15:
        print(f"  ... and {len(rows) - 15} more")
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
            demote_alpha(conn, r["id"], r["hard_fails"], r["quality_status"])
            success += 1
        except Exception as e:
            print(f"  FAILED id={r['id']}: {e}")
            failed += 1

    conn.close()
    print(f"Done. {success} demoted / {failed} failed.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
