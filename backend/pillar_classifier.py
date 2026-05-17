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
# Tuple (not set) for deterministic iteration — ``infer_pillar``'s vote dict
# is initialized via this iterable, and ``max(votes.items())`` ties are
# resolved by insertion order. A set would make the tie-break non-deterministic
# across processes (P2 review: test_voting_volatility_via_operators self-
# acknowledged "result in (volatility, momentum)").
PILLAR_VALUES: tuple[str, ...] = (
    "momentum", "value", "quality", "volatility", "sentiment", "other",
)
# Membership-check view for callers that previously used PILLAR_VALUES as a set
# (`x in PILLAR_VALUES` works on tuple too, but frozenset is O(1) for hot paths).
_PILLAR_VALUES_SET: frozenset[str] = frozenset(PILLAR_VALUES)


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

    # ---- Phase 1 Q4 (2026-05-17): Qlib operator name aliases ----
    # LLM-generated alpha 或外部知识 import 可能 emit Qlib-style 名字
    # (Mean/Std/Rank/Delta/Corr/...) 大小写,_extract_operators lowercases
    # 后查这里。映射沿用 backend/qlib_translator.py 的 BRAIN 等价物语义。
    # 与既有 BRAIN 名字 (ts_mean / ts_std_dev / ...) 共存,不覆盖。
    # 注意:`zscore` / `rank` / `sum` / `max` / `min` 等 lowercase 已是 BRAIN
    # 横截面算子名 (上方已有 neutral 标注),Qlib 同名 lowercased 不重新登记
    # 避免覆盖 BRAIN 语义。
    "mean":     {"momentum", "value"},  # alias of ts_mean
    "std":      {"volatility"},          # alias of ts_std_dev
    "var":      {"volatility"},          # alias of ts_std_dev (variance ≈ std²)
    "med":      {"momentum", "value"},   # median behaves like smoothed mean
    "median":   {"momentum", "value"},
    "quantile": {"sentiment"},           # alias of ts_quantile
    "skew":     {"volatility"},          # alias of ts_skewness
    "kurt":     {"volatility"},          # alias of ts_kurtosis
    "idxmax":   {"momentum"},            # alias of ts_arg_max
    "idxmin":   {"momentum"},            # alias of ts_arg_min
    "corr":     {"quality", "sentiment"},# alias of ts_corr
    "cov":      {"quality"},             # alias of ts_covariance
    "wma":      {"volatility"},          # alias of ts_decay_linear
    "ema":      {"volatility"},          # alias of ts_decay_exp
    "slope":    {"quality"},             # alias of ts_regression
    "delta":    {"momentum"},            # alias of ts_delta
    "ref":      set(),                   # alias of ts_delay (neutral)
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
    return s if s in _PILLAR_VALUES_SET else None


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

    # P2 review fix: single-pillar ops are unambiguous evidence (e.g.
    # ts_std_dev → volatility, divide is not in this category since it's
    # multi-pillar) and outweigh fields. Field weight reduced from 2.0 to
    # 1.0 — previously `ts_std_dev(returns)` got tied or lost to momentum
    # because `returns` field-pattern fired at 2.0 vs ts_std_dev op at 1.0.
    SINGLE_PILLAR_OP_WEIGHT = 1.5
    MULTI_PILLAR_OP_SHARE = 1.0  # split 1/N across the membership set
    FIELD_WEIGHT = 1.0

    ops: list[str] = []
    if suggested_operators:
        ops.extend(o.strip().lower() for o in suggested_operators if o)
    if expression:
        ops.extend(_extract_operators(expression))
    for op in ops:
        pillars = OPERATOR_TO_PILLAR.get(op, set())
        if not pillars:
            continue
        if len(pillars) == 1:
            # Unambiguous op — assign the full single-pillar weight directly.
            (only_pillar,) = pillars
            votes[only_pillar] += SINGLE_PILLAR_OP_WEIGHT
        else:
            # Multi-pillar op — share 1.0 across the membership set so a
            # single op cannot dominate over fields + other op evidence.
            share = MULTI_PILLAR_OP_SHARE / len(pillars)
            for pp in pillars:
                votes[pp] += share

    fields: list[str] = []
    if key_fields:
        fields.extend(str(f).lower() for f in key_fields if f)
    if expression:
        fields.extend(_extract_field_tokens(expression))
    for f in fields:
        pp = _classify_field(f)
        if pp:
            votes[pp] += FIELD_WEIGHT

    # 4. Fallback
    if not any(v > 0 for v in votes.values()):
        return "other"
    top_p, top_v = max(votes.items(), key=lambda kv: kv[1])
    # Require ≥1 unit of signal (one field hit OR one full single-pillar op
    # OR enough split-op evidence to add up). Below that the vote is noise.
    if top_v < 1.0:
        return "other"
    return top_p


# =============================================================================
# Phase 1 Q5 (2026-05-17): Five Pillars × Theoretical anchor
# =============================================================================
# Static mapping pillar → academic anchor citations. Phase 1 R8 RAG / hypothesis
# prompt 可注入"该 pillar 的学术根基"上下文,引导 LLM 生成时锚定文献而非凭空。
# Phase 2+ R5 LLM judge 可对照 anchor 验 hypothesis ↔ description 一致性。

THEORETICAL_ANCHORS: dict[str, list[str]] = {
    "momentum": [
        "Jegadeesh & Titman 1993 (3-12m winner-loser)",
        "Carhart 1997 UMD (4-factor extension)",
        "Asness Moskowitz Pedersen 2013 (value+momentum everywhere)",
    ],
    "value": [
        "Fama-French 1993 HML (3-factor)",
        "Fama-French 2015 FF5 (RMW+CMA augmentation)",
        "Lakonishok Shleifer Vishny 1994 (contrarian investment)",
    ],
    "quality": [
        "Novy-Marx 2013 GP (gross profitability)",
        "Asness Frazzini Pedersen 2019 QMJ (Quality-Minus-Junk)",
        "Sloan 1996 (accruals anomaly)",
    ],
    "volatility": [
        "Frazzini Pedersen 2014 BAB (betting against beta)",
        "Ang Hodrick Xing Zhang 2006 IVOL (idiosyncratic vol puzzle)",
        "Baker Bradley Wurgler 2011 (low-vol anomaly)",
    ],
    "sentiment": [
        "Baker Wurgler 2006 (investor sentiment index)",
        "Tetlock 2007 (news textual sentiment)",
        "Diether Malloy Scherbina 2002 (analyst dispersion)",
    ],
    "other": [],  # explicit empty — caller distinguishes "no anchor" vs missing
}


def get_theoretical_anchor(pillar: str) -> list[str]:
    """Return academic anchors for a Five Pillars pillar string.

    Accepts normalized canonical pillar name (one of PILLAR_VALUES) OR a raw
    LLM-emit alias that ``normalize_pillar`` would map. Unknown → empty list
    (NOT "other" — caller can distinguish "anchor lookup failed" from
    "explicitly anchored to nothing").
    """
    if not pillar or not isinstance(pillar, str):
        return []
    p = normalize_pillar(pillar) or pillar.strip().lower()
    return list(THEORETICAL_ANCHORS.get(p, []))
