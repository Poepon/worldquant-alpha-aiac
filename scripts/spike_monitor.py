"""Spike progress monitor — Plan v5+ V-2 tracking.

Periodically queries Postgres to track Spike run progress against the
1200-2000 alpha target. Compares post-Quasi-T1 metrics against the
pre-Quasi-T1 baseline (docs/spike_baseline_report_2026-05-02.md).

Usage:
    python scripts/spike_monitor.py                    # one-shot snapshot
    python scripts/spike_monitor.py --watch --interval 300   # poll every 5 min
    python scripts/spike_monitor.py --since-date 2026-05-02   # filter by start date
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime

import psycopg2

PRE_QUASI_BASELINE = {
    "pass_rate": 1.16,
    "pass_prov_rate": 2.76,
    "can_submit_rate_of_pass": 32.69,
    "t1_pass_rate": 3.67,
    "t2_pass_rate": 16.67,
    "t3_pass_rate": 1.54,
    "tier_none_pct": 90.24,
    "tier_none_pass_pct_of_pass": 32.69,  # 17/52
    "cross_dataset_rate": 18.19,
}


def snapshot(since_date: str | None = None) -> dict:
    conn = psycopg2.connect(
        host="localhost", port=5433,
        user="postgres", password="postgres", dbname="alpha_gpt",
    )
    cur = conn.cursor()
    agg_where = ""
    cd_where_extra = ""
    params: list = []
    if since_date:
        agg_where = "WHERE created_at >= %s"
        cd_where_extra = "AND a.created_at >= %s"
        params = [since_date]

    cur.execute(f"""
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE quality_status='PASS') AS passed,
            COUNT(*) FILTER (WHERE quality_status='PASS_PROVISIONAL') AS prov,
            COUNT(*) FILTER (WHERE quality_status='PASS' AND can_submit=TRUE) AS can_sub,
            COUNT(*) FILTER (WHERE factor_tier=1) AS t1,
            COUNT(*) FILTER (WHERE factor_tier=1 AND quality_status='PASS') AS t1_p,
            COUNT(*) FILTER (WHERE factor_tier=2) AS t2,
            COUNT(*) FILTER (WHERE factor_tier=2 AND quality_status='PASS') AS t2_p,
            COUNT(*) FILTER (WHERE factor_tier=3) AS t3,
            COUNT(*) FILTER (WHERE factor_tier=3 AND quality_status='PASS') AS t3_p,
            COUNT(*) FILTER (WHERE factor_tier IS NULL) AS none_t,
            COUNT(*) FILTER (WHERE factor_tier IS NULL AND quality_status='PASS') AS none_p
        FROM alphas {agg_where}
    """, params)
    row = cur.fetchone()
    cols = [d[0] for d in cur.description]
    s = dict(zip(cols, row))

    cur.execute(f"""
        WITH a AS (
            SELECT a.id, a.quality_status,
                   COUNT(DISTINCT df.dataset_id) AS nd
            FROM alphas a
            LEFT JOIN LATERAL jsonb_array_elements_text(a.fields_used) AS f(field_id) ON TRUE
            LEFT JOIN datafields df ON df.field_id=f.field_id AND df.region=a.region
            WHERE a.fields_used IS NOT NULL
              AND jsonb_typeof(a.fields_used)='array'
              AND jsonb_array_length(a.fields_used)>0
              {cd_where_extra}
            GROUP BY a.id, a.quality_status
        )
        SELECT COUNT(*), COUNT(*) FILTER (WHERE nd>=2)
        FROM a
    """, params)
    cd_total, cd_cross = cur.fetchone()
    s["cd_total"] = cd_total
    s["cd_cross"] = cd_cross

    cur.close()
    conn.close()
    return s


def fmt_delta(now: float, base: float) -> str:
    delta = now - base
    sign = "+" if delta >= 0 else ""
    return f"{now:6.2f}% (Δ {sign}{delta:5.2f}pp vs baseline {base:.2f}%)"


def report(s: dict, since: str | None) -> None:
    total = s["total"] or 1
    pr = s["passed"] / total * 100
    pp = (s["passed"] + s["prov"]) / total * 100
    cs_pass = (s["can_sub"] / s["passed"] * 100) if s["passed"] else 0
    t1r = (s["t1_p"] / s["t1"] * 100) if s["t1"] else 0
    t2r = (s["t2_p"] / s["t2"] * 100) if s["t2"] else 0
    t3r = (s["t3_p"] / s["t3"] * 100) if s["t3"] else 0
    none_pct = s["none_t"] / total * 100
    none_p_of_pass = (s["none_p"] / s["passed"] * 100) if s["passed"] else 0
    cd_rate = (s["cd_cross"] / s["cd_total"] * 100) if s["cd_total"] else 0

    target_low = 1200
    target_high = 2000
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    scope = f"since {since}" if since else "all-time"

    print("=" * 70)
    print(f"Spike Monitor [{ts}]  scope: {scope}")
    print("=" * 70)
    print(f"Alpha count:     {total:>5}     "
          f"target: {target_low}-{target_high}     "
          f"progress: {min(100, total*100/target_low):.0f}%")
    print(f"PASS rate:       {fmt_delta(pr, PRE_QUASI_BASELINE['pass_rate'])}")
    print(f"PASS+PROV rate:  {fmt_delta(pp, PRE_QUASI_BASELINE['pass_prov_rate'])}")
    if s["passed"]:
        print(f"can_submit/PASS: {fmt_delta(cs_pass, PRE_QUASI_BASELINE['can_submit_rate_of_pass'])}")
    print(f"T1 PASS rate:    {fmt_delta(t1r, PRE_QUASI_BASELINE['t1_pass_rate'])}  ({s['t1_p']}/{s['t1']})")
    print(f"T2 PASS rate:    {fmt_delta(t2r, PRE_QUASI_BASELINE['t2_pass_rate'])}  ({s['t2_p']}/{s['t2']})")
    print(f"T3 PASS rate:    {fmt_delta(t3r, PRE_QUASI_BASELINE['t3_pass_rate'])}  ({s['t3_p']}/{s['t3']})")
    print(f"tier=None pct:   {fmt_delta(none_pct, PRE_QUASI_BASELINE['tier_none_pct'])}  ← Quasi-T1 should reduce this")
    if s["passed"]:
        print(f"tier=None of PASS: {fmt_delta(none_p_of_pass, PRE_QUASI_BASELINE['tier_none_pass_pct_of_pass'])}  ← Quasi-T1 reclassifies into T1")
    print(f"Cross-dataset:   {fmt_delta(cd_rate, PRE_QUASI_BASELINE['cross_dataset_rate'])}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=int, default=300, help="seconds between polls")
    parser.add_argument("--since-date", default=None,
                        help="ISO date to filter alphas, e.g. 2026-05-02")
    args = parser.parse_args()

    if args.watch:
        try:
            while True:
                s = snapshot(args.since_date)
                report(s, args.since_date)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nstopped.")
    else:
        s = snapshot(args.since_date)
        report(s, args.since_date)
    return 0


if __name__ == "__main__":
    sys.exit(main())
