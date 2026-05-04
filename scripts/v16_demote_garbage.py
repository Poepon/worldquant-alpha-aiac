"""V-16 garbage demotion — flag PENDING orphan alphas as FAIL when they
exhibit unambiguous data-anomaly signatures.

Plan v5+ §V-16 follow-up (2026-05-04 user decision):
sharpe > 3.0 alone isn't enough to call an alpha garbage — many sync'd
BRAIN-side alphas have legitimately strong signals. The garbage signature
requires THREE simultaneous conditions:

  1. quality_status = 'PENDING'  (BRAIN-sync orphan, never went through
                                  evaluation.py)
  2. is_sharpe > 3.0             (above V-16 threshold)
  3. drawdown = 0                (simulation boundary anomaly)
  4. turnover >= 0.50            (extreme turnover; cost vacuum)

These 4 together are diagnostic of either:
  - BRAIN simulation anomalies (signal degenerate to constant)
  - 100% daily churn that ignores transaction costs entirely
  - Numerator/denominator pathologies (divide producing huge values
    on certain dates)

Such rows pollute the "PENDING" pool used by FactorLibrary UI / future
RAG queries, so we promote them to 'FAIL' with reason='v16_garbage'.
This makes them visible in failure analytics + KB pitfall learning.

Usage:
    python scripts/v16_demote_garbage.py --dry-run     # preview
    python scripts/v16_demote_garbage.py               # execute
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg2
import psycopg2.extras


def find_garbage(conn) -> list:
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT id, alpha_id, factor_tier, quality_status, expression,
               (metrics->>'sharpe')::float AS sharpe,
               (metrics->>'drawdown')::float AS drawdown,
               (metrics->>'turnover')::float AS turnover,
               metrics
        FROM alphas
        WHERE quality_status = 'PENDING'
          AND (metrics->>'sharpe')::float > 3.0
          AND COALESCE((metrics->>'drawdown')::float, 0) = 0
          AND COALESCE((metrics->>'turnover')::float, 0) >= 0.50
        ORDER BY (metrics->>'sharpe')::float DESC
    """)
    rows = cur.fetchall()
    cur.close()
    return rows


def demote(conn, alpha_id_int: int, sharpe: float, turnover: float) -> None:
    cur = conn.cursor()
    reason = (
        f"v16_garbage: sharpe={sharpe:.2f}, drawdown=0, turnover={turnover:.2f} — "
        "unambiguous data-anomaly signature"
    )[:200]
    try:
        cur.execute(
            """
            UPDATE alphas SET quality_status = 'FAIL',
                              updated_at = NOW()
            WHERE id = %s AND quality_status = 'PENDING'
            """,
            (alpha_id_int,),
        )
        if cur.rowcount != 1:
            raise RuntimeError(f"alpha id={alpha_id_int} update affected {cur.rowcount} rows")
        cur.execute(
            """
            INSERT INTO alpha_status_transitions
                (alpha_id, old_status, new_status, reason, source, transitioned_at)
            VALUES (%s, 'PENDING', 'FAIL', %s, 'v16_demote_garbage', NOW())
            """,
            (alpha_id_int, reason),
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
    args = ap.parse_args()

    conn = psycopg2.connect(
        host="localhost", port=5433,
        user="postgres", password="postgres", dbname="alpha_gpt",
    )
    rows = find_garbage(conn)

    if not rows:
        print("No PENDING garbage alphas matching criteria.")
        conn.close()
        return 0

    print("=" * 70)
    print(f"V-16 garbage demotion — {len(rows)} PENDING alphas → FAIL")
    print("=" * 70)
    print(f"Criteria: PENDING + sharpe>3 + drawdown=0 + turnover>=0.50")
    print()
    print("Affected:")
    for r in rows:
        aid = r["alpha_id"] or "(none)"
        tier = r["factor_tier"] if r["factor_tier"] is not None else "?"
        print(f"  id={r['id']:>5} alpha_id={aid:<10} T{tier}  "
              f"sharpe={r['sharpe']:>6.2f}  turn={r['turnover']:.2f}  dd={r['drawdown']:.2f}")
        print(f"          expression: {(r['expression'] or '')[:90]}")
    print()

    if args.dry_run:
        print("[dry-run] No changes.")
        conn.close()
        return 0

    print("Demoting to FAIL...")
    success = 0
    failed = 0
    for r in rows:
        try:
            demote(conn, r["id"], r["sharpe"], r["turnover"])
            success += 1
        except Exception as e:
            print(f"  FAILED id={r['id']}: {e}")
            failed += 1
    conn.close()
    print(f"Done. {success} demoted to FAIL / {failed} failed.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
