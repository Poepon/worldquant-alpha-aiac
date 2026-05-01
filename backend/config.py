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
    # T1: 找到方向 — 裸 ts_op 信号；低门槛驱动 T2 种子产能
    TIER1_SHARPE_MIN: float = 0.8
    TIER1_FITNESS_MIN: float = 0.5
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
    TIER3_SELF_CORR_MAX: float = 0.7  # 仅 T3 严格判 self_corr（T1/T2 跳过）

    # PASS_PROVISIONAL 阈值（同梯度，各项放宽 30-40%）
    TIER1_PROVISIONAL_SHARPE_MIN: float = 0.5
    TIER1_PROVISIONAL_FITNESS_MIN: float = 0.3
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
    
    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    
    class Config:
        case_sensitive = True
        env_file = ".env"
        extra = "ignore"


settings = Settings()
