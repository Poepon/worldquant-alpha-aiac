#!/usr/bin/env python
"""Verify the 2026-05-24 prompt / optimization changes against live mining data.

Run this AFTER restarting the backend and letting it mine for a while. It filters
to alphas / hypotheses created after --since (the restart time) so you measure the
NEW prompts' output, not the pre-change baseline. Each check prints the data, a
VERDICT, and a recommended next action.

The six commits this validates:
  bfbef3f operator signatures + operator-reality guardrails
  4759de0 optimization sign-flip gated to sharpe<0
  0f33826 hypothesis single-mechanism + concise statement (命题4)
  ac1421a velocity/turnover model recalibration (P2)
  46b82b7 5-slot reasoning chain persistence (P1)
  0691013 dead optimization-prompt removal

Usage:
  python scripts/verify_prompt_changes_2026_05_24.py --since '2026-05-25 09:00'
  python scripts/verify_prompt_changes_2026_05_24.py            # default: last 2 days
  python scripts/verify_prompt_changes_2026_05_24.py --region USA

Read-only. Safe to run any time; checks with too little post-restart data report
INSUFFICIENT rather than a misleading verdict.
"""
from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta

import psycopg2
from dotenv import load_dotenv

PASS = "quality_status IN ('PASS','PASS_PROVISIONAL')"
MIN_N = 30  # below this a verdict is statistically meaningless

# Pre-change baselines measured this session (2026-05-24), for before/after context.
BASELINE = {
    "stmt_ge240_pass": 2.5,    # 命题4: ≥240-char statements PASS rate
    "stmt_lt180_pass": 50.0,   # 命题4: <180-char (within one pillar)
    "single_pass": 10.5,       # single-mechanism PASS rate
    "blend_pass": 0.8,         # add-blended PASS rate
    "unknown_share": 17.0,     # unknown pillar share of hypotheses (approx)
    "pv_sweet_pass": 24.1,     # P2: price/volume at 0.40-0.70 PASS rate
}


def _conn():
    load_dotenv()
    return psycopg2.connect(
        host=os.getenv("POSTGRES_SERVER", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", "5433")),
        dbname=os.getenv("POSTGRES_DB", "alpha_gpt"),
        user=os.getenv("POSTGRES_USER", "postgres"),
        password=os.getenv("POSTGRES_PASSWORD"),
    )


def _hdr(title: str) -> None:
    print(f"\n{'='*72}\n{title}\n{'='*72}")


def _verdict(label: str, detail: str = "") -> None:
    print(f"  → VERDICT: {label}" + (f"  ({detail})" if detail else ""))


# ---------------------------------------------------------------------------
# 命题4 (0f33826): statement length & single-mechanism should lift PASS rate
# ---------------------------------------------------------------------------
def check_thesis4(cur, since, region_clause):
    _hdr("命题4 — statement 简洁 + 单一机制 (commit 0f33826)")

    cur.execute(
        f"""
        SELECT CASE WHEN length(h.statement)<180 THEN '<180' ELSE '>=180' END b,
               count(a.id) n,
               round(100.0*count(*) FILTER (WHERE a.{PASS})/NULLIF(count(a.id),0),1) pr
        FROM alphas a JOIN hypotheses h ON a.hypothesis_id=h.id
        WHERE a.created_at > %s {region_clause} AND h.statement IS NOT NULL
        GROUP BY 1 ORDER BY 1
        """,
        (since,),
    )
    rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    n_total = sum(v[0] for v in rows.values())
    print(f"  statement 长度 vs PASS率 (baseline <180≈{BASELINE['stmt_lt180_pass']}% / "
          f">=180 断崖):")
    for b in ("<180", ">=180"):
        if b in rows:
            print(f"    {b:<7} n={rows[b][0]:<5} PASS率={rows[b][1]}%")
    if n_total < MIN_N:
        _verdict("INSUFFICIENT", f"仅 {n_total} 个新 alpha,需 ≥{MIN_N}")
    else:
        short_pr = rows.get("<180", (0, 0))[1] or 0
        long_pr = rows.get(">=180", (0, 0))[1] or 0
        if short_pr > long_pr:
            _verdict("✅ 短 statement PASS 更高,命题4 方向成立",
                     f"{short_pr}% vs {long_pr}%")
        else:
            _verdict("⚠️ 未见短>长优势,复查 prompt 是否生效")

    # single vs blended mechanism
    cur.execute(
        f"""
        SELECT CASE
                 WHEN expression ~ 'trade_when|if_else' THEN '条件交互'
                 WHEN expression ~ 'add\\(' THEN '线性混合add'
                 WHEN expression ~ 'multiply\\(' AND expression !~ 'multiply\\(\\s*-1' THEN '乘法耦合'
                 ELSE '单一信号链' END typ,
               count(*) n,
               round(100.0*count(*) FILTER (WHERE {PASS})/NULLIF(count(*),0),1) pr
        FROM alphas a WHERE a.created_at > %s {region_clause} AND expression IS NOT NULL
        GROUP BY 1 ORDER BY 2 DESC
        """,
        (since,),
    )
    print(f"  机制类型 vs PASS率 (baseline 单一≈{BASELINE['single_pass']}% / "
          f"混合≈{BASELINE['blend_pass']}%):")
    blend_total = 0
    grand = 0
    for typ, n, pr in cur.fetchall():
        print(f"    {typ:<12} n={n:<5} PASS率={pr}%")
        grand += n
        if typ != "单一信号链":
            blend_total += n
    if grand >= MIN_N:
        share = 100.0 * blend_total / grand
        if share < 20:
            _verdict("✅ 复合机制占比下降", f"复合 {share:.0f}% of 产出")
        else:
            _verdict("⚠️ 复合机制仍 ≥20%", f"复合 {share:.0f}% — prompt 约束未完全生效")


# ---------------------------------------------------------------------------
# 命题4 unknown share (0f33826 should reduce it at the source)
# ---------------------------------------------------------------------------
def check_unknown_share(cur, since):
    _hdr("命题4 衍生 — unknown pillar 占比 (决定 P-A② 是否还需要)")
    cur.execute(
        """
        SELECT COALESCE(pillar,'(null)') p, count(*) n
        FROM hypotheses WHERE created_at > %s GROUP BY 1
        """,
        (since,),
    )
    rows = dict(cur.fetchall())
    total = sum(rows.values())
    unk = rows.get("unknown", 0)
    print(f"  baseline unknown 占比 ≈{BASELINE['unknown_share']}%")
    if total < MIN_N:
        _verdict("INSUFFICIENT", f"仅 {total} 个新假设")
        return
    share = 100.0 * unk / total
    print(f"  unknown {unk}/{total} = {share:.1f}%")
    if share < 8:
        _verdict("✅ unknown 占比显著下降 → P-A② 不需要", f"{share:.1f}%")
    elif share < BASELINE["unknown_share"]:
        _verdict("🔶 unknown 下降但仍偏高 → 可考虑 node 侧 unknown→重试", f"{share:.1f}%")
    else:
        _verdict("⚠️ unknown 未降 → 命题4 未生效或 unknown 另有来源")


# ---------------------------------------------------------------------------
# P1 (46b82b7): predicted_turnover vs actual — is the 5-slot CoT real?
# ---------------------------------------------------------------------------
def check_p1_cot(cur, since, region_clause):
    _hdr("P1 — 5-slot CoT 真实性: predicted_turnover vs 实际 (commit 46b82b7)")
    cur.execute(
        f"""
        SELECT count(*) n,
               count(*) FILTER (WHERE metrics ? '_reasoning_predicted_turnover') has_slot
        FROM alphas a WHERE a.created_at > %s {region_clause}
        """,
        (since,),
    )
    n_all, has_slot = cur.fetchone()
    print(f"  落库覆盖: {has_slot}/{n_all} 新 alpha 带 _reasoning_predicted_turnover")
    if has_slot == 0:
        _verdict("INSUFFICIENT", "无落库数据 — 确认已重启 + 跑过 code_gen")
        return

    cur.execute(
        f"""
        SELECT corr((metrics->>'_reasoning_predicted_turnover')::float, is_turnover) r,
               round(avg(abs((metrics->>'_reasoning_predicted_turnover')::float - is_turnover))::numeric,3) mae,
               count(*) n
        FROM alphas
        WHERE created_at > %s {region_clause} AND is_turnover IS NOT NULL
          AND metrics->>'_reasoning_predicted_turnover' ~ '^[0-9.]+$'
        """,
        (since,),
    )
    r, mae, n = cur.fetchone()
    print(f"  predicted vs actual turnover: n={n} corr={r} MAE={mae}")
    if n is None or n < MIN_N or r is None:
        _verdict("INSUFFICIENT", f"仅 {n} 个数值样本,需 ≥{MIN_N}")
    elif abs(r) >= 0.3:
        _verdict("✅ CoT 真实(predicted 与 actual 相关) → 保留 5-slot + 喂 P2 校准",
                 f"corr={r:.2f}")
    else:
        _verdict("❌ CoT 走过场(predicted 与 actual 不相关,LLM 编数字) → 删 5-slot 省 xhigh token",
                 f"corr={r:.2f}")


# ---------------------------------------------------------------------------
# P2 (ac1421a): output turnover should migrate toward 0.40-0.70 for PV signals
# ---------------------------------------------------------------------------
def check_p2_turnover(cur, since, region_clause):
    _hdr("P2 — turnover 分布迁移 + 信号源族×turnover (commit ac1421a)")
    cur.execute(
        f"""
        SELECT CASE
                 WHEN fields_used::text ~ 'eps|cash|accrual|roe|margin|book|fn_|liab|income' THEN 'fundamental'
                 WHEN fields_used::text ~ 'close|volume|returns|vwap|adv|high|low|cap' THEN 'price/volume'
                 ELSE 'other' END fam,
               CASE WHEN is_turnover<0.20 THEN '1.<0.20' WHEN is_turnover<0.40 THEN '2.0.20-0.40'
                    WHEN is_turnover<0.70 THEN '3.0.40-0.70' ELSE '4.>=0.70' END tb,
               count(*) n,
               round(100.0*count(*) FILTER (WHERE {PASS})/NULLIF(count(*),0),1) pr,
               count(*) FILTER (WHERE can_submit) cs
        FROM alphas a
        WHERE a.created_at > %s {region_clause} AND is_turnover IS NOT NULL
          AND fields_used IS NOT NULL AND fields_used::text NOT IN ('null','[]','')
        GROUP BY 1,2 HAVING count(*)>=5 ORDER BY 1,2
        """,
        (since,),
    )
    rows = cur.fetchall()
    if not rows:
        _verdict("INSUFFICIENT", "无足量分组数据")
        return
    fam = None
    pv_sweet_n = 0
    pv_total = 0
    for f, tb, n, pr, cs in rows:
        if f != fam:
            print(f"  --- {f} ---")
            fam = f
        print(f"    TO {tb:<12} n={n:<4} PASS率={pr}% can_submit={cs}")
        if f == "price/volume":
            pv_total += n
            if tb == "3.0.40-0.70":
                pv_sweet_n = n
    print(f"  baseline 价量 0.40-0.70 PASS≈{BASELINE['pv_sweet_pass']}%")
    if pv_total >= MIN_N:
        share = 100.0 * pv_sweet_n / pv_total
        if share >= 25:
            _verdict("✅ 价量产出向 0.40-0.70 迁移", f"{share:.0f}% of 价量在甜区")
        else:
            _verdict("🔶 价量甜区占比偏低,观察更多数据", f"{share:.0f}%")
    else:
        _verdict("INSUFFICIENT", f"价量样本 {pv_total} < {MIN_N}")


# ---------------------------------------------------------------------------
# 命题4 trade-off: did concision trade PASS rate for fool's-gold PROVISIONAL?
# ---------------------------------------------------------------------------
def check_foolsgold(cur, since, region_clause):
    _hdr("命题4 trade-off — 短/单一是否产愚人金(PASS_PROVISIONAL 多但 can_submit 不增)")
    cur.execute(
        f"""
        SELECT CASE WHEN length(h.statement)<180 THEN '短<180' ELSE '长>=180' END b,
               count(a.id) n,
               count(*) FILTER (WHERE a.quality_status='PASS_PROVISIONAL') prov,
               count(*) FILTER (WHERE a.quality_status='PASS') hard_pass,
               count(*) FILTER (WHERE a.can_submit) cs
        FROM alphas a JOIN hypotheses h ON a.hypothesis_id=h.id
        WHERE a.created_at > %s {region_clause} AND h.statement IS NOT NULL
        GROUP BY 1 ORDER BY 1
        """,
        (since,),
    )
    rows = cur.fetchall()
    if not rows or sum(r[1] for r in rows) < MIN_N:
        _verdict("INSUFFICIENT", "数据不足")
        return
    for b, n, prov, hp, cs in rows:
        cs_rate = 100.0 * cs / n if n else 0
        print(f"    {b:<8} n={n:<5} PASS_PROVISIONAL={prov} PASS={hp} can_submit={cs} ({cs_rate:.1f}%)")
    short = next((r for r in rows if r[0] == "短<180"), None)
    if short and short[1] >= MIN_N:
        prov, hp, cs = short[2], short[3], short[4]
        if cs == 0 and prov > 0:
            _verdict("⚠️ 短 statement 只产 PASS_PROVISIONAL 0 can_submit → 疑似愚人金,需查 self-corr")
        else:
            _verdict("✅ 短 statement 有真 can_submit,非纯愚人金", f"can_submit={cs}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None,
                    help="ISO datetime; default = now-2days. Set to your restart time.")
    ap.add_argument("--region", default=None, help="filter to one region, e.g. USA")
    args = ap.parse_args()

    since = args.since or (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
    region_clause = ""
    if args.region:
        region_clause = f"AND a.region = '{args.region}'"

    print(f"分析 created_at > {since}" + (f" region={args.region}" if args.region else "")
          + "  (基线=本会话 2026-05-24 改动前实测)")

    conn = _conn()
    cur = conn.cursor()
    try:
        check_thesis4(cur, since, region_clause)
        check_unknown_share(cur, since)
        check_p1_cot(cur, since, region_clause)
        check_p2_turnover(cur, since, region_clause)
        check_foolsgold(cur, since, region_clause)
    finally:
        cur.close()
        conn.close()
    print("\n完成。INSUFFICIENT 项表示重启后数据还不够,过几天再跑。")


if __name__ == "__main__":
    main()
