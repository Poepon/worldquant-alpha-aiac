"""Layer 1 — portfolio theme distribution audit.

Classifies every alpha expression into one of ~15 financial themes via
regex pattern matching, then reports distribution across three cohorts:

  1. Submitted (BRAIN-side competition reality)
  2. can_submit=True (mining-intent: what we think is submittable)
  3. All PASS (broadest mining output)

Output: docs/portfolio_theme_distribution_<date>.md — per-cohort breakdown
of counts, percentages, mean sharpe/fitness/turnover. The deltas between
cohorts reveal where mining is over- vs under-investing relative to what
actually wins on the competition side.

No BRAIN calls; pure DB read. <1 min runtime.
"""
from __future__ import annotations

import asyncio
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from backend.database import AsyncSessionLocal


# ---------------------------------------------------------------------------
# Theme classification rules
# ---------------------------------------------------------------------------
# Order matters: first match wins. Most specific patterns first so
# generic catch-alls don't shadow them.

THEMES: list[tuple[str, list[str]]] = [
    # ── multi-field composites (V-22.6 family) ──
    ("intraday_return", [
        r"divide\s*\(\s*subtract\s*\(\s*close\s*,\s*open\s*\)\s*,\s*open\s*\)",
    ]),
    ("intraday_range", [
        r"divide\s*\(\s*subtract\s*\(\s*high\s*,\s*low\s*\)\s*,\s*close\s*\)",
    ]),
    ("close_in_range", [
        r"divide\s*\(\s*subtract\s*\(\s*close\s*,\s*low\s*\)\s*,\s*subtract\s*\(\s*high\s*,\s*low\s*\)\s*\)",
    ]),
    ("overnight_gap", [
        r"divide\s*\(\s*subtract\s*\(\s*open\s*,\s*ts_delay\s*\(\s*close",
        r"subtract\s*\(\s*divide\s*\(\s*close\s*,\s*ts_delay\s*\(\s*close",
    ]),
    ("close_vwap_gap", [
        r"subtract\s*\(\s*close\s*,\s*vwap\s*\)",
        r"divide\s*\(\s*subtract\s*\(\s*close\s*,\s*vwap\s*\)\s*,\s*vwap",
    ]),
    ("pe_synth", [
        r"divide\s*\(\s*close\s*,\s*eps\b",
        r"divide\s*\(\s*close\s*,\s*book_value",
    ]),
    ("earnings_yield", [
        r"divide\s*\(\s*ebit\s*,\s*(enterprise_value|ev\b)",
    ]),
    ("book_to_market", [
        r"divide\s*\(\s*book_value_per_share[a-z_0-9]*\s*,\s*close",
    ]),
    ("accrual_quality", [
        r"divide\s*\(\s*cash_flow_from_operations\s*,\s*net_income",
        r"divide\s*\(\s*cfo\s*,\s*net_income",
    ]),
    ("cfo_yield", [
        r"divide\s*\(\s*(cash_flow_from_operations|cfo)\s*,\s*cap\b",
    ]),
    ("asset_turnover", [
        r"divide\s*\(\s*revenue\s*,\s*fnd6_newa1v1300_at",
        r"divide\s*\(\s*sales\s*,\s*total_assets",
    ]),
    ("leverage_ratio", [
        r"divide\s*\(\s*(debt_lt|total_debt)\s*,\s*(fnd6_teq|total_equity|shareholders_equity)",
    ]),
    ("volume_per_cap", [
        r"divide\s*\(\s*volume\s*,\s*cap\b",
        r"divide\s*\(\s*amount\s*,\s*cap\b",
    ]),
    ("vol_per_price", [
        r"divide\s*\(\s*volume\s*,\s*close\b",
    ]),

    # ── single-field families ──
    # V2 (2026-05-12): match returns anywhere inside a sign-flip OR group wrapper.
    # Layer 1 first audit missed ~70 alphas with shapes like
    # `multiply(-1, ts_decay_linear(ts_rank(returns, 5), 4))` because the old
    # regex required `returns` to be the immediate first arg of an op INSIDE
    # multiply(-1, ...). Now: detect signed sign + `returns` anywhere in expr.
    ("returns_reversal", [
        # multiply(-1, ...) AND returns appears somewhere in the inner
        r"multiply\s*\(\s*-1\s*,[^\)]*\breturns\b",
        # subtract(0, X(returns,...)) negation form
        r"^subtract\s*\(\s*0\s*,[^\)]*\breturns\b",
    ]),
    ("returns_momentum", [
        # ts_*(returns, *) without negation prefix
        r"^[a-zA-Z_]+\s*\(\s*returns\s*,",
    ]),
    # V2 (2026-05-12) — size factor. cap-based signals are 51-strong in
    # current mining output. Examples:
    #   multiply(-1, ts_zscore(cap, 5))
    #   ts_arg_max(cap, 5)
    #   group_neutralize(multiply(-1, ts_zscore(cap, 5)), industry)
    ("size_cap", [
        r"[a-zA-Z_]+\s*\(\s*cap\s*,\s*\d",
        r"multiply\s*\(\s*-1\s*,[^\)]*\bcap\s*,",
    ]),
    # V2 (2026-05-12) — BRAIN proprietary rank-derivative scores:
    # earnings_certainty / growth_potential / analyst_revision /
    # relative_valuation / profitability / etc. _rank_derivative suffix.
    ("rank_derivative_score", [
        r"\b[a-z_]+_rank_derivative\b",
    ]),
    # V2 (2026-05-12) — option-implied signals. Distinct family from
    # historical/implied volatility (which already has a theme).
    ("option_signal", [
        r"\bforward_price_\d+",
        r"\b(call|put)_breakeven_\d+",
        r"\b(call|put)_volume_\d+",
        r"\boption_(volume|skew|skewness|iv_)",
    ]),
    # V2 (2026-05-12) — T2 group-wrapper outer indicator. Catches T2 alphas
    # whose inner isn't in the explicit theme catalog. Generic and broad —
    # placed late so more specific themes win.
    ("t2_group_wrapped", [
        r"^group_(neutralize|zscore|rank|scale|normalize|demean)\s*\(",
    ]),
    # V2 (2026-05-12) — cap-weighted within-group residualize:
    # `subtract(X, group_mean(X, cap, group))` shape.
    ("cap_residualize", [
        r"^subtract\s*\([^,]+,\s*group_mean\s*\(",
    ]),
    ("volatility_signal", [
        r"\bts_std_dev\b",
        r"\bhistorical_volatility",
        r"\bimplied_volatility",
    ]),
    ("price_momentum", [
        r"^[a-zA-Z_]+\s*\(\s*(close|vwap)\s*,",
        r"multiply\s*\(\s*-1\s*,\s*[a-zA-Z_]+\s*\(\s*(close|vwap)\b",
    ]),
    ("volume_signal", [
        r"^[a-zA-Z_]+\s*\(\s*(volume|adv\d+|amount|shares)\s*,",
    ]),
    ("sentiment", [
        r"\bsnt\d?_",
        r"\bnws\d+_",
        r"\bsocialmedia",
        r"\brp_css_",
    ]),
    ("analyst", [
        r"\banl\d+_",
        r"\bfam_",
        r"\best\d+_",
        r"_recom",
        r"\beps_actual",
    ]),
    ("factor_composite", [
        r"\bfscore_",
        r"\bmdl\d+_",
        r"\bcomposite_factor",
        r"_score_",
    ]),
    ("fundamental_other", [
        # fundamental fields not caught by specific composites
        r"\bfnd\d+_",
        r"\bfn_",
        r"\bcapex\b",
        r"\bcashflow",
        r"\breturn_(equity|assets|invested)",
        r"\bret_on_(invested|equity)",
        r"\benterprise_value\b",
        r"\bebit(da)?\b",
        r"\beps\b",
        # V2 (2026-05-12): additional FND tokens surfaced from 'other' bucket
        r"\bcurrent_ratio\b",
        r"\bbookvalue_ps\b",
        r"\bdepre_amort\b",
        r"\b(actual|news)_eps_actual",
        r"\bactual_eps_value_quarterly\b",
        r"\bindustry_rel_ttm_",
    ]),
    ("event_driven", [
        r"\btrade_when\b",
        r"days_to_announcement",
        r"earn_(date|surp|announce)",
    ]),
]
OTHER_THEME = "other"


def classify(expression: str) -> str:
    """Return the first theme whose regex matches the expression."""
    s = expression or ""
    for theme, patterns in THEMES:
        for pat in patterns:
            if re.search(pat, s):
                return theme
    return OTHER_THEME


# ---------------------------------------------------------------------------
# Cohort queries
# ---------------------------------------------------------------------------

COHORTS = {
    "submitted":   "WHERE date_submitted IS NOT NULL",
    "can_submit":  "WHERE can_submit = TRUE",
    "all_pass":    "WHERE quality_status IN ('PASS','PASS_PROVISIONAL')",
}


async def fetch_cohort(db, where_clause: str) -> list[dict]:
    sql = text(f"""
        SELECT id, alpha_id, expression, is_sharpe, is_fitness, is_turnover,
               can_submit, date_submitted, quality_status
        FROM alphas {where_clause}
    """)
    result = await db.execute(sql)
    return [dict(r._mapping) for r in result]


def aggregate(rows: list[dict]) -> dict:
    """Compute per-theme stats."""
    by_theme: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_theme[classify(r["expression"])].append(r)

    n_total = len(rows)
    stats = {}
    for theme, group in by_theme.items():
        sharpes = [r["is_sharpe"] for r in group if isinstance(r["is_sharpe"], (int, float))]
        fits = [r["is_fitness"] for r in group if isinstance(r["is_fitness"], (int, float))]
        tos = [r["is_turnover"] for r in group if isinstance(r["is_turnover"], (int, float))]
        stats[theme] = {
            "count": len(group),
            "pct": 100 * len(group) / n_total if n_total else 0,
            "mean_sharpe": sum(sharpes) / len(sharpes) if sharpes else None,
            "mean_fitness": sum(fits) / len(fits) if fits else None,
            "mean_turnover": sum(tos) / len(tos) if tos else None,
        }
    return {"n": n_total, "themes": stats}


def fmt_pct(v):
    return f"{v:5.1f}%" if isinstance(v, (int, float)) else "—"


def fmt_num(v, prec=2):
    return f"{v:.{prec}f}" if isinstance(v, (int, float)) else "—"


def saturation_flag(pct: float) -> str:
    if pct >= 30:
        return "🔴 SATURATED"
    if pct >= 15:
        return "🟠 dense"
    if pct >= 5:
        return "🟢 ok"
    if pct >= 1:
        return "🔵 sparse"
    return "⚪ absent"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    print("=== Portfolio theme distribution audit ===\n")
    cohort_stats: dict[str, dict] = {}
    async with AsyncSessionLocal() as db:
        for name, where in COHORTS.items():
            rows = await fetch_cohort(db, where)
            cohort_stats[name] = aggregate(rows)
            print(f"  cohort '{name}': {cohort_stats[name]['n']} alphas")

    # Union of all themes seen across cohorts (in declaration order + 'other')
    seen = set()
    for cs in cohort_stats.values():
        seen.update(cs["themes"].keys())
    ordered_themes = [t for t, _ in THEMES if t in seen] + (
        [OTHER_THEME] if OTHER_THEME in seen else []
    )

    # Markdown report
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    out_dir = Path("docs/portfolio_theme")
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"distribution_{today}.md"

    lines = [
        f"# Portfolio theme distribution — {today} UTC",
        "",
        "Classifies every alpha expression into a financial theme via regex.",
        "Three cohorts compared: SUBMITTED (BRAIN-side reality) vs CAN_SUBMIT",
        "(mining-intent ready-pool) vs ALL_PASS (broadest mining output).",
        "",
        "## Cohort sizes",
        "",
    ]
    for n, cs in cohort_stats.items():
        lines.append(f"- **{n}**: {cs['n']} alphas")
    lines.append("")

    lines.append("## Theme distribution by cohort")
    lines.append("")
    lines.append("| theme | submitted (n / %) | can_submit (n / %) | all_pass (n / %) | flag (all_pass) |")
    lines.append("|---|---|---|---|---|")
    for theme in ordered_themes:
        cells = []
        for cohort in COHORTS:
            s = cohort_stats[cohort]["themes"].get(theme)
            if s:
                cells.append(f"{s['count']} / {fmt_pct(s['pct'])}")
            else:
                cells.append("0 / 0.0%")
        ap = cohort_stats["all_pass"]["themes"].get(theme, {"pct": 0})
        lines.append(f"| `{theme}` | {cells[0]} | {cells[1]} | {cells[2]} | {saturation_flag(ap['pct'])} |")
    lines.append("")

    lines.append("## Per-theme sharpe / fitness / turnover (cohort=all_pass)")
    lines.append("")
    lines.append("| theme | n | mean sharpe | mean fitness | mean turnover |")
    lines.append("|---|---|---|---|---|")
    ap_themes = cohort_stats["all_pass"]["themes"]
    sorted_themes = sorted(ap_themes.items(), key=lambda kv: -kv[1]["count"])
    for theme, s in sorted_themes:
        lines.append(
            f"| `{theme}` | {s['count']} | "
            f"{fmt_num(s['mean_sharpe'])} | "
            f"{fmt_num(s['mean_fitness'])} | "
            f"{fmt_num(s['mean_turnover'])} |"
        )
    lines.append("")

    # Action implications
    lines.append("## Key signals")
    lines.append("")

    # Saturation: themes with >15% of all_pass
    saturated_themes = [t for t, s in ap_themes.items() if s["pct"] >= 15]
    lines.append("### Crowded themes (≥15% of all_pass cohort)")
    if saturated_themes:
        for t in saturated_themes:
            s = ap_themes[t]
            lines.append(f"- **`{t}`**: {s['count']} alphas ({fmt_pct(s['pct'])}) — mining is over-investing here")
    else:
        lines.append("- (none above 15%)")
    lines.append("")

    # Absent: V-22.6 composite themes with 0 or <1% in all_pass
    V22_THEMES = {"intraday_return", "intraday_range", "close_in_range", "overnight_gap",
                  "close_vwap_gap", "pe_synth", "earnings_yield", "book_to_market",
                  "accrual_quality", "cfo_yield", "asset_turnover", "leverage_ratio",
                  "volume_per_cap", "vol_per_price"}
    lines.append("### V-22.6 composite themes with sparse coverage (<5% of all_pass)")
    sparse_v22 = [
        (t, ap_themes.get(t, {"count": 0, "pct": 0}))
        for t in V22_THEMES
        if ap_themes.get(t, {"count": 0})["count"] < 5
        or ap_themes.get(t, {"pct": 0})["pct"] < 5
    ]
    for t, s in sparse_v22:
        lines.append(f"- `{t}`: {s.get('count', 0)} alphas ({fmt_pct(s.get('pct', 0))}) — unexplored / under-mined")
    lines.append("")

    # Submitted vs can_submit delta — what we kept vs what we have ready
    lines.append("### Submitted vs can_submit gap (% of cohort)")
    lines.append("")
    lines.append("| theme | submitted % | can_submit % | gap |")
    lines.append("|---|---|---|---|")
    for theme in ordered_themes:
        sub_pct = cohort_stats["submitted"]["themes"].get(theme, {"pct": 0})["pct"]
        cs_pct = cohort_stats["can_submit"]["themes"].get(theme, {"pct": 0})["pct"]
        gap = sub_pct - cs_pct
        if abs(gap) >= 5:
            arrow = "⬆" if gap > 0 else "⬇"
            lines.append(f"| `{theme}` | {fmt_pct(sub_pct)} | {fmt_pct(cs_pct)} | {arrow} {gap:+.1f} pp |")
    lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")

    # Console summary
    print()
    print("=== Top themes per cohort ===")
    for cname, cs in cohort_stats.items():
        top = sorted(cs["themes"].items(), key=lambda kv: -kv[1]["count"])[:5]
        print(f"  {cname} (n={cs['n']}):")
        for theme, s in top:
            print(f"    {theme:25s}  {s['count']:>4d}  {fmt_pct(s['pct'])}  sh={fmt_num(s['mean_sharpe'])}")

    print(f"\nReport: {md_path}")


if __name__ == "__main__":
    asyncio.run(main())
