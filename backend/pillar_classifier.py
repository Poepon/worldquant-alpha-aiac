"""Five Pillars 因子分类静态映射 + 推断 (P2-B, 2026-05-15).

来源: docs/alphagbm_skills_research_2026-05-15.md skill `compare`.

Two paths:
  1. ``infer_pillar(hypothesis_pillar=...)`` — LLM emit 的 pillar 优先
  2. 静态 op + field 投票兜底 — legacy alpha + LLM 偷懒防御

Pure-function module (无 DB 依赖)。

注意:`expected_signal=mean_reversion` 时由 infer_pillar 映射到
``pillar=momentum``(短期反转 = PV momentum 子类);LLM 可继续 emit
mean_reversion(expected_signal 字段)不冲突。
"""
from __future__ import annotations

import re
from typing import Iterable, Optional


# Six canonical pillars + ``other`` fallback. Validated by ``normalize_pillar``.
PILLAR_VALUES: set[str] = {
    "momentum", "value", "quality", "volatility", "sentiment", "other",
}


# Operator → pillar votes. An operator may legitimately belong to multiple
# pillars (e.g. ``ts_mean`` is both momentum and value smoothing) — the
# weighted-vote algorithm in ``infer_pillar`` splits the 1.0 vote among them
# (S1: documented). Empty ``set()`` means "neutral, defer to FIELD_PATTERNS".
OPERATOR_TO_PILLAR: dict[str, set[str]] = {
    # Pure momentum (trend / continuation / short-term reversal on PV)
    "ts_delta":        {"momentum"},
    "ts_returns":      {"momentum"},
    "ts_arg_max":      {"momentum"},
    "ts_arg_min":      {"momentum"},
    "ts_av_diff":      {"momentum"},
    "ts_max_diff":     {"momentum"},
    "ts_min_diff":     {"momentum"},
    # Smoothing — shared between momentum and value
    "ts_mean":         {"momentum", "value"},
    "ts_zscore":       {"momentum", "value"},
    "ts_rank":         {"momentum", "value"},
    # Ratios — value × quality territory
    "divide":          {"value", "quality"},
    # Regression / correlation — quality + sentiment
    "ts_regression":   {"quality"},
    "ts_corr":         {"quality", "sentiment"},
    "ts_covariance":   {"quality"},
    "ts_product":      {"quality"},
    # Dispersion = volatility
    "ts_std_dev":      {"volatility"},
    "ts_skewness":     {"volatility"},
    "ts_kurtosis":     {"volatility"},
    "ts_decay_linear": {"volatility"},
    # Quantile / extremes — sentiment surprise
    "ts_quantile":     {"sentiment"},
    # Neutral operators — defer to field-pattern voting
    "group_rank":       set(),
    "group_zscore":     set(),
    "group_neutralize": set(),
    "group_mean":       set(),
    "rank":             set(),
    "zscore":           set(),
    "log":              set(),
    "sqrt":             set(),
    "abs":              set(),
    "sign":             set(),
    "multiply":         set(),
    "add":              set(),
    "subtract":         set(),
}


# S2 fix: regex word-boundary patterns instead of substring matching.
# ``close_buy_volume`` must NOT be classified as ``momentum`` via the bare
# substring "close" — patterns anchor with ``^...$`` or ``^prefix_``.
# Order is intentionally specific → general (volatility / sentiment / quality /
# value / momentum) so the first match wins.
FIELD_PATTERNS: list[tuple[str, str]] = [
    # ----- volatility (most specific first) -----
    (r"^implied_volatility$", "volatility"),
    (r"^iv_",                 "volatility"),
    (r"^opt\d+_",             "volatility"),
    (r"^realized_vol",        "volatility"),

    # ----- sentiment (analyst / news / social) -----
    (r"^snt",            "sentiment"),
    (r"^anl",            "sentiment"),
    (r"^est",            "sentiment"),
    (r"^fam_",           "sentiment"),
    (r"^news_",          "sentiment"),
    (r"^social_",        "sentiment"),
    (r"^recommendation", "sentiment"),
    (r"^revision",       "sentiment"),
    (r"^surprise",       "sentiment"),
    (r"^consensus",      "sentiment"),
    (r"^actual_eps$",    "sentiment"),
    (r"^actual_sales$",  "sentiment"),

    # ----- quality (profitability / efficiency / stability) -----
    (r"^roic$",          "quality"),
    (r"^roe$",           "quality"),
    (r"^roa$",           "quality"),
    (r"^margin",         "quality"),
    (r"^gross_profit",   "quality"),
    (r"^cash_flow",      "quality"),
    (r"^cfo$",           "quality"),
    (r"^net_income",     "quality"),
    (r"^accrual",        "quality"),
    (r"^debt_to_equity", "quality"),
    (r"^total_debt$",    "quality"),
    (r"^total_assets$",  "quality"),
    (r"^fnd6_teq",       "quality"),
    (r"^asset_turnover", "quality"),

    # ----- value (valuation / mean-reversion) -----
    (r"^eps$",            "value"),
    (r"^pe_",             "value"),
    (r"^pb_",             "value"),
    (r"^book_value",      "value"),
    (r"^book_to_market",  "value"),
    (r"^enterprise_value","value"),
    (r"^dividend",        "value"),
    (r"^revenue$",        "value"),
    (r"^sales$",          "value"),
    (r"^ebit$",           "value"),
    (r"^earnings_yield",  "value"),
    (r"^fnd6_newa1v1300", "value"),

    # ----- momentum (PV — fallback for any bare price/volume field) -----
    (r"^returns$",  "momentum"),
    (r"^ret_",      "momentum"),
    (r"^close$",    "momentum"),
    (r"^vwap$",     "momentum"),
    (r"^open$",     "momentum"),
    (r"^volume$",   "momentum"),
    (r"^amount$",   "momentum"),
    (r"^cap$",      "momentum"),
    # intraday range — volatility
    (r"^high$",     "volatility"),
    (r"^low$",      "volatility"),
]


# Aliases for LLM-emit normalization. Keeps the LLM honest by mapping common
# synonyms / sub-classes into canonical pillars before the membership test.
# ``mean_reversion`` → ``momentum`` per plan §决策 4 (short-term reversal is
# a PV-momentum sub-class).
_ALIASES: dict[str, str] = {
    "mean_reversion":    "momentum",
    "reversal":          "momentum",
    "vol":               "volatility",
    "risk":              "volatility",
    "news":              "sentiment",
    "analyst":           "sentiment",
    "earnings_quality":  "quality",
    "profitability":     "quality",
    "valuation":         "value",
}


def normalize_pillar(raw) -> Optional[str]:
    """LLM emit → canonical pillar string or None.

    None lets ``infer_pillar`` walk to the static-vote fallback rather than
    forcing the row into the ``other`` bucket (which would suppress the
    inference path).
    """
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip().lower()
    if not s:
        return None
    s = _ALIASES.get(s, s)
    return s if s in PILLAR_VALUES else None


def _classify_field(field_name: str) -> Optional[str]:
    """Walk FIELD_PATTERNS in order and return the first matching pillar.

    Returns None when no pattern matches — the caller decides whether to fall
    through to the ``other`` bucket or aggregate more evidence.
    """
    f = (field_name or "").lower()
    if not f:
        return None
    for pat, pillar in FIELD_PATTERNS:
        if re.search(pat, f):
            return pillar
    return None


# Function-name extraction — same regex flavor as diversity_tracker /
# alpha_semantic_validator: ``name(`` anchors a call site.
_OP_PATTERN = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(")


def _extract_operators(expression: str) -> list[str]:
    """Pull function names out of an alpha expression.

    Returns lowercased identifiers regardless of whether they are real ops —
    the caller filters via ``OPERATOR_TO_PILLAR``.
    """
    if not expression:
        return []
    return [m.group(1).lower() for m in _OP_PATTERN.finditer(expression)]


def _extract_field_tokens(expression: str) -> list[str]:
    """Pull non-operator identifiers (presumed fields) out of an expression.

    Excludes anything that already appears in ``OPERATOR_TO_PILLAR`` plus bare
    numeric literals.
    """
    if not expression:
        return []
    tokens = re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b", expression)
    known_ops = set(OPERATOR_TO_PILLAR.keys())
    return [
        t.lower() for t in tokens
        if t.lower() not in known_ops and not t.isdigit()
    ]


def infer_pillar(
    expression: Optional[str] = None,
    *,
    hypothesis_pillar: Optional[str] = None,
    key_fields: Optional[Iterable[str]] = None,
    suggested_operators: Optional[Iterable[str]] = None,
    expected_signal: Optional[str] = None,
) -> str:
    """Resolve a Five Pillars classification for an alpha / hypothesis.

    Four-stage priority:

    1. ``hypothesis_pillar`` (LLM-emit, normalized) — trust the model first
    2. ``expected_signal`` hint — coarse mapping for legacy rows
    3. Weighted vote across operators and key fields
    4. ``other`` — explicit bucket so the caller never sees None

    Returns one of PILLAR_VALUES (never None). M10 documented limitation: the
    vote algorithm is coarse and ~10% of legacy alphas land in ``other`` even
    when human inspection would assign a real pillar — the daily
    pillar_balance_check report exposes that share so we can iterate.
    """
    # 1. LLM-emit (normalized)
    p = normalize_pillar(hypothesis_pillar)
    if p:
        return p

    # 2. expected_signal hint
    if expected_signal:
        es = expected_signal.strip().lower()
        if es == "value":
            return "value"
        if es in ("momentum", "mean_reversion", "reversal"):
            return "momentum"

    # 3. Weighted vote
    votes: dict[str, float] = {pp: 0.0 for pp in PILLAR_VALUES}

    ops: list[str] = []
    if suggested_operators:
        ops.extend(o.strip().lower() for o in suggested_operators if o)
    if expression:
        ops.extend(_extract_operators(expression))
    for op in ops:
        pillars = OPERATOR_TO_PILLAR.get(op, set())
        if not pillars:
            continue
        # Multi-pillar ops split their vote so a single op cannot dominate.
        w = 1.0 / len(pillars)
        for pp in pillars:
            votes[pp] += w

    # Fields carry 2× weight: the economic mechanism is usually visible at the
    # data layer, while operators are often re-used across families.
    fields: list[str] = []
    if key_fields:
        fields.extend(str(f).lower() for f in key_fields if f)
    if expression:
        fields.extend(_extract_field_tokens(expression))
    for f in fields:
        pp = _classify_field(f)
        if pp:
            votes[pp] += 2.0

    # 4. Fallback
    if not any(v > 0 for v in votes.values()):
        return "other"
    top_p, top_v = max(votes.items(), key=lambda kv: kv[1])
    # Require at least one strong signal (>= 1 field hit OR >= 1 full op vote)
    # before trusting the inference. Below that the vote is just noise.
    if top_v < 1.0:
        return "other"
    return top_p
