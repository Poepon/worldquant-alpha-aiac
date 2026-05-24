"""Marginal-contribution analysis → submit recommendation (multi-dimensional).

Turns BRAIN before-and-after-performance deltas into a SUBMIT / NEUTRAL / SKIP
recommendation. Pure, no I/O — unit-testable like alpha_routing.

Semantics (verified 2026-05-24): the endpoint reports portfolio metrics BEFORE
this alpha is submitted vs AFTER it is merged in, so delta = after - before is
the alpha's MARGINAL contribution to the portfolio.

WHY MULTI-DIMENSIONAL (not Sharpe-led): adding a single alpha (standalone
sharpe ~1.4-1.7) to a mature high-sharpe (~3) portfolio can dilute the portfolio
sharpe (Δsharpe < 0) when the alpha is correlated — a structural effect, not a
quality signal. Empirically the Δsharpe sign VARIES by alpha (a 20-alpha live
sample on 2026-05-24 was ~17/20 positive, but earlier batches skewed negative),
so Sharpe alone is an unreliable gate. The right question is "does adding this
alpha make the portfolio better across return AND risk dimensions, and is it
temporally robust?" — a weighted scorecard of all dimensions (positive AND
negative) plus hard guardrails for genuine red flags. Sharpe is the
highest-weighted single dimension but does NOT veto on its own, and a positive
Sharpe cannot override severe risk/cost deterioration.

Dimensions (direction +1 = higher-better, -1 = lower-better):
  return side : sharpe, returns, margin, fitness, pnl_norm (Δpnl / bookSize)
  risk side   : drawdown, turnover
  robustness  : recent_yearly_sharpe (median Δsharpe over the latest ~2 years)

This is the portfolio-contribution dimension; it complements (does not replace)
can_submit and self-correlation gates shown elsewhere.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

# metric -> (direction, scale, weight, display_name, value_fmt)
#   direction: +1 higher-is-better, -1 lower-is-better
#   scale:     a "clearly significant" |Δ| — Δ/scale maps to a normalized unit
#   weight:    relative importance in the composite
# scale: |Δ| at which a dimension is a "clearly significant" move. CALIBRATED
# from the observed live distribution (median |Δ| over 20 can_submit alphas:
# sharpe 0.07 / returns 0.002 / margin 0.00022 / drawdown 0.0017 / turnover
# 0.0217) — scale ≈ 2× median so the median move reads as a moderate (~0.5)
# signal and only genuinely large moves saturate toward ±_NORM_CAP. Re-derive
# with scripts/iqc_marginal_audit.py (scale ≈ 2 × median(|Δ|)) if BRAIN shifts.
#
# pnl_norm REMOVED 2026-05-24 (3rd review): it is collinear with `returns`
# (both ≈ PnL/bookSize) and double-counted the return side. recent_yearly_sharpe
# kept at WEIGHT 0 — it is shown in the scorecard + drives the decay guardrail,
# but does NOT enter the composite (it is collinear with `sharpe`, just a
# different window — scoring it would double-count the sharpe family).
_DIMS: Dict[str, Tuple[int, float, float, str, str]] = {
    "sharpe":               (+1, 0.12,   1.0, "Sharpe", ".3f"),
    "returns":              (+1, 0.004,  0.8, "Returns", ".4f"),
    "margin":               (+1, 0.0004, 0.5, "Margin", ".5f"),
    "fitness":              (+1, 0.08,   0.5, "Fitness", ".3f"),
    "drawdown":             (-1, 0.003,  0.6, "回撤", ".4f"),
    "turnover":             (-1, 0.045,  0.5, "换手", ".4f"),
    "recent_yearly_sharpe": (+1, 0.05,   0.0, "近年Sharpe趋势", ".3f"),
}

_RETURN_SIDE = {"sharpe", "returns", "margin", "fitness"}
_RISK_SIDE = {"drawdown", "turnover"}

_NORM_CAP = 1.5          # clip normalized magnitude to ±1.5
_NOISE_FLOOR = 0.15      # |normalized| <= this → neutral (abstains from composite)

# Composite thresholds on the weighted-average normalized score (∈ [-1.5, 1.5]).
_T_SUBMIT = 0.25
_T_SKIP = -0.25

# Guardrail trigger magnitudes (on normalized scale). Set to "genuinely severe"
# (≈ 2.5× median move) so routine portfolio shifts don't trip them — at the old
# -0.7, a median +0.02 turnover increase wrongly read as a "severe cost blowup".
# Guardrails only DOWNGRADE to NEUTRAL (a genuine red flag → don't auto-recommend
# SUBMIT, force human judgment). A SKIP comes only from a genuinely negative
# composite, never from a single guardrail.
_GR_RISK = -1.2          # drawdown OR turnover this bad → cap NEUTRAL
_GR_RETURN = -1.2        # returns this bad (strong dilution) → cap NEUTRAL
_GR_YEARLY = -1.0        # recent-year sharpe decaying this much → cap NEUTRAL

_LABELS = {
    "SUBMIT": "推荐提交",
    "NEUTRAL": "中性（自行判断）",
    "SKIP": "不推荐提交",
    "UNKNOWN": "数据不足",
}
_RANK = {"SKIP": 0, "NEUTRAL": 1, "SUBMIT": 2}
_RANK_INV = {v: k for k, v in _RANK.items()}

_VERB = {
    "sharpe": ("抬升组合 Sharpe", "稀释组合 Sharpe"),
    "returns": ("增厚组合收益", "稀释组合收益"),
    "margin": ("提高单位交易收益率", "降低单位交易收益率"),
    "fitness": ("提升组合 Fitness", "降低组合 Fitness"),
    "drawdown": ("降低组合回撤", "加大组合回撤"),
    "turnover": ("降低组合换手/成本", "推高组合换手/成本"),
    "recent_yearly_sharpe": ("近年边际 Sharpe 改善（稳健）", "近年边际 Sharpe 恶化（衰减风险）"),
}


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def recent_yearly_sharpe_delta(yearly_block: Optional[Dict[str, Any]], recent_n: int = 2) -> Optional[float]:
    """Median per-year Δsharpe (after - before) over the latest `recent_n` years.

    Parses BRAIN's {before:{schema,records}, after:{schema,records}} yearly block
    (records are positional arrays keyed by schema.properties). Returns None when
    absent/unparsable (caller treats the robustness dimension as missing). A
    strongly negative value flags an alpha whose marginal contribution is decaying
    even if its all-time Δsharpe looks fine.
    """
    if not isinstance(yearly_block, dict):
        return None

    def _per_year_sharpe(side: Dict[str, Any]) -> Dict[str, float]:
        if not isinstance(side, dict):
            return {}
        props = (side.get("schema") or {}).get("properties") or []
        names = [p.get("name") if isinstance(p, dict) else p for p in props]
        try:
            yi = names.index("year")
            si = names.index("sharpe")
        except ValueError:
            return {}
        out: Dict[str, float] = {}
        for rec in side.get("records") or []:
            if not isinstance(rec, (list, tuple)) or len(rec) <= max(yi, si):
                continue
            yr, sh = rec[yi], rec[si]
            if _is_num(sh):
                out[str(yr)] = float(sh)
        return out

    before = _per_year_sharpe(yearly_block.get("before") or {})
    after = _per_year_sharpe(yearly_block.get("after") or {})
    common = sorted(set(before) & set(after))
    if not common:
        return None
    recent = common[-recent_n:]
    deltas = sorted(after[y] - before[y] for y in recent)
    n = len(deltas)
    mid = n // 2
    return deltas[mid] if n % 2 else (deltas[mid - 1] + deltas[mid]) / 2.0


def _normalize(metric: str, delta: float) -> float:
    direction, scale, *_ = _DIMS[metric]
    return max(-_NORM_CAP, min(_NORM_CAP, direction * delta / scale))


def analyze_marginal_contribution(
    deltas: Optional[Dict[str, Any]],
    merged: Optional[Dict[str, Any]] = None,
    baseline: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Map marginal deltas to a multi-dimensional submit recommendation.

    Args:
        deltas: after-before deltas for any of the _DIMS metrics (incl. derived
            pnl_norm and recent_yearly_sharpe). Any may be None / missing /
            non-finite (treated as "dimension absent").
        merged: stats.after absolute values (for context in the rationale).
        baseline: stats.before absolute values (reserved for absolute-level
            context; currently used only to note high-portfolio tolerance).

    Returns a JSON-friendly dict (back-compat fields kept: recommendation, label,
    reasons, signals, marginal_score):
        recommendation:  SUBMIT | NEUTRAL | SKIP | UNKNOWN
        label:           中文 label
        composite_score: weighted-average normalized contribution ∈ [-1.5, 1.5]
        positives/negatives/neutrals: scorecard rows {metric,name,delta,
                         normalized,weight,text}
        guardrails:      list of triggered hard-flag descriptions (中文)
        rationale:       one-line 中文 verdict explanation
        reasons:         flattened 中文 bullet list (back-compat; positives then
                         negatives then guardrails)
        signals:         {metric: -1|0|1} (back-compat)
        marginal_score:  alias of composite_score (back-compat)
    """
    deltas = deltas or {}
    merged = merged or {}

    present: Dict[str, float] = {
        m: float(deltas[m]) for m in _DIMS if m in deltas and _is_num(deltas.get(m))
    }

    # Core data gate: need at least one of sharpe / returns to say anything.
    if "sharpe" not in present and "returns" not in present:
        return {
            "recommendation": "UNKNOWN",
            "label": _LABELS["UNKNOWN"],
            "composite_score": None,
            "marginal_score": None,
            "positives": [], "negatives": [], "neutrals": [],
            "guardrails": [],
            "rationale": "缺少 Sharpe / Returns 边际数据，无法评估",
            "reasons": ["缺少 Sharpe / Returns 边际数据，无法评估"],
            "signals": {},
        }

    positives: List[Dict[str, Any]] = []
    negatives: List[Dict[str, Any]] = []
    neutrals: List[Dict[str, Any]] = []
    signals: Dict[str, int] = {}
    norm: Dict[str, float] = {}
    wsum = 0.0
    contrib = 0.0

    for m, dval in present.items():
        direction, scale, weight, name, fmt = _DIMS[m]
        n = _normalize(m, dval)
        norm[m] = n
        good = n > _NOISE_FLOOR
        bad = n < -_NOISE_FLOOR
        # Only dimensions with a clear (above-noise) signal AND non-zero weight
        # vote in the composite — neutral / display-only (weight 0) dims abstain
        # so they neither dilute nor drag the weighted average.
        if weight > 0 and (good or bad):
            wsum += weight
            contrib += weight * n
        signals[m] = 1 if good else (-1 if bad else 0)
        verb = _VERB[m][0] if good else (_VERB[m][1] if bad else "影响可忽略")
        row = {
            "metric": m, "name": name, "delta": round(dval, 6),
            "normalized": round(n, 3), "weight": weight,
            "text": f"Δ{name} {dval:+{fmt}} — {verb}",
        }
        (positives if good else (negatives if bad else neutrals)).append(row)

    positives.sort(key=lambda r: -r["normalized"] * r["weight"])
    negatives.sort(key=lambda r: r["normalized"] * r["weight"])

    composite = round(contrib / wsum, 3) if wsum else 0.0

    # Base verdict from the weighted-average normalized contribution.
    if composite >= _T_SUBMIT:
        base = "SUBMIT"
    elif composite <= _T_SKIP:
        base = "SKIP"
    else:
        base = "NEUTRAL"

    # Hard guardrails — they only DOWNGRADE (cap) the recommendation.
    guardrails: List[str] = []
    cap = "SUBMIT"  # most permissive cap

    def _cap_to(level: str):
        nonlocal cap
        if _RANK[level] < _RANK[cap]:
            cap = level

    if norm.get("drawdown", 0) <= _GR_RISK or norm.get("turnover", 0) <= _GR_RISK:
        guardrails.append("风险/成本显著恶化（回撤或换手大幅上升）— 不允许直接推荐提交")
        _cap_to("NEUTRAL")
    if norm.get("returns", 0) <= _GR_RETURN:
        guardrails.append("边际收益明显为负 — 真实稀释组合收益，需人工权衡")
        _cap_to("NEUTRAL")
    if norm.get("recent_yearly_sharpe", 0) <= _GR_YEARLY:
        guardrails.append("近年边际 Sharpe 明显衰减 — 可能已被市场套利，提交价值存疑")
        _cap_to("NEUTRAL")

    rec = _RANK_INV[min(_RANK[base], _RANK[cap])]

    # Rationale
    pos_w = sum(r["weight"] * r["normalized"] for r in positives)
    neg_w = -sum(r["weight"] * r["normalized"] for r in negatives)
    merged_sh = merged.get("sharpe")
    sh_ctx = (
        f"（并入后组合 Sharpe≈{merged_sh:.2f}）"
        if _is_num(merged_sh) else ""
    )
    if rec == "SUBMIT":
        head = f"综合边际为正(评分{composite:+.2f}){sh_ctx}：正向贡献压过负向，建议提交"
    elif rec == "SKIP":
        head = f"综合边际为负(评分{composite:+.2f}){sh_ctx}：负向拖累压过正向，不建议提交"
    else:
        head = f"综合边际中性(评分{composite:+.2f}){sh_ctx}：正负相抵或存在否决项，建议人工权衡"
    if guardrails and _RANK[cap] < _RANK[base]:
        head += "（否决门触发，已下调推荐）"

    reasons = [head] + [r["text"] for r in positives] + [r["text"] for r in negatives] + guardrails

    return {
        "recommendation": rec,
        "label": _LABELS[rec],
        "composite_score": composite,
        "marginal_score": composite,  # back-compat alias
        "positives": positives,
        "negatives": negatives,
        "neutrals": neutrals,
        "guardrails": guardrails,
        "rationale": head,
        "reasons": reasons,
        "signals": signals,
    }
