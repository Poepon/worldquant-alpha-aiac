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
            "provisional": {
                "sharpe_min": settings.TIER1_PROVISIONAL_SHARPE_MIN,
                "fitness_min": settings.TIER1_PROVISIONAL_FITNESS_MIN,
                "turnover_max": settings.TIER1_PROVISIONAL_TURNOVER_MAX,
                "subuniv_min": settings.TIER1_PROVISIONAL_SUBUNIV_MIN,
            },
        }
    if tier == 2:
        return {
            "tier": 2,
            "sharpe_min": settings.TIER2_SHARPE_MIN,
            "fitness_min": settings.TIER2_FITNESS_MIN,
            "turnover_min": settings.TIER2_TURNOVER_MIN,
            "turnover_max": settings.TIER2_TURNOVER_MAX,
            "subuniv_min": settings.TIER2_SUBUNIV_MIN,
            "self_corr_max": None,  # T2 不查 self_corr（同种子产物簇允许共存，T3 阶段才收敛）
            "check_self_corr": False,
            "check_concentrated": True,
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
        "provisional": None,  # legacy path 由 evaluation.py 现有 PROVISIONAL 逻辑处理
    }


def get_min_seed_count() -> int:
    """T2/T3 task 启动门槛 + node_tier_seed_load 早停门槛共用此值。"""
    return settings.MIN_TIER_SEED_COUNT
