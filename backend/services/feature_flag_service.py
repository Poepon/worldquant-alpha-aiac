"""FeatureFlagService — runtime ENABLE_* flag store backing /ops/feature-flags.

Source: docs/alphagbm_skills_research_2026-05-15.md, ops dashboard plan §1.4.

Architecture
------------
Three layers, in priority order on read::

    settings.ENABLE_X  →  __getattribute__ hook in backend/config.py  →
        _flag_override_cache (module-level dict, refreshed every 60s) →
            DB row in `feature_flag_overrides` (durable source of truth)
                (Redis hash `aiac:feature_flags:v1` is *only* a short-lived
                 cross-process invalidation hint; DB is authoritative)

Read fallback: if Redis is down we still serve from DB. If DB is also down
we fall back to env defaults (the hook's super().__getattribute__ path).
The system NEVER crashes on a flag read.

Whitelist
---------
Only flags listed in :data:`SUPPORTED_FLAGS` may be overridden. The keys
must match the attribute names on :class:`backend.config.Settings` exactly,
otherwise the override is silently ignored on read. New flags must be
added here AND in ``Settings``; we don't auto-discover to avoid letting
the ops console flip arbitrary settings (e.g. SHARPE_MIN).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, select

from backend.models.config import FeatureFlagAudit, FeatureFlagOverride
from backend.services.base import BaseService, transactional

logger = logging.getLogger("services.feature_flag")


# ---------------------------------------------------------------------------
# Whitelist + types
# ---------------------------------------------------------------------------

FLAG_TYPES = ("bool", "int", "float", "str", "json")

LIFECYCLES = ("operational", "experimental", "dormant")
DOMAINS = ("submit", "rag", "evaluation", "generation", "llm-routing",
           "regime", "breadth", "brain", "kb", "misc")


@dataclass(frozen=True)
class FlagSpec:
    """Description of a single overridable flag."""
    name: str
    flag_type: str              # one of FLAG_TYPES
    lifecycle: str              # one of LIFECYCLES — drives frontend top-level tab
    domain: str                 # one of DOMAINS — drives frontend sub-group
    description: str

    def __post_init__(self):
        if self.lifecycle not in LIFECYCLES:
            raise ValueError(f"FlagSpec {self.name}: invalid lifecycle {self.lifecycle!r}")
        if self.domain not in DOMAINS:
            raise ValueError(f"FlagSpec {self.name}: invalid domain {self.domain!r}")


# Source of truth for what /ops/feature-flags is allowed to flip. Keep
# alphabetically grouped by P-tier so the rendered table stays stable.
SUPPORTED_FLAGS: Dict[str, FlagSpec] = {
    # --- P0 ---
    "ENABLE_SIGNAL_CONTROL_DUAL_RUN": FlagSpec(
        name="ENABLE_SIGNAL_CONTROL_DUAL_RUN",
        flag_type="bool",
        lifecycle="experimental",
        domain="evaluation",
        description="信号-对照双跑 (额外消耗 BRAIN 模拟配额;评估归因更准)",
    ),
    # --- P1 ---
    "ENABLE_GRADED_SCORE": FlagSpec(
        name="ENABLE_GRADED_SCORE",
        flag_type="bool",
        lifecycle="experimental",
        domain="evaluation",
        description="百分位归一化评分 (5 档 A-E)",
    ),
    "ENABLE_ROBUSTNESS_CHECK": FlagSpec(
        name="ENABLE_ROBUSTNESS_CHECK",
        flag_type="bool",
        lifecycle="experimental",
        domain="evaluation",
        description="What-if 参数扰动鲁棒性门 (增加 ~N 次 simulate)",
    ),
    # --- P2-A 宏观叙事 ---
    "ENABLE_MACRO_NARRATIVE_GUIDANCE": FlagSpec(
        name="ENABLE_MACRO_NARRATIVE_GUIDANCE",
        flag_type="bool",
        lifecycle="experimental",
        domain="generation",
        description="LLM prompt 注入 macro narrative 段 (引导 economic-mechanism 生成)",
    ),
    "ENABLE_MACRO_NARRATIVE_EXTRACT": FlagSpec(
        name="ENABLE_MACRO_NARRATIVE_EXTRACT",
        flag_type="bool",
        lifecycle="dormant",
        domain="generation",
        description="每日 10:00 SH LLM 批生成长尾 narrative (消耗 token)",
    ),
    # --- P2-B 五支柱平衡 ---
    "ENABLE_PILLAR_AWARE_SELECTION": FlagSpec(
        name="ENABLE_PILLAR_AWARE_SELECTION",
        flag_type="bool",
        lifecycle="experimental",
        domain="generation",
        description="hypothesis 节点根据 deficit 给出 pillar nudge",
    ),
    "ENABLE_ORTHOGONAL_PROMPT_STEERING": FlagSpec(
        name="ENABLE_ORTHOGONAL_PROMPT_STEERING",
        flag_type="bool",
        lifecycle="experimental",
        domain="generation",
        description=(
            "Phase A 正交导向探索:把 SUBMITTED 池的 pillar 覆盖画像作软 NUDGE 注入 "
            "hypothesis prompt,引导 LLM 探索与已提交池正交的机制(负向知识)。OFF → "
            "submitted_pool_profile 不调用 + PromptContext.submitted_pool_profile=None → "
            "字节不变。shadow-first 再 A/B。见 orthogonality_steered_exploration_plan_2026-06-05.md"
        ),
    ),
    # --- Pool-Phase2 认知层 / 战术生成先验 ---
    "ENABLE_POOL_COGNITIVE_RECONCILE": FlagSpec(
        name="ENABLE_POOL_COGNITIVE_RECONCILE",
        flag_type="bool",
        lifecycle="experimental",
        domain="kb",
        description=(
            "池认知层 reconcile beat:扫近窗 landed alpha(watermark on created_at + "
            "grace),驱动 hypotheses 生命周期(auto-activate / refresh can_submit_count / "
            "PROMOTE on can_submit_count>0 非 pass_count / 廉价归因)。替代 FLAT 同步 "
            "in-graph CoSTEER。OFF → beat no-op。见 kb_feedback_redesign_2026-06-06.md §7 Track C。"
        ),
    ),
    "ENABLE_R1A_KB_SKELETON_FREQUENCY": FlagSpec(
        name="ENABLE_R1A_KB_SKELETON_FREQUENCY",
        flag_type="bool",
        lifecycle="experimental",
        domain="kb",
        description=(
            "R1a-v1 生成先验:挖近窗 SUCCESS_PATTERN skeleton 频率,把最拥挤结构作 SOFT "
            "降权 nudge 注入 hypothesis prompt(非硬禁用表)。sample-size-gated + [:5] cap + "
            "field-aware。OFF → 字节不变。soak + pillar-nudge A/B 双过前不升级。见 §7 Track B。"
        ),
    ),
    "ENABLE_FIELD_HYGIENE": FlagSpec(
        name="ENABLE_FIELD_HYGIENE",
        flag_type="bool",
        lifecycle="operational",
        domain="generation",
        description=(
            "字段卫生(#25c):池字段路径只给 code-gen LLM 数值信号字段,剔除非信号元数据"
            "(UNIVERSE 成员 flag top500/SYMBOL/UTC 时间戳 *_time_utc/日期 *_date_utc/ISO 代码"
            " iso_code)。修 2026-05-20 生成塌方(LLM 在时间戳/代码/flag 上建退化式→负 sharpe)。"
            "默认 ON(纠错);OFF=legacy 全字段 roster。kill-switch。"
        ),
    ),
    # --- P2-C 市场体制 (Consolidated 2026-05-19: single switch + stage str) ---
    "ENABLE_REGIME": FlagSpec(
        name="ENABLE_REGIME",
        flag_type="bool",
        lifecycle="operational",
        domain="regime",
        description=(
            "P2-C 市场体制总开关。OFF 时无 regime 推断、无阈值倍率、无 style "
            "preset 注入(byte-for-byte legacy)。ON 时按 REGIME_STAGE 决定生效"
            "深度。Consolidated 2026-05-19 from 3 booleans into 1 + stage str。"
        ),
    ),
    "REGIME_STAGE": FlagSpec(
        name="REGIME_STAGE",
        flag_type="str",
        lifecycle="operational",
        domain="regime",
        description=(
            "Regime staged rollout 等级(类比 QLIB_PRESCREEN_MODE):"
            "'inference' = 仅每日 10:30 SH 推断 regime + 写 Redis cache 攒数据;"
            "'thresholds' = 推断 + 按 regime 倍率应用 sharpe/fitness/turnover;"
            "'style' = 推断 + 倍率 + 注入投资哲学 block 进 hypothesis prompt。"
            "Default 'inference'。需 ENABLE_REGIME=True 才生效。"
        ),
    ),
    # --- P2-D 负向知识 ---
    "ENABLE_NEGATIVE_KNOWLEDGE_NUDGE": FlagSpec(
        name="ENABLE_NEGATIVE_KNOWLEDGE_NUDGE",
        flag_type="bool",
        lifecycle="experimental",
        domain="generation",
        description="hypothesis prompt 加近期 top pitfalls 警告段",
    ),
    # --- P3-Brain 角色切换 ---
    "ENABLE_BRAIN_CONSULTANT_MODE": FlagSpec(
        name="ENABLE_BRAIN_CONSULTANT_MODE",
        flag_type="bool",
        lifecycle="operational",
        domain="brain",
        description="BRAIN Consultant 模式 — 解锁 multi-sim/PROD-corr/全球 region/Sharpe≥1.58。仅在收到 BRAIN 升级邮件后翻。",
    ),
    # --- Phase 1 R4' Dual-channel RAG ---
    "ENABLE_DUAL_CHANNEL_RAG": FlagSpec(
        name="ENABLE_DUAL_CHANNEL_RAG",
        flag_type="bool",
        lifecycle="experimental",
        domain="rag",
        description="hypothesis prompt 拆分 success_patterns / failure_pitfalls 成 Channel A (✓) + Channel B (⛔) 视觉分离。OFF 时 byte-for-byte legacy 单段渲染。",
    ),
    # --- Phase 1 R2/Q7 Contextual Thompson Sampling DirectionBandit ---
    "ENABLE_DIRECTION_BANDIT": FlagSpec(
        name="ENABLE_DIRECTION_BANDIT",
        flag_type="bool",
        lifecycle="experimental",
        domain="generation",
        description="启用 ContextualDirectionBandit (4-arm Beta-Bernoulli + (region, dataset_category, recent_failure_pattern) 三维 context) 选 strategy 生成方式。任一 segment ≥ 10 select 触发 GO 闸门。",
    ),
    # --- Phase 1 R3/Q8 AST subtree-isomorphism diversity dim ---
    "ENABLE_AST_DIVERSITY_DIM": FlagSpec(
        name="ENABLE_AST_DIVERSITY_DIM",
        flag_type="bool",
        lifecycle="experimental",
        domain="evaluation",
        description="启用 DiversityScore 第 6 维 ast_diversity (1 − Jaccard subtree overlap)。Light wiring 仅记录到 ast_distance_log,不 gate 生成。Phase 2+ R10 family-cap 复用此信号。",
    ),
    # --- G3 AST originality gate (Phase A shadow, 2026-05-19) ---
    "ENABLE_AST_ORIGINALITY_GATE": FlagSpec(
        name="ENABLE_AST_ORIGINALITY_GATE",
        flag_type="bool",
        lifecycle="experimental",
        domain="evaluation",
        description=(
            "G3 AST 原创度门 (Phase A shadow):node_evaluate R10 之后调 "
            "backend.alpha_originality.OriginalityChecker,ast_distance "
            "(1−Jaccard subtree overlap) < AST_ORIGINALITY_MIN_DISTANCE (τ, "
            "默认 0.15) 的 alpha 写 metrics['_g3_*'] + "
            "['_g3_ast_originality_blocked']=True。"
            "AST_ORIGINALITY_MODE 控制效果:'shadow' (默认,仅 log + metrics) / "
            "'soft' (标 PASS_PROVISIONAL 仍 simulate) / 'hard' (标 FAIL 跳 "
            "simulate)。前置:Phase 1 R3/Q8 ast_distance_log 已 light-wire,"
            "G3 复用 backend.knowledge_extraction.ast_distance_from_expressions。"
            "Phase B 在 /ops/g3/originality-stats 7d 数据上 calibrate τ,Phase C "
            "operator 决策 promote mode shadow→soft→hard。"
            "与 R10 family-cap 互补:R10 看 operator-sequence 同族,G3 看 "
            "AST subtree 同构。Soft-fail:checker 异常永不 break round。"
            "⚠️ @deprecated_pending_r12_decision (Sprint 4, 2026-05-20): "
            "B4.1 Sprint 4 ships G3-v2 grammar-aware validator as the "
            "successor path (ENABLE_GRAMMAR_VALIDATOR). G3 shadow code "
            "stays UNCHANGED in Sprint 4 (freeze constraint preserved). "
            "Sprint 5 B4.2 conditionally retires this shadow per R12 "
            "decision (GO/NO-GO/PARTIAL routes per plan v5 §6.14b)."
        ),
    ),
    # --- R8 Hierarchical RAG ---
    "ENABLE_HIERARCHICAL_RAG": FlagSpec(
        name="ENABLE_HIERARCHICAL_RAG",
        flag_type="bool",
        lifecycle="operational",
        domain="rag",
        description=(
            "Phase 3 R8 (Alpha-GPT v1.0 v2): 4-layer fall-through hierarchical "
            "RAG retriever — RAG#0 exact pattern_hash + Q9 decayed filter → "
            "RAG#1 pillar/theme via infer_pillar JOIN → RAG#2 family_signature "
            "via family_classifier + R5 composite_score 排序 + R10 family_capped "
            "filter → RAG#3 field-level current expr × dataset/region availability。"
            "Orchestrator sequential fall-through (NOT parallel union) per plan "
            "§4.1 — cost/determinism/cache-friendly。Total cap RAG_HIER_TOTAL_CAP=20。"
            "Redis cache TTL RAG_HIER_CACHE_TTL_SEC=300。Additive overlay — "
            "legacy query() preserved; flag dispatch routes to query_hierarchical。"
            "Rollback < 1 min via flag flip OFF。前置 Alembic b3c8d9e2f4a1 KB "
            "meta_data GIN index + 一次性回填 3K+ entries。"
        ),
    ),
    # --- RAG category-overlap A/B experiment harness ---
    "ENABLE_RAG_CATEGORY_AB": FlagSpec(
        name="ENABLE_RAG_CATEGORY_AB",
        flag_type="bool",
        lifecycle="experimental",
        domain="rag",
        description=(
            "实验台 (2026-05-21): per-round A/B 评测 P0 的 dataset-category-overlap "
            "检索是否真提升挖矿产出。ON 时 node_rag_query 按 hash((task_id,round))%2 "
            "把每轮分到 'control'(layer1_pillar 关闭 category 派生,回退 pillar/recency) "
            "或 'category'(现 P0 行为);arm 写 alpha.metrics['_rag_ab_arm'] + "
            "alpha_failures.rag_ab_arm。scripts/rag_ab_report.py 算 PASS-per-real-sim "
            "(分母合并 alphas+alpha_failures、扣 PRESIM_SKIP/DEDUP_SKIP) 按 arm 对比。"
            "OFF(默认)= arm 空、category 常开 = 现状零变化。soft-fail。"
        ),
    ),
    # --- Phase 3 Q10: pyqlib local pre-screen (Multi-Fidelity Layer 0) ---
    "ENABLE_QLIB_PRESCREEN": FlagSpec(
        name="ENABLE_QLIB_PRESCREEN",
        flag_type="bool",
        lifecycle="experimental",
        domain="evaluation",
        description=(
            "Phase 3 Q10 (2026-05-18): local pyqlib pre-screen layer in front "
            "of BRAIN simulate. Translate BRAIN expression → qlib DSL → eval "
            "on local OHLCV snapshot → approximate Sharpe/IC. Below floor "
            "rejects (hard mode) save BRAIN call. 3-tier engine degrade "
            "(pyqlib_live → pyqlib_snapshot → pandas_snapshot → disabled). "
            "Untranslatable expressions (group_neutralize / trade_when / "
            "fnd*) skip — proceed to BRAIN. Coverage ~30-45% T1 traffic. "
            "Must pair with QLIB_PRESCREEN_MODE setting (shadow|soft|hard)."
        ),
    ),
    "QLIB_PRESCREEN_MODE": FlagSpec(
        name="QLIB_PRESCREEN_MODE",
        flag_type="str",
        lifecycle="experimental",
        domain="evaluation",
        description=(
            "Q10 rollout stage: 'shadow' (log only, BRAIN proceeds) → 'soft' "
            "(log + alpha.metrics['_qlib_prescreen_warned']=True, BRAIN "
            "proceeds) → 'hard' (skip BRAIN, alpha marked simulation_success="
            "False with simulation_error). Default 'shadow'. Calibration phase "
            "~5d shadow → 3d soft → hard per plan §10 stage gates."
        ),
    ),
    # --- SoftReg-P1 软正则 (2026-05-23): AlphaAgent-style 复杂度+原创软惩罚 ---
    "CODE_GEN_SOFT_REG_MODE": FlagSpec(
        name="CODE_GEN_SOFT_REG_MODE",
        flag_type="str",
        lifecycle="experimental",
        domain="generation",
        description=(
            "Soft regularizer over code-gen candidates at pre_simulate_filter: "
            "'off' (inert) | 'shadow' (compute + stamp alpha.metrics['_soft_reg_*'] "
            "only, default) | 'soft' (also down-weight pre-sim P(PASS) = "
            "p*(1-lambda*penalty)). P1 legs = complexity + originality (alignment "
            "= R5 reserved for P2). No 'hard' mode by design (no complexity reject)."
        ),
    ),
    "CODE_GEN_SOFT_REG_LAMBDA": FlagSpec(
        name="CODE_GEN_SOFT_REG_LAMBDA",
        flag_type="float",
        lifecycle="experimental",
        domain="generation",
        description="soft 模式 P(PASS) 最大降权比例 (0=无效, 1=penalty=1 的候选完全压制)。",
    ),
    "CODE_GEN_SOFT_REG_W_COMPLEXITY": FlagSpec(
        name="CODE_GEN_SOFT_REG_W_COMPLEXITY",
        flag_type="float",
        lifecycle="experimental",
        domain="generation",
        description="复杂度腿权重 (在活跃腿间归一化;只有与其他权重的比值有意义)。",
    ),
    "CODE_GEN_SOFT_REG_W_ORIGINALITY": FlagSpec(
        name="CODE_GEN_SOFT_REG_W_ORIGINALITY",
        flag_type="float",
        lifecycle="experimental",
        domain="generation",
        description="原创腿权重 (AST min-distance → 1-dist 惩罚;活跃腿间归一化)。",
    ),
    "CODE_GEN_SOFT_REG_W_ALIGNMENT": FlagSpec(
        name="CODE_GEN_SOFT_REG_W_ALIGNMENT",
        flag_type="float",
        lifecycle="experimental",
        domain="generation",
        description=(
            "对齐腿权重 (R5 c1/c2)。**P2 总开关**:>0 才激活对齐腿 (跑 R5 + 计入"
            "penalty);默认 0 = P2 休眠、零 LLM 成本。对齐腿单向:只增不减 penalty。"
        ),
    ),
    "CODE_GEN_SOFT_REG_COMPLEXITY_C0": FlagSpec(
        name="CODE_GEN_SOFT_REG_COMPLEXITY_C0",
        flag_type="float",
        lifecycle="experimental",
        domain="generation",
        description="复杂度斜坡下限:complexity_score (=n_ops+0.5*n_fields) 在此及以下惩罚为 0。",
    ),
    "CODE_GEN_SOFT_REG_COMPLEXITY_CMAX": FlagSpec(
        name="CODE_GEN_SOFT_REG_COMPLEXITY_CMAX",
        flag_type="float",
        lifecycle="experimental",
        domain="generation",
        description="复杂度斜坡上限:complexity_score 达此值惩罚=1 (之后饱和)。",
    ),
    # --- SoftReg-P2 对齐腿 (R5 c1/c2);总开关 = W_ALIGNMENT>0 ---
    "CODE_GEN_SOFT_REG_ALIGNMENT_TOPK": FlagSpec(
        name="CODE_GEN_SOFT_REG_ALIGNMENT_TOPK",
        flag_type="int",
        lifecycle="experimental",
        domain="generation",
        description=(
            "soft 模式下对 effective-P(PASS) 排名 top-K 的候选跑 R5 对齐判 "
            "(每候选 2 次 LLM)。0=关闭对齐腿。仅在 W_ALIGNMENT>0 时生效。"
        ),
    ),
    "CODE_GEN_SOFT_REG_ALIGNMENT_SHADOW_SAMPLE": FlagSpec(
        name="CODE_GEN_SOFT_REG_ALIGNMENT_SHADOW_SAMPLE",
        flag_type="int",
        lifecycle="experimental",
        domain="generation",
        description=(
            "shadow 模式下每轮跑 R5 的候选数 (小采样,只为攒 alignment 分布做"
            "校准)。0=shadow 不跑 R5。仅在 W_ALIGNMENT>0 时生效。"
        ),
    ),
    # --- G8 Phase A — Hypothesis forest cross-task reference ---
    "ENABLE_HYPOTHESIS_FOREST_REUSE": FlagSpec(
        name="ENABLE_HYPOTHESIS_FOREST_REUSE",
        flag_type="bool",
        lifecycle="experimental",
        domain="generation",
        description=(
            "G8 Phase A (2026-05-19, RD-Agent hypothesis-forest):node_hypothesis "
            "在 LLM 生成前调 HypothesisService.fetch_cross_task_promoted 拉同 "
            "region 内 top-K (pass_count ≥ HYPOTHESIS_FOREST_MIN_PASS_COUNT "
            "AND sharpe_avg ≥ HYPOTHESIS_FOREST_MIN_SHARPE_AVG) PROMOTED/ACTIVE "
            "hypothesis,经 P2-B pillar_hint 过滤后注入 prompt 作为 reference。"
            "LLM 可选 extend 或 propose new。OFF 时 byte-for-byte legacy 渲染。"
            "Soft-fail:fetch 异常 → cross_task_hypotheses=[] → prompt 不变。"
            "Phase C(7d+ obs)再考虑 hard reuse(parent_hypothesis_id 跨 task chain)。"
        ),
    ),
    # --- G2 Phase A — per-call LLM cost telemetry ---
    "ENABLE_COST_TELEMETRY": FlagSpec(
        name="ENABLE_COST_TELEMETRY",
        flag_type="bool",
        lifecycle="dormant",
        domain="llm-routing",
        description=(
            "G2 Phase A (2026-05-19): per-LLM-call row INSERT 到 llm_call_log "
            "覆盖普通 round + R1b retry/mutate 全路径 (R1b 路径仍保留 "
            "r1b_retry_log 写入,llm_call_log 用作全局聚合源)。task_id / "
            "run_id / round_idx / node_key 经 contextvar 推送,round 末 batch "
            "flush。Soft-fail:tracker 异常永不打断 LLM 调用。cost_usd 由 "
            "tokens × LLM_PRICING_USD_PER_1K_TOKENS 估算 (Phase A 用 blended "
            "rate,Phase B 可拆 prompt/completion)。flag OFF 时纯 no-op,零"
            "采集开销。Phase C ≥7d 观察后 promote 到 cost-aware throttling。"
        ),
    ),
    # Retired 2026-05-19 — ENABLE_HIERARCHICAL_RAG_CACHE + ENABLE_R5_L2_RANKING
    # subsumed into the main ENABLE_HIERARCHICAL_RAG switch (cache always on,
    # L2 R5 ranking always on). DB orphan override rows silently no-op.
    # phase15-D PR3c (2026-05-18): ENABLE_CASCADE_LEGACY flag retired —
    # cascade dispatch + router + watchdog probe now refuse
    # unconditionally. Removed from SUPPORTED_FLAGS so the override UI
    # stops showing it. Existing FeatureFlagOverride rows for this name
    # silently no-op (orphan flag warned at load time by
    # _load_overrides_into_cache).
    # --- R8 query-level telemetry (per-call layer_hits + cache_hit row) ---
    "ENABLE_R8_QUERY_LOG": FlagSpec(
        name="ENABLE_R8_QUERY_LOG",
        flag_type="bool",
        lifecycle="experimental",
        domain="rag",
        description=(
            "R8 follow-up (2026-05-18): per-query layer_hits + cache_hit + "
            "had_failure_tree_elevation row in r8_query_log. Default OFF — "
            "zero overhead on hot RAG path until promoted. Typical use: "
            "enable for 7d obs window after ENABLE_HIERARCHICAL_RAG flip "
            "to measure L0/L1/L2/L3 fall-through patterns. Dedicated "
            "AsyncSession soft-fail INSERT — DB error never aborts RAG."
        ),
    ),
    # --- R9 simulation cache ---
    "ENABLE_SIMULATION_CACHE": FlagSpec(
        name="ENABLE_SIMULATION_CACHE",
        flag_type="bool",
        lifecycle="operational",
        domain="evaluation",
        description=(
            "Phase 3 R9 (master plan §4.5): cache BRAIN sim results keyed on "
            "(region, universe, expression, settings_json) sha256[:64]。"
            "Hit → skip BRAIN call return cached; miss → BRAIN sim + write cache。"
            "Est. 40-60% BRAIN cost reduction (cascade T2/T3 wrapper dup + flat "
            "dataset cycling)。TTL SIMULATION_CACHE_TTL_DAYS default 14d;beyond TTL "
            "treated as miss but row kept for analytics。"
            "默认 OFF — flag ON 后 cached_simulate_batch wraps brain.simulate_batch。"
            "Soft-fail: cache DB error → fall back to direct BRAIN call (never blocks)。"
        ),
    ),
    "ENABLE_REGIME_MONITOR": FlagSpec(
        name="ENABLE_REGIME_MONITOR",
        flag_type="bool",
        lifecycle="operational",
        domain="regime",
        description=(
            "[regime-turn 探针 — greenfield branch B] 池暂停期周期 re-sim 已提交赢家"
            "(+backlog 抽样)于当前数据(rolling test_period),老边际恢复=regime 转"
            "→重启生产。beat backend.tasks.run_regime_monitor,结果存 Redis "
            "regime_monitor:latest;口径 current IS 非 OS,仅判 WHEN 重启不判 WHAT 提交。"
        ),
    ),
    "ENABLE_RESIM_BACKLOG": FlagSpec(
        name="ENABLE_RESIM_BACKLOG",
        flag_type="bool",
        lifecycle="operational",
        domain="submit",
        description=(
            "[backlog 当前数据 re-sim — on-demand] submit-backlog 页按需把一个/一批"
            "候选在当前数据 re-sim,对比冻结-IS baseline 判衰减(stable/soft/hard/"
            "margin_killed/unmeasurable_cached)。只读(simulate 不 submit)。端点 "
            "POST/GET /ops/submit-backlog/resim-current,celery resim_backlog_current。"
            "口径 current IS 非 OS;持平≠该提交(仍需过 self_corr<0.7 + marginal)。"
        ),
    ),
    # --- Phase 2 R7: Co-STEER self-correct 半接受 ---
    "ENABLE_SELF_CORRECT_SEMI_ACCEPT": FlagSpec(
        name="ENABLE_SELF_CORRECT_SEMI_ACCEPT",
        flag_type="bool",
        lifecycle="experimental",
        domain="generation",
        description=(
            "Phase 2 R7 (rd_agent Co-STEER): SELF_CORRECT 节点 LLM 修正后 "
            "用 alpha_semantic_validator 快速 re-validate,新版本 VALID OR "
            "严格少 hard findings 才 overwrite;否则保原 expression + "
            "标 metrics['_r7_self_correct_rejected']=True。"
            "防 LLM 把一个 broken expression 改成另一个 broken expression。"
            "Reject 仍占 1 次 retry_count,LangGraph max_retries 行为不变。"
        ),
    ),
    # --- Phase 2 R10: Family-cap (Hubble v2) ---
    "ENABLE_FAMILY_CAP": FlagSpec(
        name="ENABLE_FAMILY_CAP",
        flag_type="bool",
        lifecycle="experimental",
        domain="evaluation",
        description=(
            "Phase 2 R10 (Hubble v2 Table 1): 同 pillar 同 family "
            "(operator-sequence signature) 只保留 top-K=2 by score。"
            "防止一个 op pipeline 在 evaluation batch 刷榜挤掉异质 alpha。"
            "evaluation node R5 hook 之后调 family_classifier.apply_family_cap,"
            "超出 K 的只 stamp metrics['_r10_family_cap_dropped']=True;FAIL "
            "transition 走 evaluation node 末尾的 finalize pass(B3 Sprint 2 重构 — "
            "R10 + R10-v2 stamps 合并 → quality_status=FAIL,允许 7d 互验 SQL 分别"
            "计算两机制 false-positive rate)。误杀时 flag flip OFF 或 FAMILY_CAP_TOP_K=5 放宽。"
        ),
    ),
    # --- Phase 4 Sprint 0 (2026-05-19) ---
    "ENABLE_LLM_API_CIRCUIT": FlagSpec(
        name="ENABLE_LLM_API_CIRCUIT",
        flag_type="bool",
        lifecycle="operational",
        domain="llm-routing",
        description=(
            "Phase 4 PR0:LLM provider(DeepSeek/Anthropic)outage 熔断。"
            "60s 内连续 LLM_API_CIRCUIT_FAIL_THRESHOLD(默认 5)次 5xx/timeout "
            "→ trip 300s 冷却 → 任何 LLM caller fast-fail return success=False/"
            "error='llm_api_circuit_open',不发实际 HTTP。任何 success → "
            "立即 clear。Default ON(防御机制 default ON 与 BRAIN_AUTH_CIRCUIT "
            "一致)。Soft-fail Redis blip 永不 brown-out。"
        ),
    ),
    "ENABLE_R8_L0": FlagSpec(
        name="ENABLE_R8_L0",
        flag_type="bool",
        lifecycle="operational",
        domain="rag",
        description=(
            "Phase 4 PR0.5:R8 hierarchical RAG L0(exact pattern_hash match)"
            "选择性 sub-flag。Default ON(R8 4-layer 全部 LIVE)。"
            "R12 LLM_MODE=assistant sentinel ON 时,全局 set False 仅 skip L0,"
            "保留 L1 pillar / L2 family / L3 field。双 entry skip:"
            "`backend/agents/hierarchical_rag.py:query_hierarchical` 主 entry + "
            "`backend/agents/services/rag_service.py:query()` legacy entry。"
        ),
    ),
    # --- Persistence-Ontology (P1-P4 2026-05-19, plan v1.3.1) ---
    "ENABLE_FAIL_ALPHA_PERSIST": FlagSpec(
        name="ENABLE_FAIL_ALPHA_PERSIST",
        flag_type="bool",
        lifecycle="operational",
        domain="evaluation",
        description=(
            "P1:把 BRAIN 接受过的 FAIL alpha (alpha_id 存在 + 真 sim 成功)"
            "写 alphas 表;OFF 时回到 PASS-only legacy 行为。修复 mining-time"
            "write filter — alpha_failures.QUALITY_CHECK_FAILED 不再丢 BRAIN"
            "handle。Flip ON 后 ≥1h 才能跑 P2 backfill 脚本(脚本自检)。"
        ),
    ),
    # --- A1.2 R12 LLM_MODE=assistant (Sprint 1, 2026-05-20) ---
    "ENABLE_LLM_ASSISTANT_MODE": FlagSpec(
        name="ENABLE_LLM_ASSISTANT_MODE",
        flag_type="bool",
        lifecycle="dormant",
        domain="generation",
        description=(
            "Phase 4 R12:工业 8 家共识(Citadel/Two Sigma/Bridgewater AIA)— "
            "LLM 做 research assistant 不做 expression-author。Default OFF。"
            "Set True 时联动 4 LLM_ASSISTANT_SENTINEL_FLAGS 强制 False "
            "(G8 forest reuse / R8 L0 / G3 / R9 cache)"
            ",audit 留 sentinel_trigger_for 标识便于 restore。OFF 仅关 kill "
            "switch — sentinel flag 不会自动恢复,需 POST /ops/llm-mode/"
            "restore-sentinel 显式回滚。task.config['llm_mode']='assistant' 控制 "
            "per-task opt-in。"
        ),
    ),
    # --- B1 R11 alpha_capacity_estimator (Sprint 2, 2026-05-20) ---
    "ENABLE_CAPACITY_SCORE": FlagSpec(
        name="ENABLE_CAPACITY_SCORE",
        flag_type="bool",
        lifecycle="experimental",
        domain="evaluation",
        description=(
            "Phase 4 B1 R11:工业派 capacity-cap(RenTec $10B / Bridgewater $5B "
            "软上限)纳入 composite_score 第 5 维。Default OFF — 翻 ON 时 "
            "evaluate_alpha_comprehensive composite normalize sum=1.0(原 4 维 × "
            "0.9 + capacity × CAPACITY_SCORE_WEIGHT),calculate_alpha_score 加 "
            "capacity 项。capacity_estimator.estimate(alpha) 用 ADV × universe × "
            "(1 - turnover_decay) 粗估 USD,log-scale 5 桶 normalize [0,1]。"
            "PASS alpha persist 前 stamp `alphas.capacity_usd_estimate`(Alembic "
            "k2b3c4d5e6f7)。Phase A 真效果,不是 stamp-only。"
        ),
    ),
    "CAPACITY_SCORE_WEIGHT": FlagSpec(
        name="CAPACITY_SCORE_WEIGHT",
        flag_type="float",
        lifecycle="experimental",
        domain="evaluation",
        description=(
            "R11 capacity 维度 weight。Default 0.10 — composite normalize 时原 4 "
            "维 weight × (1 - 0.10) + capacity × 0.10 = 1.0。调高 → capacity 主导,"
            "调低 → 接近原 4 维 baseline。验收期 obs 7d 后可 calibrate。"
        ),
    ),
    # --- B3 R10-v2 family hard-ban shadow (Sprint 2, 2026-05-20) ---
    "ENABLE_FAMILY_HARD_BAN": FlagSpec(
        name="ENABLE_FAMILY_HARD_BAN",
        flag_type="bool",
        lifecycle="experimental",
        domain="evaluation",
        description=(
            "Phase 4 B3 R10-v2:同 (pillar, family) alpha pairwise PnL correlation ≥ "
            "FAMILY_BAN_MIN_PAIRWISE_CORR 时 stamp metrics['_r10v2_hard_banned']"
            "=True。Shadow mode:不直接 set FAIL,evaluation 末 finalize pass "
            "scan stamp 后统一 set FAIL。允许 R10/R10-v2 双 stamp 共存,7d obs "
            "后跑 plan v5 §6.10 互验 SQL 比较 false-positive rate 决定胜出者。"
            "⚠️ Default OFF + **DOA without Sprint 3 follow-up wire**:"
            "  apply_family_hard_ban 读 state.r10v2_pnl_corr_matrix (Optional[pd."
            "DataFrame]),但 producer 还没写 — flag ON 时 evaluation 块仅 DEBUG-"
            "log skip。Sprint 3 follow-up:在 node_correlation_check / "
            "node_evaluate 上游加 batch fetch + pandas .corr 写到该字段。"
            "operator 先把 τ 用 calibrate_r10_pairwise_corr.py 校准 region-"
            "specific 之后再 flip。"
        ),
    ),
    "FAMILY_BAN_MIN_PAIRWISE_CORR": FlagSpec(
        name="FAMILY_BAN_MIN_PAIRWISE_CORR",
        flag_type="float",
        lifecycle="experimental",
        domain="evaluation",
        description=(
            "R10-v2 hard-ban pairwise PnL correlation threshold τ ∈ [0, 1]。"
            "Default 0.65 = 保守初值;scripts/calibrate_r10_pairwise_corr.py "
            "输出 region 内 intra-family p95/p99 中位会 calibrate 到 region-specific "
            "(USA TOP3000 通常 0.7-0.8,emerging market 0.5-0.6)。调高 → 更宽容,"
            "调低 → 更激进 ban。"
        ),
    ),
    # --- B2 R13 factor_decomposition shadow (Sprint 2, 2026-05-20) ---
    "ENABLE_FACTOR_LENS": FlagSpec(
        name="ENABLE_FACTOR_LENS",
        flag_type="bool",
        lifecycle="experimental",
        domain="evaluation",
        description=(
            "Phase 4 B2 R13:OLS 分解 PASS alpha daily returns 对 5 个 style "
            "factor(size/value/momentum/quality/low_vol)产 residual_sharpe + "
            "factor_exposures。Default OFF。三阶段 rollout:shadow→soft→hard "
            "(per FACTOR_LENS_MODE)。Shadow 模式 stamp 不改 quality_status,"
            "soft 模式 residual<τ → PASS_PROVISIONAL,hard 模式 residual<τ → "
            "FAIL。数据依赖 backend/data/factor_returns_snapshot/{region}."
            "parquet — operator 月维护。flag ON 但 snapshot 缺失 → soft-fall "
            "skip(无 exception)。"
        ),
    ),
    "FACTOR_LENS_MODE": FlagSpec(
        name="FACTOR_LENS_MODE",
        flag_type="string",
        lifecycle="experimental",
        domain="evaluation",
        description=(
            "R13 rollout 阶段 — 'shadow'(default)/ 'soft' / 'hard'。验收期"
            "(per [[feedback_light_wiring_deferred_gate]]):shadow 7d obs ≥30 "
            "alpha residual → flip soft → 7d obs PASS_PROV 中 ≥80% can_submit "
            "→ flip hard。"
        ),
    ),
    "FACTOR_LENS_RESIDUAL_SHARPE_MIN": FlagSpec(
        name="FACTOR_LENS_RESIDUAL_SHARPE_MIN",
        flag_type="float",
        lifecycle="experimental",
        domain="evaluation",
        description=(
            "R13 soft/hard 模式的 residual_sharpe 阈值 τ。default 0.5 — "
            "alpha 经 style factor neutralize 后年化 sharpe 仍 ≥0.5 才认为"
            "有 idiosyncratic 编辑。scripts/calibrate_r13_threshold.py 可"
            "根据 7d obs 数据校准 region-specific 值(fast-follow)。"
        ),
    ),
    # --- B5 R8-v3 cognitive layer 7-layer (Sprint 3, 2026-05-20) ---
    "ENABLE_COGNITIVE_LAYER_PROMPT": FlagSpec(
        name="ENABLE_COGNITIVE_LAYER_PROMPT",
        flag_type="bool",
        lifecycle="experimental",
        domain="generation",
        description=(
            "Phase 4 B5 R8-v3:每 round 选 1 个 cognitive layer(7 选 1,"
            "macro/behavioral/technical/value/microstructure/cross_sectional/"
            "time_series_mean_reversion)splice 进 hypothesis prompt。Default "
            "OFF — flag ON 时 node_hypothesis fetch + 注入 layer block + "
            "stamp alpha.metrics['cognitive_layer_id']。R12 sentinel 联动:"
            "R8 L0 disable 时 R8-v3 仍可独立 LIVE(L1/L2/L3 + cognitive "
            "layer 共存)。"
        ),
    ),
    "COGNITIVE_LAYER_SELECT_MODE": FlagSpec(
        name="COGNITIVE_LAYER_SELECT_MODE",
        flag_type="string",
        lifecycle="experimental",
        domain="generation",
        description=(
            "R8-v3 layer 选择策略:'round_robin'(default,公平轮转)/ "
            "'bandit'(Beta-Bernoulli Thompson sample exploit > 0.5 优势 "
            "layer)/ 'deficit_aware'(挑 PASS rate 最低 boost coverage)。"
            "运行 ≥7d 累积 bandit state 后建议 flip 到 'bandit'。"
        ),
    ),
    "COGNITIVE_LAYER_PROMPT_TOKEN_BUDGET": FlagSpec(
        name="COGNITIVE_LAYER_PROMPT_TOKEN_BUDGET",
        flag_type="int",
        lifecycle="experimental",
        domain="generation",
        description=(
            "R8-v3 hypothesis prompt 总 token 上限。Default 8000 — 超过时按 "
            "drop order(dedup_blacklist → cross_task_forest → macro_narrative)"
            "删除上下文块。cognitive_layer 块绝不删(R8-v3 的核心)。"
        ),
    ),
    # --- A5.1 G10 logic-as-asset PR1 (Sprint 3, 2026-05-20) ---
    "ENABLE_G10_LOGIC_DISTILL": FlagSpec(
        name="ENABLE_G10_LOGIC_DISTILL",
        flag_type="bool",
        lifecycle="experimental",
        domain="kb",
        description=(
            "Phase 4 A5.1:Sunday 03:00 SH 周末 cron — 过去 7d PASS alpha "
            "按 (pillar, region) 分组,LLM 蒸馏成 1-3 句 logic 总结,写 "
            "distilled_logic_library 表(Alembic n5e6f7g8h9i0)。Default OFF。"
            "PR2 (Sprint 4) 注入回 hypothesis prompt 形成正反馈;PR1 只建库。"
            "Cost cap LOGIC_DISTILL_MAX_COST_USD_PER_WEEK $5。"
        ),
    ),
    "LOGIC_DISTILL_MAX_COST_USD_PER_WEEK": FlagSpec(
        name="LOGIC_DISTILL_MAX_COST_USD_PER_WEEK",
        flag_type="float",
        lifecycle="experimental",
        domain="kb",
        description=(
            "G10 周末蒸馏 LLM cost 上限。Default $5/周。超过即停止 dispatch "
            "新 bucket,fallback 是保留上周残余条目(staleness 在 "
            "/ops/g10/logic-library 显示)。"
        ),
    ),
    "LOGIC_DISTILL_TOP_K_PER_GROUP": FlagSpec(
        name="LOGIC_DISTILL_TOP_K_PER_GROUP",
        flag_type="int",
        lifecycle="experimental",
        domain="kb",
        description=(
            "G10 每 (pillar, region) bucket 取 sharpe DESC top-K alpha 进 "
            "distill prompt。Default 10 — LLM context size 与 distill 质量 "
            "trade-off,调大 → 上下文丰富但 cost 高。"
        ),
    ),
    "LOGIC_DISTILL_MIN_PASS_COUNT": FlagSpec(
        name="LOGIC_DISTILL_MIN_PASS_COUNT",
        flag_type="int",
        lifecycle="experimental",
        domain="kb",
        description=(
            "G10 bucket < N PASS alpha 时 skip 蒸馏(数据不足以画出 pattern)。"
            "Default 3 — production validate 后可调。"
        ),
    ),
    "LOGIC_DISTILL_LOOKBACK_DAYS": FlagSpec(
        name="LOGIC_DISTILL_LOOKBACK_DAYS",
        flag_type="int",
        lifecycle="experimental",
        domain="kb",
        description=(
            "G10 distill 回溯天数。Default 7(weekly cadence)。"
        ),
    ),
    "LOGIC_DISTILL_SIMILARITY_THRESHOLD": FlagSpec(
        name="LOGIC_DISTILL_SIMILARITY_THRESHOLD",
        flag_type="float",
        lifecycle="experimental",
        domain="kb",
        description=(
            "G10 PR2 refine 阶段判断 logic entry 与上周是否近重复的 Jaccard "
            "阈值。Default 0.70 — token 集合 70% 重叠就视为 stale 不写。"
        ),
    ),
    # --- A5.2 G10 PR2 (Sprint 4, 2026-05-20) - prompt injection ---
    "ENABLE_G10_LOGIC_INJECT": FlagSpec(
        name="ENABLE_G10_LOGIC_INJECT",
        flag_type="bool",
        lifecycle="experimental",
        domain="kb",
        description=(
            "Phase 4 A5.2:G10 distilled_logic_library 注入回 hypothesis "
            "prompt(独立 block 渲染 + 与 R8-v3 cognitive layer 并存)。"
            "Default OFF。node_hypothesis 在 G8 forest fetch 之后 fetch "
            "active 条目(retired_at IS NULL,region+pillar match),5 entry "
            "拼成 distilled_logic_block splice 进 prompt template。OFF 路径"
            "byte-for-byte legacy(空 block → 空 splice)。"
        ),
    ),
    "G10_LOGIC_INJECT_TOP_K": FlagSpec(
        name="G10_LOGIC_INJECT_TOP_K",
        flag_type="int",
        lifecycle="experimental",
        domain="kb",
        description=(
            "G10 inject 到 hypothesis prompt 的 entry 上限。Default 5。"
            "考虑 token 预算 + 信号噪声,过多 entry → prompt 稀释 + LLM 选择困难。"
        ),
    ),
    # --- B4.1 G3-v2 grammar-aware (Sprint 4, 2026-05-20) ---
    "ENABLE_GRAMMAR_VALIDATOR": FlagSpec(
        name="ENABLE_GRAMMAR_VALIDATOR",
        flag_type="bool",
        lifecycle="experimental",
        domain="generation",
        description=(
            "Phase 4 B4.1 G3-v2:lark-based 语法子集 validator,catch 结构性"
            "malformed alpha(unbalanced parens / unexpected tokens)。Default "
            "OFF。新 code path 不动 G3 shadow code(ENABLE_AST_ORIGINALITY_GATE "
            "@deprecated_pending_r12_decision,B4.2 Sprint 5 条件性 retire)。"
            "validate fail → retry_with_whole_output_hint → node_code_gen 重发 "
            "GRAMMAR_VALIDATOR_RETRY_MAX 次。lark 未安装时 degrade-open(返回 "
            "ok=True 让 caller 走 legacy 检查)。"
        ),
    ),
    # --- Breadth: dataset-steering value bandit (2026-05-22) ---
    # --- Optimization closure Stage A (2026-05-28) ---
    "ENABLE_OPTIMIZATION_LOOP": FlagSpec(
        name="ENABLE_OPTIMIZATION_LOOP",
        flag_type="bool",
        lifecycle="operational",
        domain="submit",
        description=(
            "Optimization closure Stage A — 6h beat scans 1230 delay-1 near-gate "
            "alphas, runs SettingsSweepGenerator (10 variants × 10 candidates × 4 "
            "cycles = 400 sim/day), winners queue into ops/submit-backlog. "
            "NEVER auto-submits (Stage A SubmitPolicy returns 'queue' for every "
            "winner). 14d GO/STOP gate via /ops/optimization/cycles: conversion "
            "rate >20% → Stage B; <10% → STOP (selection limited per "
            "competitive_analysis_v3). Plan: docs/optimization_closure_plan_v1_"
            "2026-05-28.md."
        ),
    ),
    "ENABLE_AUTO_SUBMIT": FlagSpec(
        name="ENABLE_AUTO_SUBMIT",
        flag_type="bool",
        lifecycle="operational",
        domain="submit",
        description=(
            "自动提交 master 开关(默认 OFF)。ON 后 6h beat 跑正交抽干守门栈;"
            "AUTO_SUBMIT_MODE(.env,非热翻)='shadow'(默认)只记 would-submit 名单不真发,"
            "'live' 才按 daily_cap 真提交。守门栈层层 fail-closed(can_submit/红线/margin≥5bps/"
            "推荐=SUBMIT/additive/正交/recon supported),最终仍由 submit_alpha 兜底。"
            "热翻此开关可即时启停(含紧急关停);名单见 GET /ops/auto-submit/audit。"
            "设计 docs/auto_submit_design_2026-06-04.md。"
        ),
    ),
    "ENABLE_CAN_SUBMIT_REFRESH": FlagSpec(
        name="ENABLE_CAN_SUBMIT_REFRESH",
        flag_type="bool",
        lifecycle="operational",
        domain="submit",
        description=(
            "can_submit 周期刷新 beat(默认 OFF)。每 6h :50 对 can_submit=True/未提交"
            " backlog 按最旧戳优先重查 BRAIN,保 can_submit 验证 + _brain_can_submit_at"
            " 新鲜度 <自动提交 G4 窗(12h),并把 BRAIN 现已拒绝(如 self_corr 升过 0.7)"
            "的 alpha 降级出 backlog。只读 BRAIN GET、1 req/s,单飞锁。auto-submit live"
            "前应开启,否则 G4 新鲜戳 ~12h 后过期 → would_submit 掉回 0。"
        ),
    ),
    "ENABLE_DATASET_VALUE_BANDIT": FlagSpec(
        name="ENABLE_DATASET_VALUE_BANDIT",
        flag_type="bool",
        lifecycle="operational",
        domain="breadth",
        description=(
            "Tier A 数据集导流 bandit (plan dataset_steering_bandit_plan_v3)。"
            "把 dormant DatasetMetadata.mining_weight 变 discounted Beta-Bernoulli "
            "后验:reward = S_d/T_d (S_d=#(can_submit & _iqc_marginal.delta_score>0),"
            "T_d=#真 BRAIN sims 排除 _pre_brain_skip),pull-indexed 衰减 g=γ^T_d。"
            "ON 后 (a) 日频 beat run_dataset_weight_refresh 算 S_d/T_d → 更新后验 "
            "→ 采样 θ+floor 写回 mining_weight;(b) _run_flat_iteration 按 "
            "mining_weight 加权采样 dataset(取代等概率 round-robin)→ 真频率导流"
            "off 被挖烂的 pv1、向高边际价值+欠挖正交源。OFF (default) → beat no-op + "
            "FLAT round-robin = byte-for-byte legacy。未解析 dataset_id 行排除。"
            "v1 目标=去优先 pv1+探索(非 submittable↑);残差 reward bump = phase-2。"
        ),
    ),
    # --- Phase 2 hypothesis-centric level (2026-05-22: make it refreshable) ---
    "HYPOTHESIS_CENTRIC_LEVEL": FlagSpec(
        name="HYPOTHESIS_CENTRIC_LEVEL",
        flag_type="int",
        lifecycle="dormant",
        domain="generation",
        description=(
            "typed Hypothesis 生命周期等级。0=legacy(无 typed hypothesis);"
            ">=1=node_hypothesis 注入假设;>=2=持久化 Hypothesis 行 + 把 alpha "
            "链到 hypothesis_id(B4/B5 attribution + R1b CoSTEER 变异的前置)。"
            "**2026-05-22 注册为可刷新**:此前只活在 .env、非受管 flag → worker "
            "启动时读一次,在 .env bump 前起的 worker 永远 level=0 → FLAT alpha "
            "全 hypothesis_id=NULL。注册后可经 /ops/flags 热改 + refresher 60s 传播,"
            "免重启。无 override 行时仍回退 .env/默认值(零行为变化)。FLAT task 现在"
            "创建时把此值钉进 config[hypothesis_centric_variant] 免疫 worker 漂移。"
        ),
    ),
    # --- LLM-Routing (PR1, 2026-05-29): per-functional-block model routing ---
    "ENABLE_PER_FUNCTION_LLM_ROUTING": FlagSpec(
        name="ENABLE_PER_FUNCTION_LLM_ROUTING",
        flag_type="bool",
        lifecycle="operational",
        domain="llm-routing",
        description=(
            "按功能块(node_key)路由到不同 LLM 模型的总开关。OFF=所有 node 走全局默认"
            "模型(byte-for-byte legacy)。ON=resolve_model_for 按 LLM_FUNCTION_MODEL_MAP "
            "为每个 node 选模型(热路径 hypothesis/code_gen 用质量优,辅助路径用便宜快)。"
        ),
    ),
    "LLM_FUNCTION_MODEL_MAP": FlagSpec(
        name="LLM_FUNCTION_MODEL_MAP",
        flag_type="json",
        lifecycle="operational",
        domain="llm-routing",
        description=(
            "node_key → {model, provider_ref?, provider?, base_url?, api_key_ref?, "
            "thinking_effort?} 路由映射。前端编辑此 json;resolve_model_for 直读 "
            "_flag_override_cache。provider_ref 指向 LLM_PROVIDERS 里的命名厂商,resolve "
            "时展开成 provider/base_url/api_key_ref(内联字段仍兼容旧 JSON)。畸形 entry "
            "(非 dict/缺 model/provider 非法)自动回退该 node 默认,不影响其它。"
        ),
    ),
    "LLM_PROVIDERS": FlagSpec(
        name="LLM_PROVIDERS",
        flag_type="json",
        lifecycle="operational",
        domain="llm-routing",
        description=(
            "命名 LLM 厂商注册表 name → {label, sdk(openai/anthropic), base_url}。"
            "预配置 endpoint;密钥不在此处,走加密 CredentialsService(凭证键 "
            "llm_provider_<name>)。路由表用 provider_ref 引用。前端配置中心「LLM 厂商」"
            "tab 编辑(经 /config/llm-providers 端点)。resolve_model_for 直读缓存。"
        ),
    ),
    "LLM_AVAILABLE_MODELS": FlagSpec(
        name="LLM_AVAILABLE_MODELS",
        flag_type="json",
        lifecycle="operational",
        domain="llm-routing",
        description="前端下拉的可选模型清单(JSON 数组)。仅 UI 用,不影响路由决策。",
    ),
}


# Redis hash key + TTL — only used to bump cross-process refreshers; DB is
# authoritative.
REDIS_FLAGS_KEY = "aiac:feature_flags:v1"
REDIS_FLAGS_BUMP_KEY = "aiac:feature_flags:bump"
REDIS_FLAGS_TTL = 86400  # 24h


# ---------------------------------------------------------------------------
# Read/write models exposed to router
# ---------------------------------------------------------------------------

@dataclass
class FlagState:
    """Effective state of one flag returned to /ops/flags."""
    name: str
    flag_type: str
    lifecycle: str
    domain: str
    description: str
    env_default: Any                  # value from Settings before override
    override_value: Optional[Any]     # decoded DB value, or None if no override
    effective_value: Any              # what callers actually see
    source: str                       # "env" | "runtime-override" | "default"
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = None
    note: Optional[str] = None


# ---------------------------------------------------------------------------
# Module-level cache — single source of truth lives in backend/config.py
# ---------------------------------------------------------------------------
# Re-exported here so call sites can keep importing from this service module
# without knowing about the config-internal implementation. The
# Settings.__getattribute__ hook reads the same dict, so write-through here
# is visible to settings.ENABLE_X immediately in the same process.
from backend.config import _flag_override_cache  # noqa: E402  (intentional late import)


def _decode_value(raw: str, flag_type: str) -> Any:
    """Decode a JSON-encoded `flag_value` string per declared type."""
    parsed = json.loads(raw)
    if flag_type == "bool":
        return bool(parsed)
    if flag_type == "int":
        return int(parsed)
    if flag_type == "float":
        return float(parsed)
    if flag_type == "str":
        return str(parsed)
    return parsed  # json — keep as-is


def _encode_value(value: Any, flag_type: str) -> str:
    """JSON-encode a value, validating it matches the declared type.

    Note bool is a subclass of int in Python — guard int/float against
    accidentally accepting True/False (would silently encode as JSON `true`
    and decode back as 1, drifting behaviour without an error).
    """
    if flag_type == "bool" and not isinstance(value, bool):
        raise ValueError(f"expected bool, got {type(value).__name__}")
    if flag_type == "int" and (isinstance(value, bool) or not isinstance(value, int)):
        raise ValueError(f"expected int, got {type(value).__name__}")
    if flag_type == "float" and (isinstance(value, bool) or not isinstance(value, (int, float))):
        raise ValueError(f"expected number, got {type(value).__name__}")
    if flag_type == "str" and not isinstance(value, str):
        raise ValueError(f"expected str, got {type(value).__name__}")
    # json — anything serializable
    return json.dumps(value)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class FeatureFlagService(BaseService):
    """DB-backed feature flag store with Redis cross-process invalidation.

    The router layer (backend/routers/ops.py) constructs one of these per
    request. Background refreshers (lifespan + worker_process_init) load
    overrides directly via :meth:`load_overrides_into_cache` without going
    through the router.
    """

    # ---- read -------------------------------------------------------------

    async def list_all(self) -> List[FlagState]:
        """Return effective state for every supported flag.

        This is the only call site that reads `Settings` env defaults
        directly — we want the raw env value, not the post-override one,
        so the UI can show both sides side-by-side.
        """
        from backend.config import settings  # lazy to avoid import cycle

        # Pull all overrides in a single query keyed by name
        rows = (await self.db.execute(select(FeatureFlagOverride))).scalars().all()
        overrides_by_name = {r.flag_name: r for r in rows}

        out: List[FlagState] = []
        for spec in SUPPORTED_FLAGS.values():
            # Read env default by going around our own __getattribute__ hook
            env_default = object.__getattribute__(settings, spec.name) \
                if hasattr(settings, spec.name) else None

            row = overrides_by_name.get(spec.name)
            if row is not None:
                try:
                    decoded = _decode_value(row.flag_value, spec.flag_type)
                    out.append(FlagState(
                        name=spec.name,
                        flag_type=spec.flag_type,
                        lifecycle=spec.lifecycle,
                        domain=spec.domain,
                        description=spec.description,
                        env_default=env_default,
                        override_value=decoded,
                        effective_value=decoded,
                        source="runtime-override",
                        updated_at=row.updated_at,
                        updated_by=row.updated_by,
                        note=row.note,
                    ))
                    continue
                except Exception as ex:
                    logger.warning(
                        "[feature_flag] decode failed for %s (%s) — falling back to env default: %s",
                        spec.name, row.flag_value, ex,
                    )

            out.append(FlagState(
                name=spec.name,
                flag_type=spec.flag_type,
                lifecycle=spec.lifecycle,
                domain=spec.domain,
                description=spec.description,
                env_default=env_default,
                override_value=None,
                effective_value=env_default,
                source="env" if env_default is not None else "default",
            ))
        return out

    async def get_one(self, name: str) -> Optional[FlagState]:
        """Return a single flag's FlagState (env_default + override + updated_at/by).

        Used by /ops/brain/role-state to fetch the last-switched timestamp for
        ENABLE_BRAIN_CONSULTANT_MODE. O(1) — direct row query, no full table scan.
        Returns None when name is not in SUPPORTED_FLAGS.
        """
        spec = SUPPORTED_FLAGS.get(name)
        if spec is None:
            return None
        from backend.config import settings  # lazy
        env_default = object.__getattribute__(settings, name) if hasattr(settings, name) else None
        row = (await self.db.execute(
            select(FeatureFlagOverride).where(FeatureFlagOverride.flag_name == name)
        )).scalar_one_or_none()
        if row is None:
            return FlagState(
                name=name, flag_type=spec.flag_type, lifecycle=spec.lifecycle, domain=spec.domain,
                description=spec.description, env_default=env_default,
                override_value=None, effective_value=env_default,
                source="env" if env_default is not None else "default",
            )
        try:
            decoded = _decode_value(row.flag_value, spec.flag_type)
        except Exception as ex:
            logger.warning(
                "[feature_flag] get_one decode failed for %s — falling back to env default: %s",
                name, ex,
            )
            return FlagState(
                name=name, flag_type=spec.flag_type, lifecycle=spec.lifecycle, domain=spec.domain,
                description=spec.description, env_default=env_default,
                override_value=None, effective_value=env_default,
                source="env" if env_default is not None else "default",
            )
        return FlagState(
            name=name, flag_type=spec.flag_type, lifecycle=spec.lifecycle, domain=spec.domain,
            description=spec.description, env_default=env_default,
            override_value=decoded, effective_value=decoded,
            source="runtime-override",
            updated_at=row.updated_at, updated_by=row.updated_by, note=row.note,
        )

    async def load_overrides_into_cache(self) -> Dict[str, Any]:
        """Pull every override row, decode, and replace ``_flag_override_cache``.

        Returns the new cache contents (mainly for diagnostics / tests).
        Call sites: lifespan startup, the 60s refresher loop, and the
        /ops/flags/refresh-all endpoint.

        DB outage tolerance: on failure we log + leave the existing cache
        in place. We never raise — the caller is the refresher loop and a
        crash there would silently kill the timer.
        """
        try:
            rows = (await self.db.execute(select(FeatureFlagOverride))).scalars().all()
        except Exception as ex:
            logger.warning("[feature_flag] cache refresh — DB read failed: %s", ex)
            return dict(_flag_override_cache)

        new_cache: Dict[str, Any] = {}
        for row in rows:
            spec = SUPPORTED_FLAGS.get(row.flag_name)
            if spec is None:
                # whitelist drift — orphan override row; ignore on read but
                # log so ops can clean it up
                logger.warning(
                    "[feature_flag] orphan override for unknown flag %r — ignoring",
                    row.flag_name,
                )
                continue
            try:
                new_cache[row.flag_name] = _decode_value(row.flag_value, spec.flag_type)
            except Exception as ex:
                logger.warning(
                    "[feature_flag] decode failed for %s — ignoring: %s",
                    row.flag_name, ex,
                )

        # Atomic replace — concurrent readers see either old or new dict,
        # never a half-built one.
        _flag_override_cache.clear()
        _flag_override_cache.update(new_cache)
        return dict(new_cache)

    async def list_audit(
        self,
        limit: int = 50,
        *,
        include_sentinel: bool = False,
    ) -> List[FeatureFlagAudit]:
        """Most recent flip / clear records for the audit Drawer.

        Phase 4 A1.2 (2026-05-20): default filters out R12 sentinel
        cascade rows (sentinel_trigger_for IS NOT NULL) so the ops
        Timeline isn't flooded by the 6-row burst on every R12 flip.
        Operator can pass ``include_sentinel=True`` to see them — useful
        when debugging "why did flag X go to False" right after R12 was
        flipped on.
        """
        stmt = select(FeatureFlagAudit)
        if not include_sentinel:
            stmt = stmt.where(FeatureFlagAudit.sentinel_trigger_for.is_(None))
        stmt = (
            stmt.order_by(desc(FeatureFlagAudit.created_at))
                .limit(min(max(limit, 1), 500))
        )
        return list((await self.db.execute(stmt)).scalars().all())

    # ---- write ------------------------------------------------------------

    @transactional
    async def set(
        self,
        name: str,
        value: Any,
        *,
        actor: str = "ops_console",
        note: Optional[str] = None,
    ) -> FlagState:
        """Set an override. Whitelist + type-check before write.

        The audit row is written in the same transaction as the UPSERT so
        either both succeed or neither does — there is no half-flipped
        state in the DB.
        """
        spec = SUPPORTED_FLAGS.get(name)
        if spec is None:
            raise ValueError(f"flag {name!r} is not in SUPPORTED_FLAGS whitelist")

        encoded = _encode_value(value, spec.flag_type)

        # SELECT-then-INSERT/UPDATE keeps this dialect-agnostic so the
        # in-memory aiosqlite test fixture works without a Postgres ON
        # CONFLICT special case. The unique index on flag_name still
        # protects us from duplicate inserts under concurrent writes —
        # a racing INSERT will fail with IntegrityError and the @transactional
        # decorator rolls back; the caller can retry.
        existing = (await self.db.execute(
            select(FeatureFlagOverride).where(FeatureFlagOverride.flag_name == name)
        )).scalar_one_or_none()
        old_encoded = existing.flag_value if existing else None

        if existing is None:
            self.db.add(FeatureFlagOverride(
                flag_name=name,
                flag_value=encoded,
                flag_type=spec.flag_type,
                updated_by=actor,
                note=note,
            ))
        else:
            existing.flag_value = encoded
            existing.flag_type = spec.flag_type
            existing.updated_by = actor
            existing.note = note

        self.db.add(FeatureFlagAudit(
            flag_name=name,
            old_value=old_encoded,
            new_value=encoded,
            action="set",
            actor=actor,
            note=note,
        ))

        # Local cache write-through — request thread sees new value
        # immediately even before the next refresher tick.
        _flag_override_cache[name] = value

        # Phase 4 A1.2 (2026-05-20): R12 LLM_MODE=assistant sentinel cascade.
        # When ENABLE_LLM_ASSISTANT_MODE is set True, force the 6
        # LLM_ASSISTANT_SENTINEL_FLAGS to False in the SAME transaction so
        # author-mode mechanisms (R1b mutate, G5 crossover, G8 forest
        # reuse, R8 L0, G3 originality, R9 sim cache) don't fire under an
        # assistant-mode hypothesis. Each forced flip writes an audit row
        # with sentinel_trigger_for=name so restore_sentinel() can reverse
        # the cascade later via a single WHERE clause.
        # Idempotent: setting False (or any value other than True) does
        # NOT cascade.
        if name == "ENABLE_LLM_ASSISTANT_MODE" and value is True:
            from backend.config import settings as _stg
            sentinel_list = list(
                getattr(_stg, "LLM_ASSISTANT_SENTINEL_FLAGS", []) or []
            )
            for sentinel_name in sentinel_list:
                sentinel_spec = SUPPORTED_FLAGS.get(sentinel_name)
                if sentinel_spec is None:
                    # Skip silently — sentinel list may include a flag
                    # that's been retired since A1.1 declared the list.
                    # The restore_sentinel path also handles partial cascades.
                    logger.warning(
                        "[ff sentinel] %s in LLM_ASSISTANT_SENTINEL_FLAGS "
                        "but not in SUPPORTED_FLAGS — skipping",
                        sentinel_name,
                    )
                    continue
                sentinel_encoded_off = _encode_value(False, sentinel_spec.flag_type)
                sentinel_existing = (await self.db.execute(
                    select(FeatureFlagOverride).where(
                        FeatureFlagOverride.flag_name == sentinel_name
                    )
                )).scalar_one_or_none()
                sentinel_old_encoded = (
                    sentinel_existing.flag_value if sentinel_existing else None
                )
                # No-op when sentinel already at False — but still write
                # an audit row so restore_sentinel can find + revert it.
                # restore_sentinel reads old_value=None as "no prior
                # override existed; restore = DELETE the row we just made".
                if sentinel_existing is None:
                    self.db.add(FeatureFlagOverride(
                        flag_name=sentinel_name,
                        flag_value=sentinel_encoded_off,
                        flag_type=sentinel_spec.flag_type,
                        updated_by=actor,
                        note=(
                            f"sentinel_cascade from ENABLE_LLM_ASSISTANT_MODE "
                            f"by {actor}"
                        ),
                    ))
                else:
                    sentinel_existing.flag_value = sentinel_encoded_off
                    sentinel_existing.flag_type = sentinel_spec.flag_type
                    sentinel_existing.updated_by = actor
                    sentinel_existing.note = (
                        f"sentinel_cascade from ENABLE_LLM_ASSISTANT_MODE "
                        f"by {actor}"
                    )
                self.db.add(FeatureFlagAudit(
                    flag_name=sentinel_name,
                    old_value=sentinel_old_encoded,
                    new_value=sentinel_encoded_off,
                    action="sentinel_set",
                    actor=actor,
                    note=(
                        f"R12 sentinel cascade: forced False by "
                        f"ENABLE_LLM_ASSISTANT_MODE=True"
                    ),
                    sentinel_trigger_for="ENABLE_LLM_ASSISTANT_MODE",
                ))
                _flag_override_cache[sentinel_name] = False

        # Cross-process invalidation hint (best-effort)
        self._bump_redis_async_safe()

        return FlagState(
            name=name,
            flag_type=spec.flag_type,
            lifecycle=spec.lifecycle,
            domain=spec.domain,
            description=spec.description,
            env_default=self._env_default(name),
            override_value=value,
            effective_value=value,
            source="runtime-override",
            updated_at=datetime.utcnow(),
            updated_by=actor,
            note=note,
        )

    @transactional
    async def clear_override(
        self,
        name: str,
        *,
        actor: str = "ops_console",
        note: Optional[str] = None,
    ) -> FlagState:
        """Remove the override row → next read falls back to env default."""
        spec = SUPPORTED_FLAGS.get(name)
        if spec is None:
            raise ValueError(f"flag {name!r} is not in SUPPORTED_FLAGS whitelist")

        existing = (await self.db.execute(
            select(FeatureFlagOverride).where(FeatureFlagOverride.flag_name == name)
        )).scalar_one_or_none()

        old_encoded = existing.flag_value if existing else None
        if existing is not None:
            await self.db.delete(existing)

        # Audit even on no-op clear — operator's intent is to reset
        self.db.add(FeatureFlagAudit(
            flag_name=name,
            old_value=old_encoded,
            new_value=json.dumps(None),
            action="clear",
            actor=actor,
            note=note,
        ))

        _flag_override_cache.pop(name, None)
        self._bump_redis_async_safe()

        env_default = self._env_default(name)
        return FlagState(
            name=name,
            flag_type=spec.flag_type,
            lifecycle=spec.lifecycle,
            domain=spec.domain,
            description=spec.description,
            env_default=env_default,
            override_value=None,
            effective_value=env_default,
            source="env" if env_default is not None else "default",
        )

    # ---- A1.2 R12 sentinel restore ---------------------------------------

    @transactional
    async def restore_sentinel(
        self,
        sentinel_for: str = "ENABLE_LLM_ASSISTANT_MODE",
        *,
        actor: str = "ops_console",
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Reverse the most-recent R12 sentinel cascade.

        Reads every feature_flag_audit row WHERE
        sentinel_trigger_for=:sentinel_for AND restored_at IS NULL,
        groups by flag_name, picks the latest row per flag (by created_at),
        and restores each sentinel flag to its `old_value` snapshot.

        Restoration rules per row:
          - old_value=None (encoded "null") → DELETE the override (the
            sentinel set was the first time a row existed; restore =
            remove it so reads fall through to env default)
          - old_value="false" / "true" / etc → UPSERT override back to
            that JSON-encoded value

        Idempotent: stamps `restored_at`, `restored_by` on every audit row
        in the matched batch so a second call skips them. Also writes a
        new audit row with action='sentinel_restore' for forensic clarity.

        Returns ``{"restored_flags": [list of flag names], "audit_rows": int,
        "skipped": int}``.

        Soft-fail philosophy:
          If a sentinel flag has been retired from SUPPORTED_FLAGS since
          the cascade fired, we still stamp restored_at on the audit row
          (so it doesn't loop forever) but skip the actual UPSERT.
        """
        # SELECT the still-unrestored sentinel audit rows for this trigger
        stmt = (
            select(FeatureFlagAudit)
            .where(
                FeatureFlagAudit.sentinel_trigger_for == sentinel_for,
                FeatureFlagAudit.restored_at.is_(None),
                FeatureFlagAudit.action == "sentinel_set",
            )
            .order_by(FeatureFlagAudit.flag_name, desc(FeatureFlagAudit.created_at))
        )
        rows = list((await self.db.execute(stmt)).scalars().all())
        if not rows:
            return {
                "restored_flags": [],
                "audit_rows": 0,
                "skipped": [],
                "skipped_reasons": {},
                "sentinel_for": sentinel_for,
                "drained_tasks": 0,
                "drained_keys_total": 0,
            }

        # Group by flag_name + pick the LATEST row per flag (already
        # ordered by created_at DESC within flag_name, so first wins).
        latest_per_flag: Dict[str, FeatureFlagAudit] = {}
        for row in rows:
            latest_per_flag.setdefault(row.flag_name, row)

        # F3 fix (S1-B A1.2 race): for each sentinel flag check whether
        # the operator manually set/cleared the flag AFTER the cascade
        # fired. If yes, the operator's manual action is the latest
        # authority — skip restoring this flag + record the reason on
        # the skipped list so the response surfaces the deliberate
        # operator intervention (not silently revert it).
        manual_override_after: Dict[str, "FeatureFlagAudit"] = {}
        flag_names_in_batch = list(latest_per_flag.keys())
        if flag_names_in_batch:
            manual_stmt = (
                select(FeatureFlagAudit)
                .where(
                    FeatureFlagAudit.flag_name.in_(flag_names_in_batch),
                    FeatureFlagAudit.action.in_(("set", "clear")),
                )
                .order_by(FeatureFlagAudit.flag_name, desc(FeatureFlagAudit.created_at))
            )
            for mrow in (await self.db.execute(manual_stmt)).scalars().all():
                # Keep only the LATEST manual action per flag. Use ROW ID
                # (strictly monotonic auto-increment) rather than created_at
                # — SQLite `func.now()` is second-resolution and can tie
                # cascade-time with operator-time in fast test runs.
                if mrow.flag_name not in manual_override_after:
                    sentinel_set_id = latest_per_flag[mrow.flag_name].id
                    if (
                        mrow.id is not None
                        and sentinel_set_id is not None
                        and mrow.id > sentinel_set_id
                    ):
                        manual_override_after[mrow.flag_name] = mrow

        now = datetime.utcnow()
        restored: List[str] = []
        skipped: List[str] = []
        skipped_reasons: Dict[str, str] = {}
        for flag_name, audit_row in latest_per_flag.items():
            # F3: operator manually intervened after cascade → skip revert
            if flag_name in manual_override_after:
                manual_row = manual_override_after[flag_name]
                logger.warning(
                    "[ff restore_sentinel] flag %r had operator manual %s "
                    "(actor=%s) at %s AFTER sentinel_set at %s — preserving "
                    "operator intent, skipping revert",
                    flag_name, manual_row.action, manual_row.actor,
                    manual_row.created_at, audit_row.created_at,
                )
                skipped.append(flag_name)
                skipped_reasons[flag_name] = "operator_manual_intervention"
                # Still stamp restored_at on the cascade audit row so a
                # repeat restore_sentinel call won't re-trip on it; also
                # write a forensic row marking the skip decision.
                for r in rows:
                    if r.flag_name == flag_name and r.restored_at is None:
                        r.restored_at = now
                        r.restored_by = actor
                self.db.add(FeatureFlagAudit(
                    flag_name=flag_name,
                    old_value=audit_row.new_value,  # still the sentinel-set False
                    new_value=manual_row.new_value,  # operator's value preserved
                    action="sentinel_restore",
                    actor=actor,
                    note=(
                        f"sentinel_restore SKIPPED for {flag_name}: "
                        f"operator manual {manual_row.action} by "
                        f"{manual_row.actor} at {manual_row.created_at} "
                        f"overrides earlier sentinel cascade"
                    ),
                    sentinel_trigger_for=sentinel_for,
                    restored_at=now,
                    restored_by=actor,
                ))
                continue

            spec = SUPPORTED_FLAGS.get(flag_name)
            if spec is None:
                # Sentinel flag retired since cascade; stamp + skip UPSERT
                logger.warning(
                    "[ff restore_sentinel] flag %r no longer in "
                    "SUPPORTED_FLAGS — stamping restored_at but skipping "
                    "UPSERT (already untouchable from ops UI)",
                    flag_name,
                )
                skipped.append(flag_name)
            else:
                # Restore prior state
                existing_override = (await self.db.execute(
                    select(FeatureFlagOverride).where(
                        FeatureFlagOverride.flag_name == flag_name
                    )
                )).scalar_one_or_none()
                if audit_row.old_value is None:
                    # Sentinel set created the override row; restore = delete
                    if existing_override is not None:
                        await self.db.delete(existing_override)
                    _flag_override_cache.pop(flag_name, None)
                else:
                    # Sentinel set replaced an existing override; restore = revert
                    if existing_override is None:
                        self.db.add(FeatureFlagOverride(
                            flag_name=flag_name,
                            flag_value=audit_row.old_value,
                            flag_type=spec.flag_type,
                            updated_by=actor,
                            note=(
                                f"sentinel_restore from {sentinel_for} by {actor}"
                            ),
                        ))
                    else:
                        existing_override.flag_value = audit_row.old_value
                        existing_override.flag_type = spec.flag_type
                        existing_override.updated_by = actor
                        existing_override.note = (
                            f"sentinel_restore from {sentinel_for} by {actor}"
                        )
                    # Best-effort decode for cache write-through
                    try:
                        _flag_override_cache[flag_name] = _decode_value(
                            audit_row.old_value, spec.flag_type,
                        )
                    except Exception:  # noqa: BLE001
                        _flag_override_cache.pop(flag_name, None)
                restored.append(flag_name)

            # Stamp restored_at on every row in this flag's batch (not
            # just the latest — operator should see the full cascade
            # marked as reverted, otherwise repeated restore calls would
            # tag earlier rows on each invocation).
            for r in rows:
                if r.flag_name == flag_name and r.restored_at is None:
                    r.restored_at = now
                    r.restored_by = actor

            # Forensic audit row for the restore itself
            self.db.add(FeatureFlagAudit(
                flag_name=flag_name,
                old_value=_encode_value(False, "bool"),  # sentinel state
                new_value=(audit_row.old_value or _encode_value(None, "json")),
                action="sentinel_restore",
                actor=actor,
                note=note or f"sentinel_restore from {sentinel_for}",
                sentinel_trigger_for=sentinel_for,
                restored_at=now,
                restored_by=actor,
            ))

        # F2 fix (S1-A Seam 1): drain cross-mode residue from active
        # tasks AFTER restoring flags. While the sentinel cascade had
        # the 6 flags OFF, R1b mutate / G5 crossover / etc. were
        # flag-gated off → no new residue was added. But STALE residue
        # from BEFORE the cascade (g5_pending_offspring,
        # __r1b_consumed_pending_hypothesis, etc.) is still sitting in
        # task.config; now that flags are back ON, the next round would
        # consume that stale residue and inject silent zombie payload.
        # Drain unconditionally — losing 1 round of cross-round
        # accumulation is cheaper than a zombie inject.
        drained_tasks = 0
        drained_keys_total = 0
        try:
            from backend.models import MiningTask
            from backend.services.llm_mode_service import drain_pending_residue
            tasks_to_drain = (await self.db.execute(
                select(MiningTask).where(
                    MiningTask.status.in_(("RUNNING", "PAUSED", "PENDING"))
                )
            )).scalars().all()
            for _task in tasks_to_drain:
                d = drain_pending_residue(_task)
                if isinstance(d, dict) and not d.get("_error"):
                    if d:  # non-empty drain dict (some keys popped)
                        drained_tasks += 1
                        drained_keys_total += len(d)
            if drained_tasks:
                logger.info(
                    "[ff restore_sentinel] drained %d residue keys across "
                    "%d active tasks (sentinel_for=%s)",
                    drained_keys_total, drained_tasks, sentinel_for,
                )
        except Exception as ex:  # noqa: BLE001
            # Soft-fail: drain best-effort; flag restore already done.
            logger.warning(
                "[ff restore_sentinel] residue drain failed (non-fatal, "
                "flags already restored): %s", ex,
            )

        self._bump_redis_async_safe()

        return {
            "restored_flags": sorted(restored),
            "audit_rows": len(rows),
            "skipped": sorted(skipped),
            "skipped_reasons": skipped_reasons,
            "sentinel_for": sentinel_for,
            "drained_tasks": drained_tasks,
            "drained_keys_total": drained_keys_total,
        }

    async def verify_sentinel_restore(
        self,
        sentinel_for: str = "ENABLE_LLM_ASSISTANT_MODE",
        *,
        expected_flags: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Sprint 5 PR2 (NO-GO route): confirm a sentinel restore is complete.

        After ``restore_sentinel`` runs on the NO-GO route, the operator
        wants proof every sentinel cascade row got reverted. This read-only
        verifier checks:
          1. **No dangling cascade**: zero feature_flag_audit rows with
             ``sentinel_trigger_for=:sentinel_for AND action='sentinel_set'
             AND restored_at IS NULL`` (every cascade row stamped restored).
          2. **Restore audit present**: ≥1 ``action='sentinel_restore'`` row
             exists for the trigger (the restore actually ran).
          3. **Per-flag final state**: each expected sentinel flag's current
             effective override value reported (operator eyeballs that none
             is stuck at the sentinel-forced False when it should be back ON).

        Args:
            sentinel_for: the trigger flag whose cascade we verify.
            expected_flags: sentinel flag names to report state for; defaults
                to ``settings.LLM_ASSISTANT_SENTINEL_FLAGS``.

        Returns:
            {complete: bool, dangling_cascade_rows: int, restore_rows: int,
             per_flag_state: {flag: {override_value, in_supported_flags}},
             warnings: [str]}

        Read-only — never mutates. Soft-fail on DB error → complete=False.
        """
        from backend.config import settings as _stg

        if expected_flags is None:
            expected_flags = list(
                getattr(_stg, "LLM_ASSISTANT_SENTINEL_FLAGS", []) or []
            )

        warnings: List[str] = []
        try:
            dangling = list((await self.db.execute(
                select(FeatureFlagAudit).where(
                    FeatureFlagAudit.sentinel_trigger_for == sentinel_for,
                    FeatureFlagAudit.action == "sentinel_set",
                    FeatureFlagAudit.restored_at.is_(None),
                )
            )).scalars().all())
            restore_rows = list((await self.db.execute(
                select(FeatureFlagAudit).where(
                    FeatureFlagAudit.sentinel_trigger_for == sentinel_for,
                    FeatureFlagAudit.action == "sentinel_restore",
                )
            )).scalars().all())
        except Exception as ex:  # noqa: BLE001
            logger.warning("[ff verify_sentinel_restore] query failed: %s", ex)
            return {
                "complete": False,
                "error": str(ex)[:200],
                "dangling_cascade_rows": -1,
                "restore_rows": -1,
                "per_flag_state": {},
                "warnings": ["query_failed"],
            }

        dangling_n = len(dangling)
        restore_n = len(restore_rows)

        if dangling_n > 0:
            warnings.append(
                f"{dangling_n} cascade row(s) still unrestored — run "
                f"restore_sentinel('{sentinel_for}') again"
            )
        if restore_n == 0:
            warnings.append(
                "no sentinel_restore audit row found — restore never ran"
            )

        # Per-flag current override state (read-only)
        per_flag_state: Dict[str, Any] = {}
        for flag in expected_flags:
            try:
                override = (await self.db.execute(
                    select(FeatureFlagOverride).where(
                        FeatureFlagOverride.flag_name == flag
                    )
                )).scalar_one_or_none()
                per_flag_state[flag] = {
                    "override_value": (override.flag_value if override else None),
                    "in_supported_flags": flag in SUPPORTED_FLAGS,
                }
            except Exception:  # noqa: BLE001
                per_flag_state[flag] = {"override_value": "ERROR", "in_supported_flags": False}

        complete = (dangling_n == 0 and restore_n > 0)
        return {
            "complete": complete,
            "sentinel_for": sentinel_for,
            "dangling_cascade_rows": dangling_n,
            "restore_rows": restore_n,
            "per_flag_state": per_flag_state,
            "warnings": warnings,
        }

    # ---- helpers ----------------------------------------------------------

    @staticmethod
    def _env_default(name: str) -> Any:
        """Read settings via object.__getattribute__ to bypass our own hook."""
        from backend.config import settings  # lazy
        try:
            return object.__getattribute__(settings, name)
        except AttributeError:
            return None

    @staticmethod
    def _bump_redis_async_safe() -> None:
        """Best-effort write of a bump key + delete of the hash key.

        Other processes' refreshers see the bumped value and re-pull from
        DB on their next tick. We never raise — Redis is a hint layer
        only; DB has already been written.
        """
        try:
            from backend.tasks.redis_pool import get_redis_client  # lazy
            cli = get_redis_client()
            cli.delete(REDIS_FLAGS_KEY)
            cli.set(REDIS_FLAGS_BUMP_KEY, str(datetime.utcnow().timestamp()), ex=REDIS_FLAGS_TTL)
        except Exception as ex:
            logger.debug("[feature_flag] redis bump failed (ignored): %s", ex)
