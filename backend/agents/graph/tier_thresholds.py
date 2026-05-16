"""
Tier-specific PASS / PASS_PROVISIONAL thresholds.

Each factor tier (T1/T2/T3) has its own PASS bar reflecting its role in the pipeline:
- T1 (single ts_op on a field): low bar — produce seed candidates for T2
- T2 (cross-sectional / smoothing wrapper on T1): mid bar — refined but not yet submission-ready
- T3 (trade_when entry filter on T2): high bar (mirrors project SHARPE_MIN=1.5) — near-submission

evaluation.node_evaluate calls get_tier_thresholds(state.factor_tier) to obtain the dict
and routes concentrated_ok / self_corr_ok per tier rules.

BRAIN role-switch (P3-Brain, 2026-05-16) — tier_thresholds 控制 PROVISIONAL 内部分流
(T1=quality / T2=throughput / T3=submission-ready);真正 submission gate 走
fallback path (tier=None) 的 sharpe_min。优先取 caller 传入的 sharpe_submit_min_override
(来自 MiningTask.config["brain_role_snapshot"] 的 task 启动快照),fallback 到
settings.effective_sharpe_submit_min。这样 running task 在 Consultant 切换前后
依然按启动时的 sharpe 门槛走,避免中途切换破坏 round 内一致性(R2-M3/M4)。
T1/T2/T3 内部分流 sharpe 不受 override 影响 — 它们是 PROVISIONAL 标签,不是
submission gate。新增 TIER4_* 等字段时也不应参与 effective_sharpe_submit_min。
"""

from typing import Dict, Optional

from backend.config import settings


def get_tier_thresholds(
    tier: Optional[int],
    *,
    sharpe_submit_min_override: Optional[float] = None,
) -> Dict:
    """Return PASS + PASS_PROVISIONAL thresholds for the given tier.

    tier=None or any value not in {1, 2, 3} falls back to legacy global thresholds
    (SHARPE_MIN / FITNESS_MIN / TURNOVER_MAX / MAX_CORRELATION). This keeps the
    classic AUTONOMOUS path working when ENABLE_FACTOR_TIERING is False.

    sharpe_submit_min_override: when not None, overrides the fallback-path sharpe_min
    (T1/T2/T3 internal labels unaffected). Callers with task context (state.effective_*
    or task.config["brain_role_snapshot"]) pass this to keep running tasks consistent
    across BRAIN role switches. Callers without task context (e.g. dry-run scripts)
    leave as None → walks settings.effective_sharpe_submit_min.
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
    # tier=None / 0 / unknown → fall back to legacy global thresholds.
    # Effective sharpe submission gate:优先 task 启动快照(避免中途切换),
    # fallback settings.effective_sharpe_submit_min(当前全局)。
    _sharpe_min = (
        sharpe_submit_min_override
        if sharpe_submit_min_override is not None
        else settings.effective_sharpe_submit_min
    )
    return {
        "tier": None,
        "sharpe_min": _sharpe_min,
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
