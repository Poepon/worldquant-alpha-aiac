"""Read-only dry-run for Feature 1 (2026-05-24): sync verdict derivation.

Runs the new `_derive_verdict_from_brain` over real synced alphas WITHOUT writing
the DB, and prints the verdict distribution + S1-guardrail trigger count, so we can
confirm the honest expectations before the next 6h sync re-classifies live:

  - the 338 degenerate synced PENDING rows (sharpe=0/fitness=null) stay PENDING
    (raw-None guard), changing 0 conclusions;
  - full PASS is ~0 (SELF_CORRELATION is always PENDING → unverified self_corr +
    no OS evidence);
  - rows with CONCENTRATED_WEIGHT / LOW_SUB_UNIVERSE_SHARPE FAIL route to FAIL.

Usage: python scripts/feature1_sync_verdict_dryrun.py
"""
import os
from collections import Counter

import psycopg2
import psycopg2.extras

from backend.tasks.sync_tasks import _derive_verdict_from_brain
from backend.can_submit import compute_can_submit


def _pw():
    for line in open(os.path.join(os.path.dirname(__file__), "..", ".env"), encoding="utf-8"):
        if line.strip().startswith("POSTGRES_PASSWORD="):
            return line.split("=", 1)[1].strip()
    return ""


def _verdict_for(row):
    is_metrics = row["is_metrics"] or {}
    os_metrics = row["os_metrics"] or {}
    checks = (is_metrics.get("checks") if isinstance(is_metrics, dict) else None) or []
    # a_data carries is.checks for compute_can_submit + the verdict (T5 same source)
    a_data = {"is": {"checks": checks}}
    can_sub, _f, _p = compute_can_submit(a_data)
    vr = _derive_verdict_from_brain(
        a_data, is_metrics, os_metrics, row.get("expression") or "", can_sub
    )
    if vr is None:
        return ("PENDING", "missing_core_metric", False)
    demoted = vr.decision.reason == "brain_unsubmittable"
    return (vr.decision.status, vr.decision.reason, demoted)


def _report(label, rows):
    statuses, reasons = Counter(), Counter()
    s1 = 0
    for r in rows:
        status, reason, demoted = _verdict_for(r)
        statuses[status] += 1
        reasons[reason] += 1
        if demoted:
            s1 += 1
    print(f"\n=== {label} (n={len(rows)}) ===")
    print("  status:", dict(statuses))
    print("  reason:", dict(reasons))
    print("  S1 guardrail (PASS→PROVISIONAL):", s1)


def main():
    conn = psycopg2.connect(host="localhost", port=5433, user="postgres",
                            password=_pw(), dbname="alpha_gpt")
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # (1) the 338 synced PENDING target population
    cur.execute("""
        SELECT alpha_id, expression, is_metrics, os_metrics
        FROM alphas
        WHERE task_id IS NULL AND quality_status = 'PENDING'
    """)
    _report("synced PENDING (target population)", cur.fetchall())

    # (2) a sample of synced rows with real metrics (sharpe>=1.25) — where the
    # new gates (CONCENTRATED/sub_univ/self_corr/V-16) actually bite
    cur.execute("""
        SELECT alpha_id, expression, is_metrics, os_metrics
        FROM alphas
        WHERE task_id IS NULL
          AND (is_metrics->>'sharpe') IS NOT NULL
          AND (is_metrics->>'sharpe')::float >= 1.25
          AND (is_metrics->>'fitness') IS NOT NULL
        LIMIT 800
    """)
    _report("synced real-metrics sharpe>=1.25 sample", cur.fetchall())

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
