"""IS diagnostic card — per-alpha 5-dim submit-selection summary (Phase C, 2026-06-08).

Aggregates ALREADY-computed submit-selection signals (robustness / marginal /
orthogonality / sub-universe / IS metrics) into ONE human/machine-readable card
for the submit-backlog page. ZERO new metrics / queries / sims — pure presentation
of what the drain-order endpoint already has.

⚠️ 口径 = ALL IS (BRAIN hides realized OS). This is the pre-submit *quality
picture* (the only controllable lever), NOT an OS predictor. Every dim is an
IS-side proxy; "robustness" = sub-period consistency of the frozen-IS PnL.

Each dim → {level: ok|warn|risk|unknown, ...evidence, note}. ``overall`` fuses
them into SUBMIT / REVIEW / HOLD / SKIP (economic margin gate first, then hard
risk dims, then the marginal scorecard verdict).
"""
from typing import Any, Dict, Optional


def build_diagnostic_card(
    *,
    sharpe: Optional[float],
    turnover: Optional[float],
    margin_bps: Optional[float],
    corr_to_pool: Optional[float],
    robustness_verdict: Optional[str],
    robustness_score: Optional[float],
    marginal_verdict: Optional[str],
    sub_universe_sharpe: Optional[float],
    fools_gold_sharpe: float = 3.0,
    corr_redline: float = 0.7,
    corr_warn: float = 0.5,
    turnover_risk: float = 0.5,
    turnover_warn: float = 0.3,
    margin_floor_bps: float = 5.0,
    sub_univ_min: float = 0.7,
) -> Dict[str, Any]:
    dims: Dict[str, Dict[str, Any]] = {}

    # 1. 过拟合 (overfit): sub-period robustness + fool's-gold (sharpe >= ~3).
    fools_gold = sharpe is not None and sharpe >= fools_gold_sharpe
    if robustness_verdict == "FRAGILE" or fools_gold:
        ov = "risk"
    elif robustness_verdict == "ROBUST":
        ov = "ok"
    elif robustness_verdict == "MODERATE":
        ov = "warn"
    else:
        ov = "unknown"
    dims["overfit"] = {
        "level": ov,
        "robustness_verdict": robustness_verdict,
        "robustness_score": robustness_score,
        "fools_gold": fools_gold,
        "note": "子周期稳健性" + (f"(sharpe≥{fools_gold_sharpe:g} 愚人金嫌疑)" if fools_gold else ""),
    }

    # 2. 流动性 (liquidity): turnover as an impact-cost proxy.
    if turnover is None:
        lq = "unknown"
    elif turnover >= turnover_risk:
        lq = "risk"
    elif turnover >= turnover_warn:
        lq = "warn"
    else:
        lq = "ok"
    dims["liquidity"] = {"level": lq, "turnover": turnover, "note": "换手率→冲击成本代理"}

    # 3. 拥挤/正交 (crowding): max |corr| to the submitted pool (BRAIN self-corr gate).
    if corr_to_pool is None:
        cr = "unknown"
    elif corr_to_pool >= corr_redline:
        cr = "risk"
    elif corr_to_pool >= corr_warn:
        cr = "warn"
    else:
        cr = "ok"
    dims["crowding"] = {
        "level": cr, "corr_to_pool": corr_to_pool,
        "note": f"vs 已提交池 max|corr|(≥{corr_redline:g} 近重复红线)",
    }

    # 4. 子宇宙稳健 (sub-universe): WQ hidden standard ~>= 0.7.
    if sub_universe_sharpe is None:
        su = "unknown"
    elif sub_universe_sharpe >= sub_univ_min:
        su = "ok"
    elif sub_universe_sharpe >= sub_univ_min * 0.7:
        su = "warn"
    else:
        su = "risk"
    dims["sub_universe"] = {
        "level": su, "sub_universe_sharpe": sub_universe_sharpe,
        "note": f"子宇宙 Sharpe(WQ 隐性 ≥{sub_univ_min:g})",
    }

    # 5. 提交建议 (recommendation): economic gate first, then hard risk dims, then marginal.
    margin_ok = margin_bps is not None and margin_bps >= margin_floor_bps
    risk_dims = [k for k in ("overfit", "crowding", "sub_universe") if dims[k]["level"] == "risk"]
    if margin_bps is not None and margin_bps < 0:
        overall, reason = "SKIP", "margin<0(无提交价值)"
    elif not margin_ok:
        overall, reason = "SKIP", f"margin<{margin_floor_bps:g}bps(不够覆盖成本)"
    elif marginal_verdict == "SKIP":
        overall, reason = "SKIP", "边际打分卡 SKIP"
    elif risk_dims:
        overall, reason = "HOLD", "风险维:" + "/".join(risk_dims)
    elif marginal_verdict == "SUBMIT" and dims["overfit"]["level"] == "ok" and dims["crowding"]["level"] == "ok":
        overall, reason = "SUBMIT", "稳健∩正交∩边际皆过"
    else:
        overall, reason = "REVIEW", "无硬伤但非全绿(人工复核)"

    return {"dims": dims, "overall": overall, "reason": reason, "basis": "IS"}
