"""Region field adapter — map Plan v5+ aliases to real BRAIN field names.

Plan §R7-2 (2026-05-03 implementation):

Quasi-T1 whitelist (factor_tier_classifier._QUASI_T1_PATTERNS) and Golden
Set v0.1 (hypothesis_golden_set_v01_draft.json) reference fields by
academic-literature aliases like `eps`, `ebit`, `total_assets`, `cfo`.
BRAIN's USA/TOP3000 dataset uses Compustat-style real names like
`fnd6_newa1v1300_at` (total assets) and `fnd6_newa2v1300_oiadp` (EBIT).

Without this adapter, alias-form expressions never match real fields and
seed-pool expansion produces 0 candidates. R7-1 audit found 9 missing
aliases on USA/TOP3000.

Mappings come in three flavors:
  1. Direct rename:   "total_assets" → "fnd6_newa1v1300_at"
  2. Synthesis:       "ev" → "subtract(add(cap, fnd6_..._dltt), fnd6_..._che)"
  3. Unsupported:     "open_interest" → None  (drop hypothesis on this region)

Adapter is region-keyed; CHN/EUR/ASI/GLB tables empty until R7 expands.

Public API:
    adapt_expression(expr_template, region) -> Optional[str]
    is_alias_supported(alias, region) -> bool
    get_alias_real_name(alias, region) -> Optional[str]   (None if synthesized)
"""
from __future__ import annotations

import re
from typing import Dict, Optional


# =============================================================================
# Alias → real name mappings (USA/TOP3000)
# =============================================================================
# Three categories:
#   - Direct rename (string)
#   - Synthesized expression (BRAIN-syntax string built from real fields)
#   - Unsupported (None) — caller drops the hypothesis for this region

# Direct alias → real field on fundamental6 (Compustat). Verified against
# docs/datafields_snapshot_v1.md and a sample of fnd6_newa1v1300_* /
# fnd6_newa2v1300_* fields present in USA/TOP3000.
_USA_DIRECT: Dict[str, str] = {
    # Universal PV / market data (already present in pv1; aliases unchanged)
    "close": "close",
    "open": "open",
    "high": "high",
    "low": "low",
    "volume": "volume",
    "vwap": "vwap",
    "returns": "returns",
    "cap": "cap",
    "industry": "industry",     # group token, not a field
    "subindustry": "subindustry",
    "sector": "sector",
    "market": "market",
    # Fundamental — Compustat naming on fnd6_newa1v1300_* / fnd6_newa2v1300_*
    "total_assets": "fnd6_newa1v1300_at",
    "total_equity": "fnd6_newa1v1300_ceq",
    "net_income": "fnd6_newa2v1300_ni",
    "sales": "fnd6_newa2v1300_revt",
    "revenue": "fnd6_newa2v1300_revt",
    "eps": "fnd6_newa1v1300_epspi",
    "shares": "fnd6_newa1v1300_csho",
    "ebit": "fnd6_newa2v1300_oiadp",
    "working_capital": "fnd6_newa2v1300_wcap",
    "long_term_debt": "fnd6_newa1v1300_dltt",
    "short_term_debt": "fnd6_newa1v1300_dlc",
    "cash": "fnd6_newa1v1300_che",
    "common_dividends": "fnd6_newa1v1300_dvc",
    "interest_expense": "fnd6_newa2v1300_xint",
    "pretax_income": "fnd6_newa2v1300_pi",
    # Analyst data
    "analyst_eps_value": "anl4_af_eps_value",
    "analyst_eps_count": "anl4_afv4_eps_number",
    "analyst_eps_mean": "anl4_afv4_eps_mean",
    "analyst_cfo": "anl4_cfo_value",
    "actual_eps": "actual_eps_value_quarterly",
    "actual_sales": "actual_sales_value_quarterly",
    "actual_cfops": "actual_cashflow_per_share_value_quarterly",
    # Quality scores (model16 dataset)
    "fscore_quality": "fscore_quality",
    "fscore_total": "fscore_total",
    "fscore_momentum": "fscore_momentum",
    "fscore_value": "fscore_bfl_value",
    # Convenience: cfo via analyst (with caveat — preferred when fundamental
    # CFO/OANCF not directly available)
    "cfo": "anl4_cfo_value",
}


# Synthesized aliases — built from real fields/aliases. Resolution is recursive:
# the synthesized string itself can reference other aliases (resolved later).
_USA_SYNTHESIZED: Dict[str, str] = {
    # Total debt = short-term + long-term debt
    "total_debt": "add(short_term_debt, long_term_debt)",
    # Book value per share = total_equity / shares
    "book_value_per_share": "divide(total_equity, shares)",
    # Enterprise value = market cap + long-term debt - cash
    # (rough; ignores preferred stock + minority interest, fine as factor proxy)
    "ev": "subtract(add(cap, long_term_debt), cash)",
    # Dollar amount = price × volume
    "amount": "multiply(close, volume)",
    # EPS quarterly fallback (when fnd6_..._epspi unavailable)
    "eps_quarterly": "actual_eps",
    # Dividend per share
    "dividend_per_share": "divide(common_dividends, shares)",
}


# Aliases not supportable on USA/TOP3000 (caller drops hypothesis on this region)
_USA_UNSUPPORTED: set = {
    "open_interest",        # option-only data, not in pv1 here
    "mkt_returns",          # synthetic aggregate — needs group_mean construction
    "industry_returns",     # same as above
}


_REGION_TABLES: Dict[str, Dict] = {
    "USA": {
        "direct": _USA_DIRECT,
        "synthesized": _USA_SYNTHESIZED,
        "unsupported": _USA_UNSUPPORTED,
    },
    # CHN/EUR/ASI/GLB intentionally empty until R7 expands — hypothesis pool
    # construction will skip non-USA regions for v1.0.
}


_IDENT_RE = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b")


def get_alias_real_name(alias: str, region: str = "USA") -> Optional[str]:
    """Direct lookup only — returns the real BRAIN field name for an alias,
    or None if the alias is synthesized / unsupported / unknown."""
    table = _REGION_TABLES.get(region)
    if not table:
        return None
    return table["direct"].get(alias)


def is_alias_supported(alias: str, region: str = "USA") -> bool:
    """True if the alias is either directly mapped or synthesizable on region."""
    table = _REGION_TABLES.get(region)
    if not table:
        return False
    if alias in table["unsupported"]:
        return False
    return alias in table["direct"] or alias in table["synthesized"]


def adapt_expression(
    expr_template: str,
    region: str = "USA",
    *,
    max_depth: int = 5,
) -> Optional[str]:
    """Substitute alias tokens in an expression template with real BRAIN names.

    Args:
        expr_template: Plan-style expression using aliases
                       (e.g. "divide(close, eps)").
        region: BRAIN region key (USA/CHN/EUR/ASI/GLB).
        max_depth: Max recursion depth for synthesized expansions.

    Returns:
        Real-name expression usable on BRAIN, or None if any required alias
        is unsupported on this region. Numeric literals, operators, and
        already-real names pass through unchanged.

    Recursion: synthesized expansions can themselves reference aliases
    (e.g. total_debt → "add(short_term_debt, long_term_debt)" → both
    short_term_debt and long_term_debt are direct aliases). max_depth
    guards against accidental cycles.
    """
    table = _REGION_TABLES.get(region)
    if not table:
        return None

    direct = table["direct"]
    synthesized = table["synthesized"]
    unsupported = table["unsupported"]

    # Operator names from the canonical T2/Quasi-T1 sets — we don't try to
    # discover them from DB here; they're lower-case identifiers in the DB
    # and won't collide with field aliases (which are also lower-case but
    # don't appear in the operator table).
    KNOWN_OPS = {
        "add", "subtract", "multiply", "divide", "signed_power",
        "rank", "zscore", "normalize", "quantile", "winsorize", "scale",
        "ts_rank", "ts_zscore", "ts_mean", "ts_std_dev", "ts_delay",
        "ts_delta", "ts_sum", "ts_corr", "ts_decay_linear",
        "ts_arg_max", "ts_arg_min", "ts_av_diff", "ts_count_nans",
        "ts_product", "ts_scale", "ts_step", "ts_regression",
        "ts_covariance", "ts_backfill", "ts_quantile", "ts_skewness",
        "ts_kurtosis", "ts_median", "ts_returns",
        "group_neutralize", "group_rank", "group_zscore",
        "group_mean", "group_scale",
        "trade_when",
        "less", "greater", "if_else", "equal", "abs", "min", "max",
        "and", "or", "not",
        # Event/utility ops surfaced by Golden Set entries
        "days_from_last_change", "bucket", "hump", "inverse", "sign",
        "sqrt", "log", "power",
    }

    def _expand_token(tok: str, depth: int) -> Optional[str]:
        if depth > max_depth:
            return None
        # Numeric literal — pass through
        if re.fullmatch(r"-?\d+(\.\d+)?", tok):
            return tok
        # Known operator — pass through (caller handles the call surrounding)
        if tok in KNOWN_OPS:
            return tok
        # Unsupported alias — fail fast
        if tok in unsupported:
            return None
        # Direct alias (or already a real BRAIN name in `direct` value-side)
        if tok in direct:
            return direct[tok]
        # Synthesized — recursively adapt the synthesized expression
        if tok in synthesized:
            return adapt_expression(synthesized[tok], region, max_depth=max_depth - 1)
        # Real BRAIN name not in our table — pass through (best-effort).
        # The validator downstream will reject if it's actually invalid.
        # Examples: fnd6_..., anl4_..., fscore_*, news18 field names, etc.
        return tok

    # Two-pass replace: scan tokens, expand each, reassemble preserving
    # punctuation. We can't simply re.sub because synthesized expansions
    # contain commas and parens that must keep their original positions.
    pieces: list = []
    pos = 0
    for m in _IDENT_RE.finditer(expr_template):
        # Emit the punctuation before this token
        pieces.append(expr_template[pos:m.start()])
        tok = m.group(1)
        expanded = _expand_token(tok, depth=0)
        if expanded is None:
            return None  # unsupported anywhere in tree → fail whole expression
        pieces.append(expanded)
        pos = m.end()
    pieces.append(expr_template[pos:])
    return "".join(pieces)
