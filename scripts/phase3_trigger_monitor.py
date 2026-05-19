"""Phase 3 trigger condition monitor.

Plan v5+ Phase 3 (hypothesis-centric main-loop invert) was deferred to Q3
reassessment per user decision: "let Phase 2 accumulate data before
deciding". This script computes the 4 trigger conditions weekly so the
team knows when Phase 3 ROI has crossed the bar.

Trigger conditions (all must hold):

  1. **Phase 2 A/B PASS rate uplift ≥ 5 pp, sustained 14 days**
     LEVEL=2 cohort PASS rate beats LEVEL=0 cohort by ≥ 5 pp over rolling
     14-day window. Statistical: simple proportion difference, n ≥ 20 per
     cohort for usable signal.

  2. **Phase 2 hypothesis abandon rate in [30%, 50%]**
     (ABANDONED + SUPERSEDED) / total ratio. SUPERSEDED is functionally
     equivalent to abandon — node_hypothesis replaces a previous hid with
     a new one and the prior gets marked SUPERSEDED rather than ABANDONED.
     B6's "ABANDONED only" view drops to 0 because the upstream
     replacement path fires before B6 attribution can accumulate.
     < 30% = LLM 假设太宽松,什么都不淘汰; > 50% = prompt 问题或阈值过严.

  3. **Cross-dataset alpha ratio ≥ 30%**
     Alphas linked to a hypothesis whose dataset_pool has ≥ 2 entries
     (i.e. hypothesis explicitly intended cross-dataset signal). High
     ratio = hypothesis-driven cross-dataset mining is producing real
     value-add; low ratio = users still anchoring single dataset.

  4. **No regression on existing metrics**
     pk=7810-era IQC marginal-positive submission rate doesn't drop
     vs pre-Phase 2 baseline.

Output: docs/phase3_readiness/trigger_monitor_<date>.md with per-
condition PASS/FAIL flag + raw numbers + recommendation.

Run standalone:
  venv/Scripts/python.exe scripts/phase3_trigger_monitor.py

Or schedule via celery_app.beat_schedule (weekly cadence).
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from backend.database import AsyncSessionLocal


# Trigger thresholds (encoded as constants so reviewers can adjust without
# re-reading the plan).
UPLIFT_PP_THRESHOLD = 5.0   # %-pt
UPLIFT_SUSTAIN_DAYS = 14
COHORT_MIN_N = 20
ABANDON_LO = 0.30
ABANDON_HI = 0.50
CROSS_DATASET_THRESHOLD = 0.30
REGRESSION_TOLERANCE_PP = 5.0   # baseline -5pp triggers regression alarm


async def cohort_pass_rate(db, days: int, variant_filter: str) -> dict:
    """Return PASS-rate stats for a Phase 2 variant cohort over last N days.

    variant_filter examples:
      "= '2'"     → LEVEL=2 cohort (Phase 2 typed)
      "IS NULL OR config->>'hypothesis_centric_variant' = '0'" → legacy

    V-22.13b (2026-05-13): restrict comparison to AUTONOMOUS_TIER1 tasks
    only — CONTINUOUS_CASCADE tasks include T2/T3 wrapper paths that
    inflate PASS rate independently of hypothesis variant. Spike showed
    mixed-mode comparison gave -13pp uplift; same-mode (AUTONOMOUS_TIER1
    only) gave +4.8pp uplift. Apples-to-apples requires same agent_mode.
    """
    sql = text(f"""
        SELECT
            count(DISTINCT a.id) as alpha_n,
            count(DISTINCT a.id) FILTER (WHERE a.quality_status = 'PASS') as pass_n,
            count(DISTINCT a.id) FILTER (WHERE a.quality_status IN ('PASS','PASS_PROVISIONAL')) as prov_or_pass_n
        FROM alphas a
        JOIN mining_tasks t ON a.task_id = t.id
        WHERE a.created_at > NOW() - INTERVAL '{days} days'
          AND (t.config->>'hypothesis_centric_variant' {variant_filter})
          AND t.agent_mode = 'AUTONOMOUS_TIER1'
    """)
    row = (await db.execute(sql)).first()
    alpha_n, pass_n, prov_or_pass_n = row[0], row[1], row[2]
    return {
        "alpha_n": alpha_n,
        "pass_n": pass_n,
        "prov_or_pass_n": prov_or_pass_n,
        "pass_rate": (pass_n / alpha_n) if alpha_n else 0.0,
        "pass_or_prov_rate": (prov_or_pass_n / alpha_n) if alpha_n else 0.0,
    }


async def hypothesis_abandon_stats(db, days: int) -> dict:
    """Phase 2 hypothesis lifecycle distribution over last N days.

    V-25.A correction (2026-05-13): exclude V-19.7 zombie-cleanup rows.
    The 290 SUPERSEDED rows with abandon_reason starting with "V-19.7
    zombie" came from a one-shot manual transition on 2026-05-06 to
    clean up pre-V-19.7 multi-sibling rows. Including them gave a fake
    43.4% retirement signal that has nothing to do with live B6 /
    G-refine. Real Phase 2 retirement rate is currently 0% — the
    mechanism has not fired end-to-end in production yet.

    Abandon-rate definition: (ABANDONED + SUPERSEDED) / total, where
    SUPERSEDED counts toward abandonment because the G-refine loop
    converts B6 fires into SUPERSEDED (functionally equivalent).
    """
    sql = text(f"""
        SELECT status, count(*) FROM hypotheses
        WHERE created_at > NOW() - INTERVAL '{days} days'
          AND COALESCE(abandon_reason, '') NOT LIKE 'V-19.7 zombie%'
        GROUP BY status
    """)
    by_status = {}
    total = 0
    for row in (await db.execute(sql)).all():
        by_status[row[0]] = row[1]
        total += row[1]
    abandoned = by_status.get("ABANDONED", 0)
    superseded = by_status.get("SUPERSEDED", 0)
    retired = abandoned + superseded
    return {
        "total": total,
        "by_status": by_status,
        "abandoned_only": abandoned,
        "superseded_only": superseded,
        "retired_total": retired,
        "abandon_rate": (retired / total) if total else 0.0,
        "abandon_rate_strict": (abandoned / total) if total else 0.0,
    }


async def cross_dataset_ratio(db, days: int) -> dict:
    """Ratio of recently saved alphas whose parent hypothesis pulled fields
    from ≥ 2 datasets (Phase 1 cross-dataset goal)."""
    sql = text(f"""
        SELECT
            count(*) as total,
            count(*) FILTER (WHERE jsonb_array_length(coalesce(h.dataset_pool, '[]'::jsonb)) >= 2) as multi_ds
        FROM alphas a
        LEFT JOIN hypotheses h ON a.hypothesis_id = h.id
        WHERE a.created_at > NOW() - INTERVAL '{days} days'
          AND a.hypothesis_id IS NOT NULL
    """)
    row = (await db.execute(sql)).first()
    total, multi = row[0], row[1]
    return {
        "total_linked": total,
        "multi_dataset": multi,
        "cross_ratio": (multi / total) if total else 0.0,
    }


async def iqc_marginal_positive_rate(db, days: int) -> dict:
    """V-22.12: of can_submit=True alphas audited, what fraction has
    metrics._iqc_marginal.delta_score > 0?

    Returns mean/median delta_score for cohort visibility — even when 0%
    positive, the magnitude tells us how badly the can_submit gate is
    calibrated vs actual portfolio impact.
    """
    sql = text(f"""
        SELECT
            count(*) as audited_n,
            count(*) FILTER (WHERE (metrics->'_iqc_marginal'->>'delta_score')::numeric > 0) as positive_n,
            avg((metrics->'_iqc_marginal'->>'delta_score')::numeric) as mean_delta,
            percentile_disc(0.5) within group (
                order by (metrics->'_iqc_marginal'->>'delta_score')::numeric
            ) as median_delta
        FROM alphas
        WHERE created_at > NOW() - INTERVAL '{days} days'
          AND metrics ? '_iqc_marginal'
    """)
    row = (await db.execute(sql)).first()
    audited, positive, mean_d, median_d = row[0], row[1], row[2], row[3]
    return {
        "audited_n": audited,
        "positive_n": positive,
        "positive_rate": (positive / audited) if audited else 0.0,
        "mean_delta_score": float(mean_d) if mean_d is not None else None,
        "median_delta_score": float(median_d) if median_d is not None else None,
    }


def emoji_for(passed: bool, gating: bool = True) -> str:
    if not gating:
        return "—"
    return "✅" if passed else "❌"


async def main() -> int:
    print("=== Phase 3 trigger monitor ===\n")

    async with AsyncSessionLocal() as db:
        # Phase 2 A/B cohorts over last 14 days
        cohort_a = await cohort_pass_rate(
            db, UPLIFT_SUSTAIN_DAYS, variant_filter="= '2'",
        )
        # 2026-05-13 fix: exclude variant='1' (Phase 1 cross-dataset gradient,
        # 05-06~08 window) from the LEVEL=0 cohort. Including it conflates
        # Phase 1 vs Phase 2 with Phase 2 vs legacy, biasing the uplift.
        cohort_b = await cohort_pass_rate(
            db, UPLIFT_SUSTAIN_DAYS,
            variant_filter="IS NULL OR t.config->>'hypothesis_centric_variant' = '0'",
        )
        # Hypothesis lifecycle
        hypo_stats = await hypothesis_abandon_stats(db, UPLIFT_SUSTAIN_DAYS)
        # Cross-dataset ratio
        cross = await cross_dataset_ratio(db, UPLIFT_SUSTAIN_DAYS)
        # IQC marginal positive rate
        iqc = await iqc_marginal_positive_rate(db, UPLIFT_SUSTAIN_DAYS)

    # --- Compute trigger results ---
    uplift_pp = (cohort_a["pass_rate"] - cohort_b["pass_rate"]) * 100
    cohort_n_ok = (cohort_a["alpha_n"] >= COHORT_MIN_N and cohort_b["alpha_n"] >= COHORT_MIN_N)
    trigger_1 = cohort_n_ok and uplift_pp >= UPLIFT_PP_THRESHOLD

    abandon_rate = hypo_stats["abandon_rate"]
    abandon_rate_strict = hypo_stats["abandon_rate_strict"]
    abandon_total = hypo_stats["total"]
    trigger_2 = abandon_total >= COHORT_MIN_N and ABANDON_LO <= abandon_rate <= ABANDON_HI

    cross_ratio = cross["cross_ratio"]
    cross_total = cross["total_linked"]
    trigger_3 = cross_total >= COHORT_MIN_N and cross_ratio >= CROSS_DATASET_THRESHOLD

    iqc_rate = iqc["positive_rate"]
    iqc_n = iqc["audited_n"]
    # Regression check is open-ended for now — baseline = 0% Δscore-positive
    # rate of pk=7810 era (1/40). Just report; don't block.
    iqc_signal = iqc_n >= 10

    all_pass = trigger_1 and trigger_2 and trigger_3

    # --- Report ---
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    out_dir = Path("docs/phase3_readiness")
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"trigger_monitor_{today}.md"

    lines = [
        f"# Phase 3 trigger monitor — {today} UTC",
        "",
        f"Window: last {UPLIFT_SUSTAIN_DAYS} days.",
        "",
        f"**Overall verdict: {'✅ READY' if all_pass else '⏳ NOT YET'}**",
        "",
        "## Trigger 1 — Phase 2 A/B PASS rate uplift",
        "",
        f"| cohort | alpha_n | PASS | PASS rate |",
        f"|---|---|---|---|",
        f"| LEVEL=2 (Phase 2) | {cohort_a['alpha_n']} | {cohort_a['pass_n']} | {cohort_a['pass_rate']*100:.1f}% |",
        f"| LEVEL=0 (legacy) | {cohort_b['alpha_n']} | {cohort_b['pass_n']} | {cohort_b['pass_rate']*100:.1f}% |",
        f"",
        f"**Uplift: {uplift_pp:+.1f} pp** (threshold ≥ {UPLIFT_PP_THRESHOLD} pp)",
        f"Cohort sample sufficient: {emoji_for(cohort_n_ok)} (both ≥ {COHORT_MIN_N})",
        f"",
        f"Trigger 1: {emoji_for(trigger_1)}",
        "",
        "## Trigger 2 — Hypothesis abandon rate sanity",
        "",
        f"| status | count |",
        f"|---|---|",
    ]
    for s in ("PROPOSED", "ACTIVE", "PROMOTED", "ABANDONED", "SUPERSEDED"):
        lines.append(f"| {s} | {hypo_stats['by_status'].get(s, 0)} |")
    lines.extend([
        f"",
        f"**Retirement rate (ABANDONED + SUPERSEDED): "
        f"{abandon_rate*100:.1f}%** "
        f"(target range [{ABANDON_LO*100:.0f}%, {ABANDON_HI*100:.0f}%])",
        f"",
        f"  - ABANDONED only: {hypo_stats['abandoned_only']} "
        f"({abandon_rate_strict*100:.1f}%) — strict B6 path",
        f"  - SUPERSEDED only: {hypo_stats['superseded_only']} — replaced by "
        f"node_hypothesis upstream (functionally retired)",
        f"  - Retired total: {hypo_stats['retired_total']} / {abandon_total}",
        f"",
        f"Trigger 2: {emoji_for(trigger_2)}",
        "",
        "## Trigger 3 — Cross-dataset alpha ratio",
        "",
        f"Hypothesis-linked alphas in window: {cross_total}",
        f"Multi-dataset (hypothesis.dataset_pool ≥ 2): {cross['multi_dataset']}",
        f"**Cross-dataset ratio: {cross_ratio*100:.1f}%** (target ≥ {CROSS_DATASET_THRESHOLD*100:.0f}%)",
        f"",
        f"Trigger 3: {emoji_for(trigger_3)}",
        "",
        "## Trigger 4 — IQC marginal-positive rate (observational, non-gating)",
        "",
        f"Auto-audited alphas (V-22.12): {iqc_n}",
        f"+Δscore: {iqc['positive_n']}",
        f"Positive rate: {iqc_rate*100:.1f}%",
        (
            f"mean Δscore: {iqc['mean_delta_score']:+.1f}  "
            f"median: {iqc['median_delta_score']:+.1f}"
        ) if iqc.get("mean_delta_score") is not None else "Δscore: insufficient data",
        f"",
        f"Sufficient signal (n ≥ 10): {emoji_for(iqc_signal, gating=False)}",
        (
            "\n⚠ All audited alphas have Δscore ≤ 0 — the can_submit gate "
            "approves alphas that hurt the IQC portfolio. **Gate calibration "
            "is the real bottleneck**, not Phase 3."
        ) if iqc_n >= 10 and iqc["positive_n"] == 0 else "",
        "",
        "## Recommendation",
        "",
    ])

    if all_pass:
        lines.extend([
            "**All gating triggers PASS. Phase 3 ROI threshold cleared.**",
            "",
            "Suggested next steps:",
            "1. Review the Phase 3 design in plan v5+ (mining_tasks main-loop invert)",
            "2. Implement ~300 LOC across mining_tasks / mining_agent / workflow",
            "3. Roll out via `HYPOTHESIS_CENTRIC_CANDIDATE=3` for 50/50 A/B",
            "4. Re-run this monitor 2 weeks post-Phase 3 to validate",
        ])
    else:
        unmet = []
        if not trigger_1:
            unmet.append(
                f"- Trigger 1: uplift {uplift_pp:+.1f} pp < {UPLIFT_PP_THRESHOLD} pp "
                f"(or cohort_n insufficient: A={cohort_a['alpha_n']}, B={cohort_b['alpha_n']})"
            )
        if not trigger_2:
            unmet.append(
                f"- Trigger 2: retirement rate {abandon_rate*100:.1f}% "
                f"(ABANDONED={hypo_stats['abandoned_only']} "
                f"+ SUPERSEDED={hypo_stats['superseded_only']}) outside "
                f"[{ABANDON_LO*100:.0f}%, {ABANDON_HI*100:.0f}%] "
                f"(or n={hypo_stats['total']} < {COHORT_MIN_N})"
            )
        if not trigger_3:
            unmet.append(
                f"- Trigger 3: cross-dataset ratio {cross_ratio*100:.1f}% < "
                f"{CROSS_DATASET_THRESHOLD*100:.0f}% (or n={cross_total} < {COHORT_MIN_N})"
            )
        lines.extend([
            f"**{len(unmet)} of 3 gating triggers NOT met. Phase 3 deferred.**",
            "",
        ])
        lines.extend(unmet)
        lines.extend([
            "",
            "Re-run this monitor weekly. When all 3 gating triggers PASS for two ",
            "consecutive weeks, escalate to Phase 3 implementation review.",
        ])

    md_path.write_text("\n".join(lines), encoding="utf-8")

    # Console summary
    print(f"Trigger 1 (PASS uplift):       {emoji_for(trigger_1)}  {uplift_pp:+.1f} pp (A:{cohort_a['alpha_n']}, B:{cohort_b['alpha_n']})")
    print(
        f"Trigger 2 (retirement rate):    {emoji_for(trigger_2)}  "
        f"{abandon_rate*100:.1f}% "
        f"(ABANDONED={hypo_stats['abandoned_only']} + "
        f"SUPERSEDED={hypo_stats['superseded_only']}, n={hypo_stats['total']})"
    )
    print(f"Trigger 3 (cross-dataset):      {emoji_for(trigger_3)}  {cross_ratio*100:.1f}% (n={cross_total})")
    iqc_delta_str = (
        f"mean={iqc['mean_delta_score']:+.0f} median={iqc['median_delta_score']:+.0f}"
        if iqc.get("mean_delta_score") is not None else "no data"
    )
    print(
        f"Trigger 4 (IQC +Δscore, obs.):  {emoji_for(iqc_signal, gating=False)}  "
        f"{iqc_rate*100:.1f}% (n={iqc_n}, {iqc_delta_str})"
    )
    if iqc_n >= 10 and iqc["positive_n"] == 0:
        print(
            "  ⚠ can_submit gate ≠ IQC value-add — gate approves alphas that "
            "hurt the portfolio. Re-calibration > Phase 3."
        )
    print()
    print(f"Overall: {'✅ Phase 3 READY' if all_pass else '⏳ Phase 3 NOT YET'}")
    print(f"Report: {md_path}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
