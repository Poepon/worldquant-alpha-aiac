"""Marginal-contribution analysis → submit recommendation.

Turns BRAIN before-and-after-performance deltas into a SUBMIT / NEUTRAL / SKIP
recommendation with human-readable reasons. Pure, no I/O — unit-testable like
alpha_routing.

Semantics (verified 2026-05-24): the endpoint reports portfolio metrics BEFORE
this alpha is submitted vs AFTER it is merged in, so delta = after - before is
the alpha's MARGINAL contribution to the portfolio:

  - Sharpe is the headline portfolio-quality signal. Δsharpe > 0 means adding the
    alpha lifts the merged portfolio (worth a submission slot); Δsharpe < 0 means
    it drags the portfolio (redundant / too correlated) even if its standalone IS
    sharpe is high.
  - Higher-is-better: sharpe, fitness, returns, pnl.
  - Lower-is-better: turnover, drawdown.

This is the portfolio-contribution dimension; it complements (does not replace)
can_submit and self-correlation gates shown elsewhere. BRAIN removed the
competition `score` field on 2026-05-24, so Δsharpe is the headline signal.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

_HIGHER_BETTER = {"sharpe", "fitness", "returns", "pnl"}
_LOWER_BETTER = {"turnover", "drawdown"}

# Per-metric dead-band: |Δ| below this is treated as "no meaningful change".
# Portfolio-level deltas are small (one alpha among many), so the bands are tight.
_DEADBAND: Dict[str, float] = {
    "sharpe": 0.01,
    "fitness": 0.02,
    "returns": 0.005,
    "pnl": 1.0,
    "turnover": 0.02,
    "drawdown": 0.005,
}

# Weights for the composite marginal_score (display/ranking only; the decision is
# sharpe-led, not score-thresholded). pnl excluded — its scale is dollars.
_WEIGHTS: Dict[str, float] = {
    "sharpe": 1.0,
    "fitness": 0.5,
    "returns": 0.3,
    "drawdown": 0.3,
    "turnover": 0.1,
}

_LABELS = {
    "SUBMIT": "推荐提交",
    "NEUTRAL": "中性（自行判断）",
    "SKIP": "不推荐提交",
    "UNKNOWN": "数据不足",
}


def _signal(metric: str, value: float) -> int:
    """+1 = good marginal move, -1 = bad, 0 = within dead-band."""
    eps = _DEADBAND.get(metric, 0.0)
    if metric in _HIGHER_BETTER:
        return 1 if value > eps else (-1 if value < -eps else 0)
    # lower-is-better
    return 1 if value < -eps else (-1 if value > eps else 0)


def analyze_marginal_contribution(
    deltas: Optional[Dict[str, Any]],
    merged: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Map marginal deltas to a submit recommendation.

    Args:
        deltas: {sharpe, fitness, turnover, returns, pnl, drawdown} after-before
            deltas (any may be None / missing).
        merged: optional stats.after dict (merged-portfolio absolute values) used
            only to enrich reasons.

    Returns a JSON-friendly dict:
        recommendation: SUBMIT | NEUTRAL | SKIP | UNKNOWN
        label:          中文 label
        reasons:        list[str] 中文 explanations (most salient first)
        signals:        {metric: -1|0|1}
        marginal_score: weighted composite (None when no usable deltas)
    """
    deltas = deltas or {}
    merged = merged or {}

    signals: Dict[str, int] = {}
    for k in ("sharpe", "fitness", "returns", "drawdown", "turnover", "pnl"):
        v = deltas.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            signals[k] = _signal(k, float(v))

    d_sharpe = deltas.get("sharpe")
    if not isinstance(d_sharpe, (int, float)) or isinstance(d_sharpe, bool):
        return {
            "recommendation": "UNKNOWN",
            "label": _LABELS["UNKNOWN"],
            "reasons": ["缺少 Δsharpe 数据，无法评估边际贡献"],
            "signals": signals,
            "marginal_score": None,
        }

    # Composite score (display): weighted sum of signed signals.
    score = round(
        sum(_WEIGHTS[k] * signals[k] for k in _WEIGHTS if k in signals), 3
    )

    s_sig = signals.get("sharpe", 0)
    sec = signals.get("fitness", 0) + signals.get("returns", 0)

    # Sharpe-led decision.
    if s_sig > 0:
        rec = "SUBMIT"
    elif s_sig < 0:
        rec = "SKIP"
    else:  # negligible Δsharpe → tie-break on fitness + returns
        rec = "SUBMIT" if sec > 0 else ("SKIP" if sec < 0 else "NEUTRAL")

    reasons = []
    merged_sh = merged.get("sharpe")
    sh_ctx = (
        f"（并入后组合 Sharpe≈{merged_sh:.2f}）"
        if isinstance(merged_sh, (int, float)) and not isinstance(merged_sh, bool)
        else ""
    )
    if s_sig > 0:
        reasons.append(f"组合 Sharpe 边际 +{d_sharpe:.3f}{sh_ctx} — 加入后抬升组合质量，值得占用提交位")
    elif s_sig < 0:
        reasons.append(f"组合 Sharpe 边际 {d_sharpe:+.3f}{sh_ctx} — 加入后拖累组合（冗余/相关性高），不建议提交")
    else:
        reasons.append(f"组合 Sharpe 边际 {d_sharpe:+.3f}{sh_ctx} — 影响可忽略")

    def _fmt_reason(metric: str, label: str, fmt: str = ".3f"):
        v = deltas.get(metric)
        sig = signals.get(metric)
        if sig is None or sig == 0:
            return
        good = sig > 0
        verb = {
            "fitness": ("提升组合 Fitness", "降低组合 Fitness"),
            "returns": ("增厚组合收益", "稀释组合收益"),
            "drawdown": ("降低组合回撤", "加大组合回撤"),
            "turnover": ("降低组合换手", "推高组合换手/成本"),
        }[metric]
        reasons.append(f"Δ{label} {v:+{fmt}} — {verb[0] if good else verb[1]}")

    _fmt_reason("fitness", "Fitness")
    _fmt_reason("returns", "Returns", ".4f")
    _fmt_reason("drawdown", "Drawdown", ".4f")
    _fmt_reason("turnover", "Turnover", ".4f")

    # Caveat: a SUBMIT that worsens drawdown is still worth flagging.
    if rec == "SUBMIT" and signals.get("drawdown", 0) < 0:
        reasons.append("注意：虽提升 Sharpe，但组合回撤略升 — 提交前确认风险可接受")
    if rec == "SKIP" and isinstance(d_sharpe, (int, float)) and d_sharpe < 0:
        reasons.append("提示：standalone IS 指标再好，若边际拖累组合则提交收益为负，建议优化降相关后再评估")

    return {
        "recommendation": rec,
        "label": _LABELS[rec],
        "reasons": reasons,
        "signals": signals,
        "marginal_score": score,
    }
