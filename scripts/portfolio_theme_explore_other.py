"""Inspect the 'other' bucket from Layer 1 theme audit.

Layer 1 left 38% of all_pass alphas unclassified. This script:
  1. Pulls every alpha that classify() returns OTHER on
  2. Extracts structural signatures (top-level op, fields, depth)
  3. Groups by signature and reports the largest sub-themes
  4. For each major sub-theme, shows 3 sample expressions + mean sharpe

Output: docs/portfolio_theme/other_bucket_breakdown_<date>.md

Run AFTER portfolio_theme_audit.py — its output names the cohorts.
This script uses the same THEMES list from portfolio_theme_audit so the
classify() result stays in sync.
"""
from __future__ import annotations

import asyncio
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from backend.database import AsyncSessionLocal
from scripts.portfolio_theme_audit import classify, OTHER_THEME


# ---------------------------------------------------------------------------
# Signature extraction
# ---------------------------------------------------------------------------

_TOP_OP_RE = re.compile(r"^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\(")
_IDENT_RE = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b")

# Operator names — non-field tokens that aren't actual fields
_OP_TOKENS = {
    # arithmetic
    "add", "subtract", "multiply", "divide", "signed_power", "abs", "min", "max",
    # cross-sectional
    "rank", "zscore", "normalize", "quantile", "winsorize", "scale", "scale_down",
    "regression_neut", "vector_neut",
    # time series
    "ts_rank", "ts_zscore", "ts_mean", "ts_std_dev", "ts_delta", "ts_delay",
    "ts_decay_linear", "ts_decay_exp_window", "ts_arg_max", "ts_arg_min",
    "ts_quantile", "ts_sum", "ts_corr", "ts_av_diff", "ts_count_nans",
    "ts_product", "ts_scale", "ts_step", "ts_regression", "ts_covariance",
    "ts_backfill", "ts_max", "ts_min", "ts_median", "ts_skewness", "ts_kurtosis",
    "ts_returns",
    # group
    "group_neutralize", "group_rank", "group_zscore", "group_mean", "group_scale",
    "group_demean", "group_normalize",
    # vec
    "vec_avg", "vec_sum", "vec_max", "vec_min", "vec_l2_norm", "vec_count",
    "vec_median", "vec_std_dev", "vec_range", "vec_stddev", "vec_powersum",
    "vec_choose", "vec_ir",
    # event
    "trade_when", "less", "greater", "equal", "if_else",
}

_GROUP_TOKENS = {"industry", "sector", "subindustry", "market", "exchange", "country"}
_KEYWORDS = {"true", "false", "nan", "inf", "std", "filter"}


def top_op(expr: str) -> str | None:
    m = _TOP_OP_RE.match(expr or "")
    return m.group(1) if m else None


def field_tokens(expr: str) -> list[str]:
    """Extract all token identifiers in expression, excluding ops/groups/keywords."""
    if not expr:
        return []
    out = []
    for m in _IDENT_RE.finditer(expr):
        tok = m.group(1).lower()
        if tok in _OP_TOKENS or tok in _GROUP_TOKENS or tok in _KEYWORDS:
            continue
        # skip pure numbers (regex catches starts-with-letter so usually safe)
        out.append(tok)
    return out


def signature(expr: str) -> tuple[str | None, tuple[str, ...]]:
    """A coarse structural signature: (top_op, sorted unique field tokens)."""
    return (top_op(expr), tuple(sorted(set(field_tokens(expr)))))


# Field-prefix grouping for compact display
def field_family(field: str) -> str:
    """Map a field name to a coarse family by prefix."""
    f = field.lower()
    if f in {"close", "open", "high", "low", "vwap", "volume", "amount", "cap",
             "returns", "shares", "open_interest", "adv20", "adv60", "adv120"}:
        return "PV"
    if f.startswith(("fnd", "fn_")) or f in {"eps", "ebit", "revenue", "sales",
                                              "cfo", "cashflow_dividends", "capex"}:
        return "FND"
    if f.startswith(("anl", "fam_", "est")) or "_recom" in f or "_eps_actual" in f or "_target" in f:
        return "ANL"
    if f.startswith(("snt", "nws", "rp_css_", "socialmedia")):
        return "SNT"
    if f.startswith(("mdl", "model")) or "score" in f or "factor" in f or "fscore_" in f:
        return "MDL"
    if f.startswith(("opt", "option")) or "implied_volatility" in f or "iv_" in f:
        return "OPT"
    if f.startswith("group_"):
        return "GRP"
    if "correlation_last" in f or "historical_volatility" in f or "beta" in f:
        return "RISK"
    return "OTHER"


async def main() -> None:
    print("=== Exploring 'other' bucket from theme classifier ===\n")

    async with AsyncSessionLocal() as db:
        sql = text("""
            SELECT id, alpha_id, expression, is_sharpe, is_turnover, is_fitness,
                   quality_status, factor_tier
            FROM alphas
            WHERE quality_status IN ('PASS','PASS_PROVISIONAL')
        """)
        rows = [dict(r._mapping) for r in await db.execute(sql)]

    others = [r for r in rows if classify(r["expression"] or "") == OTHER_THEME]
    print(f"Total all_pass alphas: {len(rows)}")
    print(f"Unclassified ('other'): {len(others)} ({100 * len(others)/max(1,len(rows)):.1f}%)")

    # ---- Group by top_op
    by_op = defaultdict(list)
    for r in others:
        by_op[top_op(r["expression"]) or "?"].append(r)
    op_freq = sorted(by_op.items(), key=lambda kv: -len(kv[1]))

    # ---- Field-family co-occurrence
    family_pair_count: Counter = Counter()
    family_single_count: Counter = Counter()
    for r in others:
        fams = set(field_family(f) for f in field_tokens(r["expression"]))
        fams.discard("OTHER")  # too noisy
        if len(fams) == 1:
            family_single_count[next(iter(fams))] += 1
        else:
            for f in fams:
                family_single_count[f] += 1
            # store sorted pair
            for f1 in sorted(fams):
                for f2 in sorted(fams):
                    if f1 < f2:
                        family_pair_count[(f1, f2)] += 1

    # ---- Field token frequency
    token_freq: Counter = Counter()
    for r in others:
        for t in set(field_tokens(r["expression"])):
            token_freq[t] += 1
    top_tokens = token_freq.most_common(30)

    # ---- Build report
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    out_dir = Path("docs/portfolio_theme")
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"other_bucket_breakdown_{today}.md"

    lines = [
        f"# 'other' bucket breakdown — {today} UTC",
        "",
        f"Layer 1 classifier (`scripts/portfolio_theme_audit.py`) left **{len(others)}**",
        f"of {len(rows)} all_pass alphas in `other`. This report decomposes them",
        "by top-level op + field-family signatures to surface hidden sub-themes.",
        "",
        "## Top-level operator distribution (others)",
        "",
        "| top_op | count | % of others | mean sharpe |",
        "|---|---|---|---|",
    ]
    for op, group in op_freq[:20]:
        shs = [r["is_sharpe"] for r in group if isinstance(r["is_sharpe"], (int, float))]
        msh = sum(shs) / len(shs) if shs else None
        lines.append(
            f"| `{op}` | {len(group)} | {100*len(group)/len(others):.1f}% | "
            f"{(f'{msh:.2f}' if msh is not None else '—')} |"
        )
    lines.append("")

    lines.append("## Field-family signature distribution (others)")
    lines.append("")
    lines.append("Family codes: PV (price-volume), FND (fundamental), ANL (analyst),")
    lines.append("SNT (sentiment/news), MDL (factor/model composite), OPT (option/IV),")
    lines.append("RISK (correlation/volatility), GRP (group built-in).")
    lines.append("")
    lines.append("| family | count | mean sharpe |")
    lines.append("|---|---|---|")
    for fam, cnt in family_single_count.most_common():
        shs = [
            r["is_sharpe"] for r in others
            if isinstance(r["is_sharpe"], (int, float))
            and fam in set(field_family(f) for f in field_tokens(r["expression"]))
        ]
        msh = sum(shs) / len(shs) if shs else None
        lines.append(
            f"| `{fam}` | {cnt} | {(f'{msh:.2f}' if msh is not None else '—')} |"
        )
    lines.append("")

    # Sample expressions per major op/family combo
    lines.append("## Sample expressions (per top_op, first 5 with highest sharpe)")
    lines.append("")
    for op, group in op_freq[:10]:
        # sort by sharpe desc
        sorted_group = sorted(
            (g for g in group if isinstance(g["is_sharpe"], (int, float))),
            key=lambda g: -g["is_sharpe"],
        )
        lines.append(f"### `{op}` ({len(group)} alphas)")
        lines.append("")
        for g in sorted_group[:5]:
            expr = (g["expression"] or "")[:200]
            sh = g["is_sharpe"]
            to = g["is_turnover"]
            sh_str = f"{sh:.2f}" if isinstance(sh, (int, float)) else "—"
            to_str = f"{to:.2f}" if isinstance(to, (int, float)) else "—"
            lines.append(f"- pk={g['id']}  sh={sh_str}  to={to_str}  `{expr}`")
        lines.append("")

    lines.append("## Top field tokens used (others)")
    lines.append("")
    lines.append("| field | usage count | family |")
    lines.append("|---|---|---|")
    for tok, cnt in top_tokens:
        lines.append(f"| `{tok}` | {cnt} | {field_family(tok)} |")
    lines.append("")

    # Recommendation hints
    lines.append("## Suggested classifier additions")
    lines.append("")
    lines.append("Looking at top_op + field family signatures, propose new themes for:")
    for op, group in op_freq[:10]:
        if len(group) < 5:
            break
        fams = Counter()
        for r in group:
            for f in set(field_family(t) for t in field_tokens(r["expression"])):
                fams[f] += 1
        top_fam = fams.most_common(2)
        fam_str = " + ".join(f"{f}({c})" for f, c in top_fam)
        lines.append(f"- `{op}` with families {fam_str}  → consider new theme")
    lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")

    # Console summary
    print(f"\nTop 10 top_op in 'other':")
    for op, group in op_freq[:10]:
        shs = [r["is_sharpe"] for r in group if isinstance(r["is_sharpe"], (int, float))]
        msh = sum(shs) / len(shs) if shs else None
        msh_s = f"{msh:.2f}" if msh is not None else "—"
        print(f"  {op:25s}  {len(group):>4d}  sh={msh_s}")
    print(f"\nTop 10 field tokens in 'other':")
    for tok, cnt in top_tokens[:10]:
        print(f"  {tok:30s}  {cnt:>4d}  [{field_family(tok)}]")
    print(f"\nReport: {md_path}")


if __name__ == "__main__":
    asyncio.run(main())
