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

# Verdict thresholds (named so they aren't scattered magic numbers).
CORR_REAL_MIN = 0.30        # P1: |corr| above this = CoT is real, keep 5-slot
BLEND_OK_SHARE = 20         # 命题4: blended-mechanism share below this = good
PV_SWEET_OK_SHARE = 25      # P2: % of price/volume output in the 0.40-0.70 band
MAIN_UNCLASSIFIED_OK = 10   # pillar: main-gen (null)+other share below this = good
PV_FAM = "price/volume"        # MUST match the SQL CASE label in check_p2_turnover
PV_SWEET_BUCKET = "3.0.40-0.70"  # MUST match the SQL turnover-bucket label

# Pre-change baselines measured this session (2026-05-24), for before/after context.
BASELINE = {
    "stmt_lt180_pass": 50.0,   # 命题4: <180-char PASS rate (within one pillar)
    "single_pass": 10.5,       # single-mechanism PASS rate
    "blend_pass": 0.8,         # add-blended PASS rate
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
        SELECT CASE WHEN length(h.statement)<180 THEN '1.<180'
                    WHEN length(h.statement)<240 THEN '2.180-240' ELSE '3.>=240' END b,
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
    print(f"  statement 长度 vs PASS率 (baseline <180≈{BASELINE['stmt_lt180_pass']}% / >=240 断崖):")
    for b in ("1.<180", "2.180-240", "3.>=240"):
        if b in rows:
            print(f"    {b:<10} n={rows[b][0]:<5} PASS率={rows[b][1]}%")
    if n_total < MIN_N:
        _verdict("INSUFFICIENT", f"仅 {n_total} 个新 alpha,需 ≥{MIN_N}")
    else:
        short_pr = rows.get("1.<180", (0, 0))[1] or 0
        long_pr = rows.get("3.>=240", (0, 0))[1] or 0
        if short_pr > long_pr:
            _verdict("✅ 短 statement PASS 更高,命题4 方向成立",
                     f"<180 {short_pr}% vs >=240 {long_pr}%")
        else:
            _verdict("⚠️ 未见短>长优势,复查 prompt 是否生效")

    # single vs blended mechanism
    cur.execute(
        f"""
        SELECT CASE
                 WHEN expression ~ 'trade_when|if_else' THEN '条件交互'
                 WHEN expression ~ 'add\\(' THEN '线性混合add'
                 -- coarse syntactic bucket, first-match precedence; exclude BOTH
                 -- sign-flip forms multiply(-1,x) and multiply(x,-1). The arg-last
                 -- form is anchored to multiply( so a trailing ", -1)" inside a
                 -- non-multiply wrapper (e.g. power(multiply(a,b), -1)) isn't misread.
                 WHEN expression ~ 'multiply\\(' AND expression !~ 'multiply\\(\\s*-1|multiply\\([^()]*,\\s*-1' THEN '乘法耦合'
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
        if share < BLEND_OK_SHARE:
            _verdict("✅ 复合机制占比下降", f"复合 {share:.0f}% of 产出")
        else:
            _verdict("⚠️ 复合机制仍 ≥20%", f"复合 {share:.0f}% — prompt 约束未完全生效")


# ---------------------------------------------------------------------------
# Pillar classification (review H1, 2026-05-24): unknown's TRUE source is the
# r1b_mutate path (r1b_loop.py:906 fallback), NOT main generation — verified
# 265/265 unknown have r1b_mutation_depth>0, 0 from main gen. node_hypothesis
# never emits 'unknown'. So 命题4 (which only edits HYPOTHESIS_SYSTEM) CANNOT
# reduce it; the main-gen unclassified bucket is (null)/other instead. Diagnose
# the two code paths separately so the verdict attributes correctly.
# ---------------------------------------------------------------------------
def check_pillar_classification(cur, since):
    _hdr("Pillar 分类 — unknown 真实来源 (修正: unknown=r1b_mutate 缺陷,非命题4)")
    cur.execute(
        """
        SELECT COALESCE(pillar,'(null)') p,
               count(*) FILTER (WHERE r1b_mutation_depth=0 AND parent_hypothesis_id IS NULL) main_gen,
               count(*) FILTER (WHERE r1b_mutation_depth>0) r1b_mutate
        FROM hypotheses WHERE created_at > %s GROUP BY 1 ORDER BY 2 DESC
        """,
        (since,),
    )
    rows = cur.fetchall()
    main_total = sum(r[1] for r in rows)
    r1b_unknown = sum(r[2] for r in rows if r[0] == "unknown")
    main_unclassified = sum(r[1] for r in rows if r[0] in ("(null)", "other"))
    for p, mg, r1b in rows:
        print(f"    {p:<12} 主生成={mg:<5} r1b_mutate={r1b}")
    print("  注: pillar='unknown' 仅 r1b_mutate fallback 产生,与命题4/主生成无关 (region-agnostic)")
    print("  注: (null)=pillar 字段(2026-05-15)前的旧假设;宽 --since 会虚高未分类率,请用重启时间过滤")
    if main_total < MIN_N:
        _verdict("INSUFFICIENT", f"主生成假设仅 {main_total} 个")
        return
    mg_unc = 100.0 * main_unclassified / main_total
    print(f"  主生成未分类率((null)+other) = {mg_unc:.1f}%  |  r1b unknown = {r1b_unknown}")
    if mg_unc < MAIN_UNCLASSIFIED_OK:
        _verdict("✅ 主生成分类良好 → 命题4 不靠减 unknown 起效;治 unknown 应改 r1b_mutate(非主生成过滤)",
                 f"主生成未分类 {mg_unc:.1f}%")
    else:
        _verdict("⚠️ 主生成未分类率偏高 → 复查 pillar 归类逻辑", f"{mg_unc:.1f}%")


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
        FROM alphas a
        WHERE a.created_at > %s {region_clause} AND is_turnover IS NOT NULL
          AND jsonb_typeof(metrics->'_reasoning_predicted_turnover') = 'number'
        """,
        (since,),
    )
    r, mae, n = cur.fetchone()
    n = n or 0
    # predicted_turnover is float-coerced on write (JSON number), so filter by
    # jsonb_typeof='number' — a char-class regex would silently drop negatives /
    # sci-notation. Print has_slot vs n so an INSUFFICIENT is explainable.
    print(f"  predicted vs actual turnover: 数值样本 n={n} "
          f"(落库 {has_slot} 中 {has_slot - n} 个非数值/缺 turnover 被排除) corr={r} MAE={mae}")
    if n < MIN_N or r is None:
        _verdict("INSUFFICIENT", f"仅 {n} 个数值样本,需 ≥{MIN_N}")
    elif abs(r) >= CORR_REAL_MIN:
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
        if f == PV_FAM:
            pv_total += n
            if tb == PV_SWEET_BUCKET:
                pv_sweet_n = n
    print(f"  baseline 价量 0.40-0.70 PASS≈{BASELINE['pv_sweet_pass']}%")
    if pv_total >= MIN_N:
        share = 100.0 * pv_sweet_n / pv_total
        if share >= PV_SWEET_OK_SHARE:
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
    if not short or short[1] < MIN_N:
        _verdict("INSUFFICIENT", "短<180 样本不足,未判定愚人金")
        return
    prov, hp, cs = short[2], short[3], short[4]
    if cs > 0:
        _verdict("✅ 短 statement 有真 can_submit,非愚人金", f"can_submit={cs}")
    elif prov > 0 and hp == 0:
        _verdict("⚠️ 短 statement 只产 PASS_PROVISIONAL(0 PASS/0 can_submit) → 疑似愚人金,查 self-corr")
    elif hp > 0:
        _verdict("🔶 短 statement 有 PASS 但 0 can_submit → 提交被 self-corr/prod-corr 阻挡(非愚人金,是提交多样性问题)")
    else:
        _verdict("🔶 短 statement 全 FAIL(无 PASS/PROVISIONAL) → 命题4 未提升此样本", "can_submit=0")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None,
                    help="ISO datetime; default = now-2days. Set to your restart time.")
    ap.add_argument("--region", default=None, help="filter to one region, e.g. USA")
    args = ap.parse_args()

    since = args.since or (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
    region_clause = ""
    if args.region:
        if not args.region.isalnum():
            raise SystemExit(f"--region 必须是字母数字 (防 SQL 注入): {args.region!r}")
        region_clause = f"AND a.region = '{args.region}'"

    print(f"分析 created_at > {since}" + (f" region={args.region}" if args.region else "")
          + "  (基线=本会话 2026-05-24 改动前实测)")

    conn = _conn()
    cur = conn.cursor()
    try:
        check_thesis4(cur, since, region_clause)
        check_pillar_classification(cur, since)
        check_p1_cot(cur, since, region_clause)
        check_p2_turnover(cur, since, region_clause)
        check_foolsgold(cur, since, region_clause)
    finally:
        cur.close()
        conn.close()
    print("\n完成。INSUFFICIENT 项表示重启后数据还不够,过几天再跑。")


if __name__ == "__main__":
    main()
