"""
Tier-specific PASS / PASS_PROVISIONAL thresholds.

Each factor tier (T1/T2/T3) has its own PASS bar reflecting its role in the pipeline:
- T1 (single ts_op on a field): low bar — produce seed candidates for T2
- T2 (cross-sectional / smoothing wrapper on T1): mid bar — refined but not yet submission-ready
- T3 (trade_when entry filter on T2): high bar (mirrors project SHARPE_MIN=1.5) — near-submission

evaluation.node_evaluate calls get_tier_thresholds(state.factor_tier) to obtain the dict
and routes concentrated_ok / self_corr_ok per tier rules.
"""

from typing import Dict, Optional

from backend.config import settings


def get_tier_thresholds(tier: Optional[int]) -> Dict:
    """Return PASS + PASS_PROVISIONAL thresholds for the given tier.

    tier=None or any value not in {1, 2, 3} falls back to legacy global thresholds
    (SHARPE_MIN / FITNESS_MIN / TURNOVER_MAX / MAX_CORRELATION). This keeps the
    classic AUTONOMOUS path working when ENABLE_FACTOR_TIERING is False.
    """
    if tier == 1:
        return {
            "tier": 1,
            "sharpe_min": settings.TIER1_SHARPE_MIN,
            "fitness_min": settings.TIER1_FITNESS_MIN,
            "turnover_min": settings.TIER1_TURNOVER_MIN,
            "turnover_max": settings.TIER1_TURNOVER_MAX,
            "subuniv_min": settings.TIER1_SUBUNIV_MIN,
            "self_corr_max": None,  # T1 不查 self_corr
            "check_self_corr": False,
            "check_concentrated": False,
            "score_pass": settings.TIER1_SCORE_PASS,
            "score_optimize": settings.TIER1_SCORE_OPTIMIZE,
            "provisional": {
                "sharpe_min": settings.TIER1_PROVISIONAL_SHARPE_MIN,
                "fitness_min": settings.TIER1_PROVISIONAL_FITNESS_MIN,
                "turnover_max": settings.TIER1_PROVISIONAL_TURNOVER_MAX,
                "subuniv_min": settings.TIER1_PROVISIONAL_SUBUNIV_MIN,
            },
        }
    if tier == 2:
        # V-22.5 (2026-05-11): self_corr 在 PASS gate 从默认 False 改为可配。
        # 原 rationale("wrapper variants 必然相关,gating 会 FAIL 整个 batch")
        # 假设了"vs within-batch 同 seed 衍生"。实际 BRAIN /correlations/SELF
        # 是 vs OS cache(已提交 portfolio),不是 within-batch — 不会因此误伤。
        # IQC audit 实测发现 13/13 Δscore>0 T2 alphas 都 corr ≥ 0.7,全部
        # BRAIN 提交期拒。开 self_corr gate 让 mining 时就拦掉 → 这些
        # alphas 自动 PROV 而非 PASS,KB 不污染 + 不入 submission queue 队尾。
        # 设 T2_SELF_CORR_MAX=1.0 或 ENABLE_T2_SELF_CORR_CHECK=False 可回退。
        return {
            "tier": 2,
            "sharpe_min": settings.TIER2_SHARPE_MIN,
            "fitness_min": settings.TIER2_FITNESS_MIN,
            "turnover_min": settings.TIER2_TURNOVER_MIN,
            "turnover_max": settings.TIER2_TURNOVER_MAX,
            "subuniv_min": settings.TIER2_SUBUNIV_MIN,
            "self_corr_max": settings.TIER2_SELF_CORR_MAX,
            "check_self_corr": settings.ENABLE_T2_SELF_CORR_CHECK,
            "check_concentrated": True,
            "score_pass": settings.TIER2_SCORE_PASS,
            "score_optimize": settings.TIER2_SCORE_OPTIMIZE,
            "provisional": {
                "sharpe_min": settings.TIER2_PROVISIONAL_SHARPE_MIN,
                "fitness_min": settings.TIER2_PROVISIONAL_FITNESS_MIN,
                "turnover_max": settings.TIER2_PROVISIONAL_TURNOVER_MAX,
                "subuniv_min": settings.TIER2_PROVISIONAL_SUBUNIV_MIN,
            },
        }
    if tier == 3:
        return {
            "tier": 3,
            "sharpe_min": settings.TIER3_SHARPE_MIN,
            "fitness_min": settings.TIER3_FITNESS_MIN,
            "turnover_min": settings.TIER3_TURNOVER_MIN,
            "turnover_max": settings.TIER3_TURNOVER_MAX,
            "subuniv_min": None,  # T3 sub-universe min 用 BRAIN 动态 limit（运行时从 metrics.checks 取）
            "self_corr_max": settings.TIER3_SELF_CORR_MAX,
            "check_self_corr": True,
            "check_concentrated": True,
            "score_pass": settings.TIER3_SCORE_PASS,
            "score_optimize": settings.TIER3_SCORE_OPTIMIZE,
            "provisional": {
                "sharpe_min": settings.TIER3_PROVISIONAL_SHARPE_MIN,
                "fitness_min": settings.TIER3_PROVISIONAL_FITNESS_MIN,
                "turnover_max": settings.TIER3_PROVISIONAL_TURNOVER_MAX,
                "subuniv_dynamic_factor": 0.7,  # PROVISIONAL: BRAIN limit × 0.7
            },
        }
    # tier=None / 0 / unknown → fall back to legacy global thresholds
    return {
        "tier": None,
        "sharpe_min": settings.SHARPE_MIN,
        "fitness_min": settings.FITNESS_MIN,
        "turnover_min": 0.01,
        "turnover_max": settings.TURNOVER_MAX,
        "subuniv_min": None,
        "self_corr_max": settings.MAX_CORRELATION,
        "check_self_corr": True,
        "check_concentrated": True,
        "score_pass": settings.SCORE_PASS_THRESHOLD,
        "score_optimize": settings.SCORE_OPTIMIZE_THRESHOLD,
        "provisional": None,  # legacy path 由 evaluation.py 现有 PROVISIONAL 逻辑处理
    }


def get_min_seed_count() -> int:
    """T2/T3 task 启动门槛 + node_tier_seed_load 早停门槛共用此值。"""
    return settings.MIN_TIER_SEED_COUNT
