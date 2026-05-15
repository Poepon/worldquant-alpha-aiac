"""
AIAC 2.0 Configuration
Centralized settings management using Pydantic
"""

import os
from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    # Project Info
    PROJECT_NAME: str = "AIAC 2.0 - Alpha-GPT Mining System"
    VERSION: str = "2.0.0"
    API_V1_STR: str = "/api/v1"
    
    # Database
    POSTGRES_USER: str = os.getenv("POSTGRES_USER", "postgres")
    POSTGRES_PASSWORD: str = os.getenv("POSTGRES_PASSWORD", "postgres")
    POSTGRES_SERVER: str = os.getenv("POSTGRES_SERVER", "localhost")
    POSTGRES_PORT: str = os.getenv("POSTGRES_PORT", "5432")
    POSTGRES_DB: str = os.getenv("POSTGRES_DB", "alpha_gpt")
    
    @property
    def SQLALCHEMY_DATABASE_URI(self) -> str:
        return f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.POSTGRES_SERVER}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
    
    # Redis (for Celery and SSE)
    REDIS_HOST: str = os.getenv("REDIS_HOST", "localhost")
    REDIS_PORT: str = os.getenv("REDIS_PORT", "6379")
    REDIS_PASSWORD: str = os.getenv("REDIS_PASSWORD", "")
    
    @property
    def REDIS_URL(self) -> str:
        if self.REDIS_PASSWORD:
            return f"redis://:{self.REDIS_PASSWORD}@{self.REDIS_HOST}:{self.REDIS_PORT}/0"
        return f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/0"
    
    # Brain Platform Credentials
    BRAIN_EMAIL: str = os.getenv("BRAIN_EMAIL", "")
    BRAIN_PASSWORD: str = os.getenv("BRAIN_PASSWORD", "")
    
    # LLM Configuration (OpenAI Compatible — fallback / legacy)
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    OPENAI_BASE_URL: str = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4")

    # W5: LLM Provider switch — "openai" (Qwen/DeepSeek/etc.) or "anthropic"
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "openai")
    # Anthropic Claude (for prompt-cache-friendly inference)
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    # Default model for code_gen (cheaper); override per-call via call(model=...)
    ANTHROPIC_MODEL: str = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5")
    
    # Mining Configuration
    DEFAULT_REGION: str = "USA"
    DEFAULT_UNIVERSE: str = "TOP3000"
    DEFAULT_DAILY_GOAL: int = 4
    
    # Quality Thresholds (Traditional — kept as fallback / pre-tier baseline)
    SHARPE_MIN: float = 1.5
    TURNOVER_MAX: float = 0.7
    FITNESS_MIN: float = 1.0
    MAX_CORRELATION: float = 0.7

    # ----- Tier-specific PASS thresholds (T1/T2/T3 factor library) -----
    # T1: 裸 ts_op 信号；2026-05-07 P0 收紧到 BRAIN 提交 gate
    # 旧值 0.8/0.5 是 探索 bar — batch 276-283 产生 8 条 PASS 全部 can_submit=False
    # (BRAIN bar: sharpe ≥ 1.25, fitness ≥ ~1.0). T1 PASS 现在 = BRAIN-submittable
    # 候选, 探索路径走 PROVISIONAL → optimization queue (Fix C 2026-05-07).
    TIER1_SHARPE_MIN: float = 1.25
    TIER1_FITNESS_MIN: float = 0.95
    TIER1_TURNOVER_MIN: float = 0.01
    TIER1_TURNOVER_MAX: float = 0.70
    TIER1_SUBUNIV_MIN: float = 0.1
    # T2: 包装后成型 — group/pure-xs/smoothing wrapper 套 T1 信号
    TIER2_SHARPE_MIN: float = 1.0
    TIER2_FITNESS_MIN: float = 0.8
    TIER2_TURNOVER_MIN: float = 0.01
    TIER2_TURNOVER_MAX: float = 0.55  # 与 T3 trade_when 协同：T3 entry-filter 把 T2 0.55 降到 0.20-0.30
    TIER2_SUBUNIV_MIN: float = 0.2
    # T3: 接近可提交 — trade_when 择时；保 BRAIN /check buffer
    TIER3_SHARPE_MIN: float = 1.5
    TIER3_FITNESS_MIN: float = 1.0
    TIER3_TURNOVER_MIN: float = 0.01
    TIER3_TURNOVER_MAX: float = 0.70
    TIER3_SELF_CORR_MAX: float = 0.7  # T3 严格判 self_corr

    # V-22.5 (2026-05-11): T2 self_corr PASS gate. Default ON with same 0.7
    # threshold as T3. Before V-22.5, T2 skipped self_corr (rationale "within-
    # batch wrapper variants correlate, would FAIL whole batch") — but
    # BRAIN /correlations/SELF is vs already-submitted OS cache not within
    # batch, so this rationale was wrong. IQC audit showed 13/13 net-positive
    # Δscore T2 alphas all had self_corr 0.85-0.99 vs portfolio — all BRAIN
    # submit-rejected. Mining-time gate puts these PROV instead of PASS so
    # KB / submission queue stay clean. Disable by setting threshold to 1.0
    # or ENABLE_T2_SELF_CORR_CHECK=False to revert.
    TIER2_SELF_CORR_MAX: float = 0.7
    ENABLE_T2_SELF_CORR_CHECK: bool = True

    # PASS_PROVISIONAL 阈值（同梯度，各项放宽 30-40%）
    # 2026-05-07 P0 同步上调 — 与 T1 PASS 1.25/0.95 配合作 探索 bar
    # 旧 0.5/0.3 在新 PASS gate 下意义不再（远低于 PASS）
    TIER1_PROVISIONAL_SHARPE_MIN: float = 0.8
    TIER1_PROVISIONAL_FITNESS_MIN: float = 0.6
    TIER1_PROVISIONAL_TURNOVER_MAX: float = 0.85
    TIER1_PROVISIONAL_SUBUNIV_MIN: float = 0.0  # 仍要为正
    TIER2_PROVISIONAL_SHARPE_MIN: float = 0.8
    TIER2_PROVISIONAL_FITNESS_MIN: float = 0.6
    TIER2_PROVISIONAL_TURNOVER_MAX: float = 0.65
    TIER2_PROVISIONAL_SUBUNIV_MIN: float = 0.1
    TIER3_PROVISIONAL_SHARPE_MIN: float = 1.3
    TIER3_PROVISIONAL_FITNESS_MIN: float = 0.8
    TIER3_PROVISIONAL_TURNOVER_MAX: float = 0.70
    # T3 sub-universe min 用 BRAIN 动态 limit；PROVISIONAL 用 limit×0.7

    # Tier system feature flags & 启动门槛
    ENABLE_FACTOR_TIERING: bool = True  # 总开关；False 时 router 拒收 AUTONOMOUS_TIER* mode
    T1_USE_LLM_GUIDED_STRATEGY: bool = True  # False 时 T1 task 回退 W0 ALPHA_GENERATION_SYSTEM
    MIN_TIER_SEED_COUNT: int = 5  # T2/T3 task 启动门槛 + node_tier_seed_load 早停门槛共用

    # V-19 Persistent Mining Service mode (2026-05-10) — cascade settings.
    # Round-driven phase switching (per IX-2 decision): each phase runs a fixed
    # round budget then transitions, regardless of PASS count. Phase skip when
    # local + global seed pool both < MIN_TIER_SEED_COUNT (IX-1 fallback).
    CASCADE_T1_ROUNDS: int = 10            # T1 phase 跑这么多 round 切 T2
    CASCADE_T2_ROUNDS: int = 10
    CASCADE_T3_ROUNDS: int = 5
    CASCADE_ENABLE_T3: bool = False        # IX-4: T3 PASS rate=0% (V-16 拦) → 默认禁
    CASCADE_PAUSE_POLL_SEC: float = 1.0    # 主循环每 round 末检查 PAUSED 间隔

    # V-19.7 watchdog + BRAIN quota guard.
    # V-27.1: DEAD_MIN 15→25 — a live worker can legitimately go 15+ min
    # without a heartbeat (long BRAIN multi-sim, slow LLM, all-dedup round).
    # The lock-takeover fix roots out the race; the wider window is a cheap
    # probability cushion that just reduces how often a falsely-presumed-dead
    # worker overlaps the replacement by one round.
    CASCADE_WATCHDOG_DEAD_MIN: int = 25      # last_alpha_persisted_at < NOW()-N → dead
    CASCADE_WATCHDOG_GRACE_MIN: int = 15     # task.created_at > NOW()-N → skip (start-up grace)
    BRAIN_DAILY_SIMULATE_LIMIT: int = 1000   # consultant 估算 — 实际由 BRAIN 决定
    BRAIN_QUOTA_PAUSE_PCT: float = 0.9       # 达 90% 自动 pause CONTINUOUS_CASCADE

    # V-27.1 cascade-lock takeover. The watchdog atomically takes over the
    # lock (new token) instead of force_clear + re-acquire; the cascade main
    # loop self-checks lock ownership at every round boundary and exits
    # gracefully if taken over. Flag off → revert to force_clear + no
    # ownership self-check (kill-switch for the new self-exit path).
    CASCADE_LOCK_TAKEOVER_ENABLED: bool = True
    CASCADE_LOCK_TTL_SEC: int = 10800        # cascade lock TTL — shared by acquire + watchdog takeover

    # V-20 (2026-05-10) round-pipeline. While round N's SIMULATE blocks on
    # BRAIN (~5 min, IO-bound), round N+1's LLM/CODE_GEN/VALIDATE runs in an
    # isolated DB session as a background asyncio task. BRAIN slot semaphore
    # (redis) blocks round N+1's SIMULATE until N releases — natural overlap.
    # Set False to revert to fully-serial cascade phases.
    CASCADE_PIPELINE_ENABLED: bool = True

    # P1 (2026-05-07): auto ts_decay_linear(., 4) wrapper for T1 candidates.
    # 实测验证 (docs/decay_verify_pk6606.json): decay=4 把 sh=1.58 fit=0.85
    # to=0.81 (HIGH_TURNOVER+LOW_FITNESS) 转成 sh=1.45 fit=1.47 to=0.51
    # (BRAIN can_submit=true,首次 PASS).启用此 flag 让 expand_t1_strategy
    # 在每个 T1 候选旁边加一个 decay=4 包装的 T2 twin,加倍候选数但显著
    # 提升每轮出 PASS 概率。decay=2/8/16 sweep 显示 4 是 sweet spot。
    T1_AUTO_DECAY_WRAPPER: bool = True
    T1_AUTO_DECAY_VALUE: int = 4  # the d in ts_decay_linear(expr, d)

    # V-22.6 (2026-05-12) — Composite-field T1 enumeration. BRAIN ts_op accepts
    # only one primary input series, so multi-field signals (PE = close/eps,
    # accrual = cfo/ni, intraday range = (high-low)/close ...) must be
    # synthesized arithmetically BEFORE ts_op. Source: spike showed every
    # PROV/PASS alpha in rounds 16-20 was single-field; 38 V-22.5-safe
    # candidates had Δscore<0 because Δscore>0 ones got self-corr blocked
    # against the OS portfolio (also single-field returns variants).
    #   - COMPOSITE_T1_ENABLED: master switch (False reverts to single-field).
    #   - COMPOSITE_T1_MAX_PER_COMPOSITE: cap ts_op×window combos per composite
    #     so 1 composite doesn't crowd stratified-sample.
    #   - COMPOSITE_T1_BACKFILL_WINDOW: ts_backfill window for sparse
    #     fundamentals (quarterly EPS / EBIT carry NaN between reports).
    #   - COMPOSITE_T1_WINSORIZE_STD: winsorize σ bound to trim ratios with
    #     near-zero denominators.
    COMPOSITE_T1_ENABLED: bool = True
    COMPOSITE_T1_MAX_PER_COMPOSITE: int = 2
    # V-22.6.1 (2026-05-12): default OFF after spike showed BRAIN's 8-operator
    # complexity limit rejects the full preprocess wrap on multi-leg composites
    # (e.g. overnight_gap counts as 13 ops). The bare form `ts_op(<composite>, w)`
    # still classifies as T2 via _peel_composite_preprocess in the classifier,
    # so wrap-less composites still flow through the T1 pipeline correctly.
    # Re-enable per-family or globally once BRAIN raises the limit OR for
    # sparse-fundamental composites where backfill is strictly needed.
    COMPOSITE_T1_APPLY_PREPROCESS: bool = False
    COMPOSITE_T1_BACKFILL_WINDOW: int = 120
    COMPOSITE_T1_WINSORIZE_STD: int = 4
    # V-22.6.3 (2026-05-12): auto-emit ts_decay_linear(<composite>, 4) variant
    # per composite. Spike showed 4 PROV composite alphas (sharpe 1.8-2.35) all
    # blocked by turnover 0.81-0.82 > BRAIN's 0.7 can_submit gate. ts_decay_linear
    # 4-day smoothing typically halves turnover with minimal sharpe loss (per
    # T1_AUTO_DECAY_WRAPPER verification 2026-05-07). The decay variant is an
    # additional candidate alongside the LLM-driven ts_op × window combos,
    # NOT a replacement — both shapes flow to BRAIN simulate.
    COMPOSITE_T1_AUTO_DECAY_WRAPPER: bool = True
    COMPOSITE_T1_AUTO_DECAY_VALUE: int = 4
    # V-22.6.5 (2026-05-12) — reserve a fraction of the final candidate pool
    # for composite alphas after stratified_sample. Spike on V-22.6.4
    # verification rounds saw fund composites averaged out by the 42-bucket
    # stratification (composite ratio ~21% of final pool, fund composite ~6%).
    # Reserving 33% for composites lifts fund-composite expected count from
    # ~0.6/round to ~2/round, giving reliable yield of fund composite saves.
    # 0.0 = disabled (legacy single-stratified-sample behavior).
    COMPOSITE_T1_FINAL_POOL_QUOTA_PCT: float = 0.33

    # Plan v5+ §Phase 1 — Hypothesis-Guided Exploration (HGE) staging flag.
    # 0 = current dataset-centric (pre-Phase 1, default until Phase 1 verified)
    # 1 = cross-dataset hypothesis: LLM picks 1-3 datasets per hypothesis from
    #     available_dataset_pool; code_gen uses union of selected_datasets'
    #     fields. Enables the per-task config["hypothesis_centric_variant"]
    #     A/B path via task_service.assign_variant.
    # 2 = typed Hypothesis + lifecycle (Phase 2, 9-12 day)
    # 3 = main-loop invert (Phase 3, deferred to Q3 re-evaluation)
    # V-22.12 (2026-05-13): when a can_submit refresh flips True, automatically
    # call BRAIN /competitions/{comp}/before-and-after-performance and stash
    # the deltas in alpha.metrics._iqc_marginal. Empty string disables the
    # auto-audit. Frontend filters by metrics._iqc_marginal.delta_score>0 to
    # surface "actually adds value to team portfolio" candidates.
    IQC_AUTO_AUDIT_COMPETITION: str = "IQC2026S1"

    HYPOTHESIS_CENTRIC_LEVEL: int = 0
    # V-22.11 (2026-05-13): Phase 2 A/B activation. CANDIDATE=2 enables 50/50
    # split — new tasks get either legacy (LEVEL=0) or Phase 2 typed
    # Hypothesis path (LEVEL=2) per task_service.assign_variant. Phase 2 code
    # (B1-B10) is fully shipped + unit-tested; this flag flips it into the
    # live mining loop for empirical validation. Per plan v5+ F-5 unidirectional
    # migration: once stable, bump LEVEL=2 to make Phase 2 default.
    HYPOTHESIS_CENTRIC_CANDIDATE: int = 2   # 50/50 A/B candidate level (>= LEVEL)
    # Phase 1 cross-dataset hypothesis: how many complementary datasets the
    # LLM is offered alongside the anchor dataset. 0 = single anchor only
    # (legacy behavior). 3 = anchor + 3 others (plan default).
    PHASE1_COMPLEMENTARY_DATASET_K: int = 3

    # PR5 — T1 sign-flip retry. When a T1 candidate FAILs but |sharpe| ≥
    # T1_FLIP_RETRY_SHARPE (default 0.5), evaluation re-simulates the negated
    # expression (`multiply(-1, expr)`) and re-evaluates against the same
    # gate. This catches alphas where the LLM picked the right field/op
    # but the wrong sign convention (e.g. quality factors that should be
    # ranked descending). Bounded by T1_FLIP_RETRY_CAP per round to keep
    # BRAIN budget under control. Set ENABLE_T1_SIGN_FLIP_RETRY=False to
    # disable globally.
    ENABLE_T1_SIGN_FLIP_RETRY: bool = True
    T1_FLIP_RETRY_SHARPE: float = 0.5  # min |sharpe| to trigger flip; below = noise
    T1_FLIP_RETRY_CAP: int = 5  # max flips per round

    # Signal-vs-control 双跑归因 — 一个 PASS alpha 被识别后,模拟一个"对照"表达式
    # (T1 信号核剥成裸字段、保留 T2/T3 结构包装);Δ(sharpe_signal − sharpe_control)
    # 归因业绩来源。Δ 小 = PASS 是结构产物(包装在做功,非假设信号)→ PASS 降级为
    # PASS_PROVISIONAL,拦在直接提交池外。每轮上限 SIGNAL_CONTROL_CAP。
    # 设 ENABLE_SIGNAL_CONTROL_DUAL_RUN=False 全局关闭(默认 opt-in,避免意外增加
    # BRAIN 模拟配额消耗)。来源:docs/alphagbm_skills_research_2026-05-15.md P0。
    ENABLE_SIGNAL_CONTROL_DUAL_RUN: bool = False  # opt-in(额外 BRAIN 模拟)
    SIGNAL_CONTROL_DELTA_SHARPE_MIN: float = 0.3  # 信号被信任所需的最小 Δsharpe
    SIGNAL_CONTROL_CAP: int = 5  # 每轮对照模拟次数上限

    # P1-A: Graded scoring — 百分位归一化 + 非均匀权重 + confidence 维度
    # (docs/alphagbm_skills_research_2026-05-15.md 原则①). Advisory layer;
    # 老 calculate_alpha_score / routing 阈值不变。opt-in。
    # 开启后:每个 PASS alpha 用 sharpe 相对 cell 历史基线算出百分位,切 5 档(A-E),
    # 附带 confidence(真实输入占比);低 confidence 的 PASS → PASS_PROVISIONAL。
    ENABLE_GRADED_SCORE: bool = False
    SCORE_WEIGHT_TEST_SHARPE: float = 0.55        # 与 alpha_scoring.default_weights 逐位一致
    SCORE_WEIGHT_TRAIN_SHARPE: float = 0.25
    SCORE_WEIGHT_FITNESS: float = 0.20
    SCORE_WEIGHT_PROD_CORR_PENALTY: float = 0.30
    SCORE_WEIGHT_TURNOVER_PENALTY: float = 0.15
    SCORE_WEIGHT_INVESTABILITY_PENALTY: float = 0.20
    SCORE_CONFIDENCE_MIN: float = 0.5             # PASS confidence 低于此 → 降级 PROVISIONAL
    SCORE_GRADED_CAP: int = 0                     # 0 = 全部 PASS;>0 仅对 sharpe top-N 跑

    # P1-A: diversity_tracker 权重(替换 evaluate_diversity 内硬编码 0.30/0.30/0.25/0.15)
    DIVERSITY_DATASET_WEIGHT: float = 0.30
    DIVERSITY_FIELD_WEIGHT: float = 0.30
    DIVERSITY_OPERATOR_WEIGHT: float = 0.25
    DIVERSITY_SETTINGS_WEIGHT: float = 0.15

    # P2-B (2026-05-15): Five Pillars factor classification.
    # 来源: docs/alphagbm_skills_research_2026-05-15.md skill `compare`.
    # diversity_tracker 5 维启用走重归一化路径 — pillar 是新增第 5 维,默认 0.20。
    # 老 4 维 weight (0.30/0.30/0.25/0.15) **不动** — ENABLE_PILLAR_AWARE_SELECTION
    # OFF / pillar=None 时 evaluate_diversity 行为 byte-for-byte 等同 P1-A。
    DIVERSITY_PILLAR_WEIGHT: float = 0.20
    # Pillar-aware selection 开关 — 默认 OFF。开启时 node_hypothesis 跑 deficit
    # 检查 + prompt nudge,diversity_tracker 走 5 维重归一化加权。
    # 1-2 周观察 docs/pillar_balance/<sh-date>.json 后再切换。
    ENABLE_PILLAR_AWARE_SELECTION: bool = False
    # 6 桶 + other 的目标分布 — pillar_balance_check 报告内 deficit 计算基准。
    PILLAR_TARGET_DISTRIBUTION: dict = {
        "momentum":   0.25,
        "value":      0.20,
        "quality":    0.20,
        "volatility": 0.15,
        "sentiment":  0.10,
        "other":      0.10,
    }
    # nudge 触发阈值:max(deficit) > threshold * target.share。
    # 例 target=0.20 + threshold=0.4 → 0.20−share ≥ 0.08 时 nudge 该 pillar。
    PILLAR_BALANCE_SKEW_THRESHOLD: float = 0.4

    # P1-C (2026-05-15): alpha library health check thresholds + weights.
    # 来源: docs/alphagbm_skills_research_2026-05-15.md skill `health-check`.
    # Consumed by backend/services/alpha_health_service.py via
    # ``from backend.config import settings`` (top-level import — never
    # inside a function body, so monkeypatching for tests works).
    STALE_YELLOW_DAYS: int = 7
    STALE_ORANGE_DAYS: int = 14
    STALE_RED_DAYS: int = 30
    DRIFT_YELLOW_PCT: float = -10.0
    DRIFT_ORANGE_PCT: float = -30.0
    DRIFT_RED_PCT: float = -50.0
    HEALTH_WEIGHT_STALE: float = 0.35
    HEALTH_WEIGHT_DRIFT: float = 0.50
    HEALTH_WEIGHT_ORPHAN: float = 0.15
    HEALTH_SCORE_TRUNCATE_THRESHOLD: int = 70

    # P1-C part 2 (2026-05-15): hypothesis structured triggers + LLM thesis
    # scoring. 来源: docs/alphagbm_skills_research_2026-05-15.md skill
    # `investment-thesis`. Consumed by
    # backend/services/hypothesis_health_service.py via
    # ``from backend.config import settings`` (top-level — never inside a
    # function so monkeypatching works).
    # Trigger thresholds — note pct values are NEGATIVE (drops):
    TRIGGER_DROPPED_SHARPE_PCT: float = -30.0       # T1 orange threshold
    TRIGGER_DROPPED_SHARPE_RED_PCT: float = -50.0   # T1 red threshold
    TRIGGER_NOPASS_N_ROUNDS: int = 5                # T2 window (rounds)
    TRIGGER_PASS_RATE_DROP_PCT: float = -50.0       # T3 threshold
    TRIGGER_PASS_RATE_WINDOW: int = 5               # T3 window (each side)
    TRIGGER_ATTR_HYPOTHESIS_WINDOW: int = 5         # T4 window (rounds)
    TRIGGER_ATTR_HYPOTHESIS_SHARE: float = 0.6      # T4 dominance share
    TRIGGER_STALE_SHARE: float = 0.5                # T5 stale-share threshold
    TRIGGER_DETAIL_MAX_ENTRIES: int = 50            # trigger_detail FIFO cap

    # LLM scoring controls
    ENABLE_LLM_THESIS_SCORE_ON_PROMOTED: bool = True
    ENABLE_LLM_THESIS_SCORE_ON_TRIGGER: bool = True
    # Per-RUN(not per-day)token budget — counter resets every Celery beat
    # invocation. Renamed in P2 review fix; the old "DAILY" name was misleading
    # because nothing tracked spend across runs.
    THESIS_SCORE_PER_RUN_TOKEN_BUDGET: int = 200_000
    LLM_SCORE_RETRY_BACKOFF_HOURS: int = 4          # fallback failure retry
    THESIS_SCORING_MAX_ROUNDS: int = 10             # prompt length cap
    THESIS_SCORING_MAX_TRIGGER_HITS: int = 3
    THESIS_SCORE_DUMP_THRESHOLD: int = 70           # JSON output truncate

    # PR7 — incremental persistence for T2/T3 tasks. By default, T2/T3 work-
    # flow batches all 8+ seeds before run_with_persistence writes Alpha rows
    # to DB (workflow.run() only returns at END). This means a 1-hour task
    # has 1-hour write-amplification: if worker dies, all PASS alphas in
    # state.generated_alphas are lost. With this flag True, node_save_results
    # writes Alpha rows + alpha_status_transitions immediately for each seed
    # batch, so frontend / downstream can see PASSes in near-real-time and
    # crashes don't lose accumulated work. Only affects factor_tier ∈ (2,3);
    # T1 tasks already persist per round.
    T2_INCREMENTAL_PERSISTENCE: bool = True

    # PR7 — wrapper-aware simulation settings. When True, node_simulate buckets
    # expressions by per-alpha settings (chosen via smart_simulation_settings
    # based on expression form + field category) and calls simulate_batch per
    # bucket. Defaults to True after backfill found 0% of mining-produced
    # alphas can submit — root cause is double-neutralization (group_*
    # wrapper + BRAIN neut=SUBINDUSTRY) and decay-vs-trade_when conflicts.
    # Toggle off if a future task shows PASS-rate regression.
    ENABLE_SMART_SIM_SETTINGS: bool = True

    # Plan v5+ #3 (2026-05-07): pre-simulate skeleton classifier toggle.
    # When True, node_simulate runs each candidate through the trained
    # sklearn LogisticRegression and skips alphas with P(PASS) < threshold
    # BEFORE BRAIN simulate. Saves BRAIN concurrent-slot time on
    # likely-fails. Model: AUC=0.813 on 2737 historical alphas (451 PASS).
    #
    # V-24.B (2026-05-13): default flipped ON with conservative threshold
    # 0.10. Threshold table from training run:
    #   0.05  → 98.9% PASS recall, skips  2.2% FAIL (negligible savings)
    #   0.10  → 98.0% PASS recall, skips  7.1% FAIL  ← current default
    #   0.15  → 96.5% PASS recall, skips 17.0% FAIL  ← recommended
    # Picking 0.10 trades 2pp PASS recall for 7% BRAIN simulate savings
    # — a safer first rollout than 0.15. Run a week of metrics
    # (scripts/pre_simulate_filter_audit.py) before bumping to 0.15.
    ENABLE_PRE_SIMULATE_FILTER: bool = True
    PRE_SIMULATE_FILTER_THRESHOLD: float = 0.10

    # V-24.E (2026-05-13): FIELD_INSIGHT / HYPOTHESIS_INSIGHT writes gated.
    # kb_hit_audit found 4170 historical rows of these types had 0% retrieve
    # rate — feedback_agent persists them but rag_service has no _get_*_insights
    # path. Disable by default; can be re-enabled if retrieve paths are added
    # later. See feedback_agent.py:1039+ for the write site.
    #
    # V-26.38/39 (2026-05-13): formal deprecation track. Decision deadline
    # 2026-Q3 — retire vs build retrieve vs defer. See
    # `docs/v26_38_39_field_insight_deprecation.md` for the three options.
    WRITE_FIELD_HYPOTHESIS_INSIGHTS: bool = False
    # PR4 — P0 实验结论：BRAIN GET /alphas/{id} 返回冻结的 sim 时 snapshot，不是
    # rolling 重算。所以 node_tier_seed_load 调 BRAIN refresh metrics 是 no-op，
    # 浪费配额。默认关闭；只有当 BRAIN 行为改变（比如未来开放 rolling endpoint）
    # 或想 detect "alpha 被删除"等副作用时才开启。OS-active alpha 的 metrics
    # 累加是另一回事，不在这个 flag 控制范围。
    TIER_SEED_LOAD_REFRESH_VIA_BRAIN: bool = False
    # 同理 — 历史 KB-referenced alpha 的 metrics 不会随时间变化（IS 冻结），
    # daily refresh 仅在你想监测 alpha-deletion 等边界行为时才开。
    REFRESH_KB_VIA_BRAIN: bool = False

    # 各 region 可用 group 列表（T2 wrapping 时用，过滤 group_neutralize/group_rank 等的 group 取值）
    # 默认值；可在 .env 用 JSON 字符串覆盖（例：REGION_GROUPS='{"USA":["industry","subindustry","sector","market"]}'）
    REGION_GROUPS: dict = {
        "USA": ["industry", "subindustry", "sector", "market"],
        "CHN": ["industry", "subindustry", "market"],  # CHN 无 sector
        "EUR": ["industry", "subindustry", "sector", "market", "country"],
        "ASI": ["industry", "subindustry", "market", "country"],
        "GLB": ["industry", "subindustry", "sector", "market", "country"],
    }
    
    # Multi-Objective Scoring Thresholds
    SCORE_PASS_THRESHOLD: float = 0.8      # Composite score to pass (legacy fallback)
    SCORE_OPTIMIZE_THRESHOLD: float = 0.3  # Score threshold for optimization queue (legacy fallback)

    # Tier-aware score thresholds (P0 #3). Defaults = global values above → zero behaviour change.
    # Per-tier tuning is a follow-up; setting TIER{N}_SCORE_PASS in .env activates it.
    TIER1_SCORE_PASS: float = 0.8
    TIER1_SCORE_OPTIMIZE: float = 0.3
    TIER2_SCORE_PASS: float = 0.8
    TIER2_SCORE_OPTIMIZE: float = 0.3
    TIER3_SCORE_PASS: float = 0.8
    TIER3_SCORE_OPTIMIZE: float = 0.3
    
    # P0-3: Two-Stage Correlation Check
    CORR_CHECK_THRESHOLD: float = 0.5      # Preliminary score threshold to trigger correlation check
    
    # P1-1: Dataset Bandit Selection
    BANDIT_SELECTION_ENABLED: bool = True  # P1-fix-1: Enable adaptive dataset selection
    BANDIT_EXPLORATION_WEIGHT: float = 2.0
    BANDIT_PYRAMID_BONUS_WEIGHT: float = 0.3
    BANDIT_SATURATION_PENALTY_WEIGHT: float = 0.2
    BANDIT_TIME_DECAY_DAYS: int = 7
    
    # P1-2: Field Selection
    FIELD_COVERAGE_WEIGHT: float = 0.3
    FIELD_NOVELTY_WEIGHT: float = 0.4
    FIELD_PYRAMID_WEIGHT: float = 0.3
    FIELD_MIN_COVERAGE: float = 0.3
    
    # P1-4: Diversity Constraints
    DIVERSITY_SIMILARITY_THRESHOLD: float = 0.7
    BATCH_DEDUP_THRESHOLD: float = 0.9
    
    # P2-2: Multi-Fidelity Evaluation
    MULTI_FIDELITY_ENABLED: bool = False   # Opt-in feature
    QUICK_TEST_PERIOD: str = "P0Y3M"
    MEDIUM_TEST_PERIOD: str = "P1Y0M"
    FULL_TEST_PERIOD: str = "P2Y0M"
    MAX_FULL_EVALS_PER_BATCH: int = 10

    # P0: Baseline + Nσ-residual screening (docs/alphagbm_skills_research_2026-05-15.md).
    # Fits a (hypothesis-family × dataset × region) performance baseline and
    # annotates each alpha with its residual sigma. Soft signal only — affects
    # optimization priority, never the PASS/FAIL hard gates. Opt-in.
    BASELINE_SCREEN_ENABLED: bool = False  # Opt-in feature
    BASELINE_METRIC: str = "sharpe"        # in-memory metrics dict key to score
    BASELINE_MIN_SAMPLES: int = 30         # min cell samples to trust a baseline
    BASELINE_DISCOVERY_SIGMA: float = 2.0  # residual >= this -> DISCOVERY
    BASELINE_BELOW_SIGMA: float = -1.0     # residual <= this -> BELOW
    BASELINE_LOOKBACK_DAYS: int = 120      # history window for cell samples
    BASELINE_SAMPLE_LIMIT: int = 2000      # cap on samples fetched per cell
    
    # Evolution Strategy Defaults
    DEFAULT_TEMPERATURE: float = 0.7
    DEFAULT_EXPLORATION_WEIGHT: float = 0.5
    MAX_EVOLUTION_ITERATIONS: int = 10
    ALPHAS_PER_ROUND: int = 4
    
    # Optimization Chain Settings
    MAX_OPTIMIZATION_VARIANTS: int = 10
    MAX_SETTINGS_VARIANTS: int = 5
    OPTIMIZATION_BUDGET_PER_ALPHA: int = 20  # Max simulations per optimization target
    
    # Field Screening Settings
    FIELD_SCREENING_ENABLED: bool = True
    FIELD_SCREENING_TOP_K: int = 20
    FIELD_SCREENING_TEMPLATES: int = 4  # Number of templates to test per field
    
    # Rate Limiting
    MAX_SIMULATIONS_PER_DAY: int = 100
    MAX_TOKENS_PER_DAY: int = 500000

    # Layer 1 Anti-collapse (2026-05-11) — ε-greedy explore budget.
    # Probability that a strategy_select round runs in EXPLORE mode: RAG
    # success patterns hidden from the LLM, prompt directs structural
    # novelty. Without this, the LLM keeps sampling the historical PASS
    # neighborhood and cascade collapses to db_duplicate ≥ 90%. Default
    # 0.3 = 1 in ~3 rounds explores; tune up if collapse persists.
    EXPLORE_BUDGET_PCT: float = 0.3

    # Layer 1 dedup blacklist tunables (2026-05-11 V-22.4):
    # state.recent_dedup_skeletons FIFO cap. evaluation.py appends rejected
    # skeletons; strategy_prompts renders last N to LLM. Higher cap = longer
    # memory, more pressure to diversify, but also pressure to wander into
    # low-sharpe field combos. Empirically 50 captures ~5-8 round history.
    DEDUP_BLACKLIST_CAP: int = 50
    # How many recent skeletons appear in T1/T2/T3 prompt blocks. T1 has
    # widest search space so more context helps; T3 has narrowest (template
    # picks) so fewer suffices. Setting to 0 disables the dedup block for
    # that tier (LLM won't see the blacklist).
    DEDUP_PROMPT_T1_LIMIT: int = 30
    DEDUP_PROMPT_T2_LIMIT: int = 20
    DEDUP_PROMPT_T3_LIMIT: int = 15

    # ------------------------------------------------------------------
    # V-26 Batch 4 (2026-05-13) — magic-number consolidation
    # ------------------------------------------------------------------
    # Values lifted from various modules where they were hardcoded. Defaults
    # match the pre-fix behavior exactly; centralising them lets ops tune
    # without code edits and lets future audits diff intent vs. live values.

    # V-26.36 — RAG retrieval scoring weights
    # rag_service._get_success_patterns_enhanced / _get_failure_pitfalls_enhanced
    # used 100/50/30/20/10/5/40/25 directly in the inner scoring loop. The
    # ratios are tuned empirically; exposing them lets a future A/B
    # (e.g. boost dataset match vs hypothesis-family preference) be a config
    # diff rather than a code commit.
    RAG_SCORE_DATASET_MATCH: float = 100.0
    RAG_SCORE_CATEGORY_EXACT: float = 50.0
    RAG_SCORE_CATEGORY_PARTIAL: float = 30.0
    RAG_SCORE_REGION_MATCH: float = 20.0
    RAG_SCORE_REGION_GENERIC: float = 5.0
    RAG_SCORE_BASE_MULTIPLIER: float = 10.0
    RAG_SCORE_SHARPE_MULTIPLIER: float = 5.0
    RAG_SCORE_USAGE_BONUS_CAP: int = 10
    RAG_SCORE_USAGE_BONUS_PER: float = 0.5
    RAG_SCORE_HYPOTHESIS_FAMILY_PATTERN: float = 40.0   # V-26.12
    RAG_SCORE_HYPOTHESIS_FAMILY_PITFALL: float = 25.0

    # V-26.59 — SELF_CORRECT / validate tunables
    SELF_CORRECT_TEMPERATURE: float = 0.3
    VALIDATE_DEDUP_SIMILARITY_THRESHOLD: float = 0.90

    # V-26.65 — node_simulate default sim settings (used when smart-settings
    # bucketing is OFF or for legacy fallback). Keeps the same defaults that
    # have been in production since the BRAIN integration.
    SIM_DEFAULT_DELAY: int = 1
    SIM_DEFAULT_DECAY: int = 4
    SIM_DEFAULT_NEUTRALIZATION: str = "SUBINDUSTRY"

    # V-26.68 — V-16 suspicion-mode thresholds. The trigger threshold
    # (was a module-level constant) plus the two heuristics for the
    # cost-vacuum check.
    V16_SUSPICION_THRESHOLD: float = 3.0
    V16_COST_VACUUM_TURNOVER: float = 0.50
    V16_COST_VACUUM_SHARPE: float = 5.0

    # V-26.83 — IQC audit backfill sweep tunables
    IQC_AUDIT_BACKFILL_LIMIT: int = 50
    IQC_AUDIT_BACKFILL_COUNTDOWN_SEC: int = 2

    # V-26.94 — round-level attribution dominance thresholds
    # (classify_attribution at backend/agents/graph/early_stop.py).
    ATTRIBUTION_IMPL_DOMINANCE_THRESHOLD: float = 0.75
    ATTRIBUTION_QUALITY_DOMINANCE_THRESHOLD: float = 0.75

    # V-26.95 — early-stop policy tunables
    EARLY_STOP_WARMUP_ROUNDS: int = 5
    EARLY_STOP_PASS_RATE_DROP_RATIO: float = 0.5

    # V-27.92 — Hypothesis abandon decision data source. On (default):
    # should_abandon_hypothesis reads the hypothesis_round_stats table
    # (authoritative, survives worker restart). Off: legacy in-memory
    # state.hypothesis_round_history path. The per-round detail table is
    # ALWAYS written regardless of this flag (additive, zero-risk) — only
    # the abandon DECISION is gated, so this is a clean kill-switch.
    HYPOTHESIS_ABANDON_USE_DB_STATS: bool = True

    # V-27.45 — re-check hypothesis status at alpha/failure INSERT time and
    # drop the link if it has gone terminal (ABANDONED/SUPERSEDED) in the
    # V-22.13 reuse race window. Off → keep the link unconditionally
    # (pre-fix behaviour).
    HYPOTHESIS_REUSE_TERMINAL_GUARD_ENABLED: bool = True

    # V-27.81 — Redis in-flight lock to stop two workers simulating the same
    # (expression_hash, region, universe) between filter_unsimulated_
    # expressions' SELECT and brain.simulate_alpha (a wasted BRAIN slot).
    # Off → skip the lock, fall back to pure DB dedup (pre-fix behaviour).
    SIMULATE_DEDUP_LOCK_ENABLED: bool = True

    # V-27.127 — submit gate-3: when can_submit=False but the ONLY failing
    # checks are self-correlation, defer to the live self_corr precheck
    # (gate-4) instead of hard-blocking on the stale verdict. Off → gate-3
    # hard-blocks on any can_submit != True (pre-fix behaviour).
    SUBMIT_GATE_LIVE_SELF_CORR_OVERRIDE: bool = True

    # V-27.108 — failure-pitfall scoring weights (failure side of V-26.36;
    # the success side is already config'd as RAG_SCORE_*).
    RAG_PITFALL_SEVERITY_HIGH: int = 30
    RAG_PITFALL_SEVERITY_MEDIUM: int = 20
    RAG_PITFALL_SEVERITY_LOW: int = 10
    RAG_PITFALL_SEVERITY_DEFAULT: int = 15
    RAG_PITFALL_CATEGORY_MATCH: float = 20.0
    RAG_PITFALL_ERROR_TYPE_BONUS: float = 15.0

    # V-27.116 — record_success_pattern quality-score formula weights.
    RAG_SUCCESS_SCORE_SHARPE_WEIGHT: float = 0.6
    RAG_SUCCESS_SCORE_FITNESS_WEIGHT: float = 0.3
    RAG_SUCCESS_SCORE_TURNOVER_WEIGHT: float = 0.1
    RAG_SUCCESS_SCORE_SHARPE_DENOM: float = 2.0
    RAG_SUCCESS_SCORE_FITNESS_DENOM: float = 1.5
    RAG_SUCCESS_SCORE_TURNOVER_THRESHOLD: float = 0.7

    # V-27.148 — crisis-window date ranges (was a module constant in
    # correlation_service.py). Add a new crisis event here — no code change.
    CRISIS_WINDOWS: dict = {
        "covid_2020": ["2020-02-20", "2020-04-30"],
        "rate_shock_2022": ["2022-01-03", "2022-06-30"],
        "svb_2023": ["2023-02-15", "2023-04-15"],
        "tariff_2025": ["2025-04-01", "2025-05-31"],
    }

    # P1-D: 参数扰动鲁棒性检验 (window perturbation).
    # 来源: docs/alphagbm_skills_research_2026-05-15.md skill `pnl-simulator`.
    # opt-in (default OFF) — 默认不烧 BRAIN 配额。
    # 集成位置: backend/agents/graph/nodes/evaluation.py graded-score 块之后、
    # baseline-screen 块之前的独立内联段(M-9)。
    # quota 配置:
    #   ROBUSTNESS_SKIP_QUOTA_PCT 整轮 pre-check(today_total + redis_extra >= pct*limit
    #   → skip 全部 alpha);默认 0.65 留 35% buffer。
    #   ROBUSTNESS_HOTCHECK_QUOTA_PCT 单 alpha 完成后 hot-check;默认 0.85。
    ENABLE_ROBUSTNESS_CHECK: bool = False
    ROBUSTNESS_N_PERTURBATIONS: int = 4
    ROBUSTNESS_MIN_RATIO: float = 0.7
    MAX_ROBUSTNESS_PER_ROUND: int = 5
    ROBUSTNESS_SKIP_QUOTA_PCT: float = 0.65
    ROBUSTNESS_HOTCHECK_QUOTA_PCT: float = 0.85
    ROBUSTNESS_PER_ALPHA_TIMEOUT_SEC: int = 600
    ROBUSTNESS_SELECTION_STRATEGY: str = "first"

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    
    class Config:
        case_sensitive = True
        env_file = ".env"
        extra = "ignore"


settings = Settings()
