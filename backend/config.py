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
    CASCADE_WATCHDOG_DEAD_MIN: int = 15      # last_alpha_persisted_at < NOW()-N → dead
    CASCADE_WATCHDOG_GRACE_MIN: int = 15     # task.created_at > NOW()-N → skip (start-up grace)
    BRAIN_DAILY_SIMULATE_LIMIT: int = 1000   # consultant 估算 — 实际由 BRAIN 决定
    BRAIN_QUOTA_PAUSE_PCT: float = 0.9       # 达 90% 自动 pause CONTINUOUS_CASCADE

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
    SCORE_PASS_THRESHOLD: float = 0.8      # Composite score to pass
    SCORE_OPTIMIZE_THRESHOLD: float = 0.3  # Score threshold for optimization queue
    
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

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    
    class Config:
        case_sensitive = True
        env_file = ".env"
        extra = "ignore"


settings = Settings()
