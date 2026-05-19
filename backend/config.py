"""
AIAC 2.0 Configuration
Centralized settings management using Pydantic
"""

import os
import json
import logging
from pydantic_settings import BaseSettings
from typing import Any, Optional, Dict

_config_logger = logging.getLogger(__name__)


def _load_thinking_overrides() -> Dict[str, str]:
    """Load per-node thinking_effort overrides.

    Defaults to the conservative tier table (post red-team review):
    hypothesis/code_gen = xhigh, self_correct = low, distill/attribution
    = disabled, strategy/round_analysis/failure_analysis = high. The env
    variable THINKING_EFFORT_OVERRIDES, if set, must be a JSON object;
    its keys merge over the defaults (partial-override semantics). On
    parse failure we warn-log and fall back to defaults — backend MUST
    NOT crash on a malformed override.
    """
    defaults = {
        "hypothesis":       "xhigh",
        "code_gen":         "xhigh",
        "self_correct":     "low",
        "distill_context":  "disabled",
        "attribution":      "disabled",
        "strategy":         "high",
        "round_analysis":   "high",
        "failure_analysis": "high",
    }
    env_val = os.getenv("THINKING_EFFORT_OVERRIDES")
    if not env_val:
        return defaults
    try:
        parsed = json.loads(env_val)
        if not isinstance(parsed, dict):
            raise ValueError("THINKING_EFFORT_OVERRIDES must be a JSON object")
        # Partial-override: env keys win, missing keys keep defaults.
        return {**defaults, **{str(k): str(v) for k, v in parsed.items()}}
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        _config_logger.warning(
            "[config] THINKING_EFFORT_OVERRIDES parse failed, using defaults | error=%s",
            e,
        )
        return defaults


# Module-level cache — loaded once at import time. Bypasses BaseSettings's
# automatic env JSON parsing (which would crash Settings() construction on
# malformed JSON); the helper above handles env + fault tolerance directly.
_THINKING_EFFORT_OVERRIDES_CACHE: Dict[str, str] = _load_thinking_overrides()


# ---------------------------------------------------------------------------
# Runtime feature-flag override cache (P3 — ops dashboard, 2026-05-16)
# ---------------------------------------------------------------------------
# Read by ``Settings.__getattribute__`` for any attribute starting with
# ``ENABLE_``. Written by FeatureFlagService.set/clear (write-through) and
# by the lifespan / worker_process_init refresher loops every 60s.
#
# This dict lives in ``backend.config`` (not in feature_flag_service) so the
# Settings hook below doesn't need a lazy import on every attribute read —
# Pydantic accesses many attributes per request and the cumulative import
# overhead would matter. FeatureFlagService imports this module-level dict
# directly: ``from backend.config import _flag_override_cache``.
#
# The dict starts empty. A flag without an entry here means "no override —
# fall back to the env default that Pydantic loaded at startup".
_flag_override_cache: Dict[str, Any] = {}


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
    # Optional override of Anthropic API endpoint — leave empty to use the
    # official api.anthropic.com. Set to a proxy/mirror (e.g. self-hosted
    # gateway, OpenRouter, vendor-compatible endpoint) when needed.
    ANTHROPIC_BASE_URL: str = os.getenv("ANTHROPIC_BASE_URL", "")
    # Anthropic extended-thinking reasoning effort (opus-4-7 family).
    # Tier names match Anthropic's model capability metadata (low/medium/high/max)
    # plus an intermediate "xhigh" and the alias "auto" → adaptive.
    # Levels:
    #   "disabled" → no thinking block (legacy / fastest)
    #   "low"      → budget_tokens=1024   (Anthropic minimum)
    #   "medium"   → budget_tokens=4096
    #   "high"     → budget_tokens=16384
    #   "xhigh"    → budget_tokens=32000  (DEFAULT — between high and max)
    #   "max"      → budget_tokens=64000  (Anthropic official top effort tier)
    #   "auto"     → model self-allocates budget (alias for Anthropic adaptive mode)
    #   "adaptive" → same as "auto"
    # Ignored on non-reasoning models (haiku/sonnet); openai-compat providers
    # also ignore this setting.
    ANTHROPIC_THINKING_EFFORT: str = os.getenv("ANTHROPIC_THINKING_EFFORT", "xhigh")

    # Per-node thinking_effort kill-switch. False → LLMService ignores the
    # per-node table and falls back to ANTHROPIC_THINKING_EFFORT for every
    # call. One env line + reload is enough to disable runaway costs.
    ENABLE_PER_NODE_THINKING_EFFORT: bool = (
        os.getenv("ENABLE_PER_NODE_THINKING_EFFORT", "true").strip().lower()
        in ("1", "true", "yes")
    )

    # Per-node thinking_effort overrides. Keys are prompt registry keys
    # (see backend/agents/prompts/registry.py); values are effort tiers
    # (low/medium/high/xhigh/max/auto/adaptive/disabled). Loaded module-level
    # to bypass Pydantic's env JSON validation (which would crash on bad JSON);
    # see _load_thinking_overrides() for env handling + fault tolerance.
    @property
    def THINKING_EFFORT_OVERRIDES(self) -> Dict[str, str]:
        return _THINKING_EFFORT_OVERRIDES_CACHE

    # Mining Configuration
    DEFAULT_REGION: str = "USA"
    DEFAULT_UNIVERSE: str = "TOP3000"
    DEFAULT_DAILY_GOAL: int = 4
    
    # Quality Thresholds (Traditional — kept as fallback / pre-tier baseline)
    SHARPE_MIN: float = 1.5
    TURNOVER_MAX: float = 0.7
    FITNESS_MIN: float = 1.0
    MAX_CORRELATION: float = 0.7

    # ----- BRAIN Consultant Mode (P3-Brain — manual role switch) -----
    # 走 ENABLE_BRAIN_CONSULTANT_MODE bool 复用现有 __getattribute__ hook
    # (line 809-812)。不引入 BRAIN_ROLE 业务字段(被 SUPPORTED_FLAGS 哲学禁止,
    # 见 feature_flag_service.py:25)。手动从 ops dashboard 翻 — 收到 BRAIN
    # 升级邮件后由用户翻一次,系统不做自动探测。
    ENABLE_BRAIN_CONSULTANT_MODE: bool = False
    CONSULTANT_SHARPE_SUBMIT_MIN: float = 1.58
    CONSULTANT_DEFAULT_TEST_PERIOD: str = "P0Y"
    # phase 1 已验证 region+universe;phase 2 TWN/KOR/GLB/AMR 在后续 PR 实测后加。
    CONSULTANT_REGION_UNIVERSES: dict = {
        "USA": "TOP3000",
        "CHN": "TOP2000A",
        "HKG": "TOP500",
        "JPN": "TOP1600",
        "EUR": "TOP2500",
    }
    # BRAIN 账号级并发 simulation 槽上限(USER=3 / CONSULTANT=80)。
    # endpoint 选择能力分类(CLAUDE.md 方向 C)— 走全局 ENABLE_BRAIN_CONSULTANT_MODE
    # 而非 task 启动快照,切回 USER 即时降回 3 避免 USER 状态撞 BRAIN 429
    # CONCURRENT_SIMULATION_LIMIT_EXCEEDED。brain_adapter._acquire_sim_slot 读这里。
    BRAIN_SIM_SLOT_LIMIT_USER: int = 3
    BRAIN_SIM_SLOT_LIMIT_CONSULTANT: int = 80

    # ----- R1a: enhance_existing_node_evaluate hook (Phase 0, 2026-05-17) -----
    # 启用 backend/agents/core/integration.py:342-407 DORMANT shim,把
    # AttributionType (HYPOTHESIS/IMPLEMENTATION/BOTH/UNKNOWN) 写入
    # alpha.metrics["_r1a_attribution"] 等 7 个字段,为 Phase 1 R2/Q7 bandit
    # arm 集设计提供 attribution 反证数据。
    # 数据量观察期门槛(详 docs/master_implementation_plan_2026-05-17.md §4.1
    # + ~/.claude/plans/docs-master-implementation-plan-2026-05-compressed-shore.md §7):
    # - 触发 ≥ 200 / non_null_pct ≥ 95% / non_unknown_pct ≥ 70% / errs < 10
    # - 0 production crash(per-alpha try/except 守护)
    # 默认 False,通过 FeatureFlagOverride (flag_type=bool, flag_value="true")
    # 翻开,无需重启。软回滚:UPDATE feature_flag_overrides SET flag_value='false'
    # (< 1 分钟)。
    ENABLE_R1A_HOOK: bool = False

    # ----- R4' Dual-channel RAG (Phase 1, 2026-05-17) -----
    # 把 hypothesis prompt 的 Historical Patterns 段 (现单段渲染
    # success_patterns + failure_pitfalls) 拆成 Channel A (✓ worked) +
    # Channel B (⛔ avoided) 视觉分离,提高 LLM positive/negative 信号区分度。
    # OFF 时 byte-for-byte 走 legacy 单段渲染(P2-D nudge 兼容)。
    # 双文件注册:本文件 + backend/services/feature_flag_service.py
    # (per [[feedback_enable_flag_double_file]] Phase 0 v1.4 教训)。
    ENABLE_DUAL_CHANNEL_RAG: bool = False

    # ----- R2/Q7 Contextual Thompson Sampling DirectionBandit (Phase 1, 2026-05-17) -----
    # Beta-Bernoulli per-(segment, arm) bandit 选 strategy 生成方式。
    # arms = R1a 18:3 hypothesis-dominant 反证集合 (plan §4.2):
    #   {rag_template, knowledge_pattern, llm_generation, genetic_mutation}
    # context = (region, dataset_category, recent_failure_pattern) — 三维
    # segment_id 用字符串拼接 (MF-V1.2-4 防 Python hash() 跨进程不稳定)。
    # cold-start: segment < 5 pulls 走 global prior fallback。
    # state 持久化在 mining_tasks.config["contextual_bandit_v1"] JSONB。
    # off-policy log 写独立 direction_bandit_log 表 (per
    # [[feedback_r1a_dedicated_log_table]] Phase 0 v1.6 教训)。
    # 双文件注册:本文件 + backend/services/feature_flag_service.py。
    ENABLE_DIRECTION_BANDIT: bool = False
    DIRECTION_BANDIT_ARMS: list = [
        "rag_template",
        "knowledge_pattern",
        "llm_generation",
        "genetic_mutation",
    ]
    DIRECTION_BANDIT_COLD_THRESHOLD: int = 5
    # G1 Phase A (2026-05-19): per plan §1.9, the R2/Q7 GO gate fires when at
    # least one segment has ≥ DIRECTION_BANDIT_GO_GATE_MIN_PULLS observed
    # selects in the telemetry window. Below this, Thompson posterior is too
    # noisy to draw arm-promotion conclusions. The /ops/direction-bandit/
    # telemetry endpoint reports go_gate_segments_ready against this value.
    DIRECTION_BANDIT_GO_GATE_MIN_PULLS: int = 10

    # ----- R3/Q8 AST subtree-isomorphism diversity dim (Phase 1, 2026-05-17) -----
    # Adds a 6th dim `ast_diversity` to DiversityScore based on Jaccard subtree
    # overlap (brute-force O(n²), fine for AIAC AST n<20 at max_depth=3).
    # Light wiring per plan §2.4: every node_code_gen alpha gets distance
    # computed + logged to ast_distance_log table, BUT diversity score is NOT
    # used as a hard gate (Phase 1.5 / 2+ work).
    # 双文件注册:本文件 + backend/services/feature_flag_service.py。
    ENABLE_AST_DIVERSITY_DIM: bool = False
    AST_DIVERSITY_MAX_DEPTH: int = 3       # OperatorNode skeleton truncation
    AST_DIVERSITY_HISTORY_K: int = 20      # compare new alpha to top-K recent attempts

    # ----- G3 AST originality gate (Phase A shadow, 2026-05-19) -----
    # Promotes Phase 1 R3/Q8 ast_distance_log → candidate-time gate in
    # node_evaluate (after R10 family-cap). Three modes:
    #   shadow — log warning + alpha.metrics['_g3_*'] only (Phase A default)
    #   soft   — block flips quality_status='PASS_PROVISIONAL' (still simulates)
    #   hard   — block flips quality_status='FAIL' (skip persistence)
    # τ (AST_ORIGINALITY_MIN_DISTANCE) standardization: ast_distance returns
    # 1 − Jaccard(subtree_sets), so values are bounded [0, 1]. τ=0.15 = "alpha
    # shares ≥85% of subtree skeletons with its nearest neighbor". Calibrate
    # via /ops/g3/originality-stats + scripts/calibrate_g3_threshold.py.
    # 双文件注册:本文件 + backend/services/feature_flag_service.py
    # (per [[feedback_enable_flag_double_file]]).
    ENABLE_AST_ORIGINALITY_GATE: bool = False
    AST_ORIGINALITY_MODE: str = "shadow"        # shadow | soft | hard
    AST_ORIGINALITY_MIN_DISTANCE: float = 0.15  # τ — Phase B re-calibrate
    AST_ORIGINALITY_HISTORY_K: int = 50         # compare new alpha vs top-K recent

    # ----- flat-F1 Advanced: FLAT_CONTINUOUS mining mode (Phase 3, 2026-05-18) -----
    # 第二个 mining_mode = FLAT_CONTINUOUS,与 legacy CONTINUOUS_CASCADE 并行。
    # Hypothesis-driven flat session:dataset × hypothesis 元组迭代,无 T1→T2→T3
    # 级联。master plan §6 D3 "保留 legacy(渐进切换)" — F1 默认 OFF,默认 mining_mode
    # 不变(仍 CONTINUOUS_CASCADE / DISCRETE);flat-F2 后续 PR 翻默认值。
    # 双文件注册:本文件 + backend/services/feature_flag_service.py(per
    # [[feedback_enable_flag_double_file]] Phase 0 v1.4 教训)。
    # plan: ~/.claude/plans/flat-F1-kickoff-2026-05-18.md v1.5 SHIP-READY。
    ENABLE_FLAT_CONTINUOUS: bool = False
    FLAT_CONTINUOUS_DAILY_GOAL: int = 20         # alphas/iteration cap
    FLAT_CONTINUOUS_MAX_ITERATIONS: int = 100    # safety bound per session

    # ----- flat-F2 default mining_mode flip (Phase 3, 2026-05-18) -----
    # 默认 mining_mode 翻 CONTINUOUS_CASCADE → FLAT_CONTINUOUS — POST
    # /mining-session/start 不再创建 cascade task,改创 flat task。
    # 前置:ENABLE_FLAT_CONTINUOUS + ENABLE_DAG_TRACE 都 ON(R6 给 flat
    # reward-guided exploration,避 linear cursor regression)+ flat-F1
    # 2 周灰度 PASS。决策 5A lock。
    # 默认 OFF — 翻 ON 后新 task 走 flat,既有 cascade task 不影响。
    # 双文件注册:本文件 + backend/services/feature_flag_service.py。
    ENABLE_DEFAULT_FLAT_SESSION: bool = False

    # ----- flat-F3 LLM-driven wrapper mutation (Phase 3, 2026-05-18) -----
    # LLM 看 _failed_tests + P2-D pitfalls 选 2-3 wrappers,替代盲目穷举。
    # 降 BRAIN sim cost 40-75%,提 PASS rate (LLM 偏避有名失败模式)。
    # Soft-fail:LLM 失败 fall back to legacy enumerate。
    # 双文件注册:本文件 + backend/services/feature_flag_service.py。
    ENABLE_LLM_MUTATE_ALPHA: bool = False
    LLM_MUTATE_TOP_K: int = 3                    # cap variants per seed
    LLM_MUTATE_MODEL: str = "claude-haiku-4-5-20251001"  # cost-effective default

    # ----- R9 simulation cache (Phase 3, 2026-05-18) -----
    # Cache BRAIN sim results keyed on (region, universe, expression, settings).
    # Hit → skip BRAIN call, return cached result; miss → BRAIN sim + write cache。
    # Est. 40-60% BRAIN cost reduction on duplicate-heavy workloads (cascade
    # T2/T3 wrappers, flat dataset cycling)。
    # TTL: SIMULATION_CACHE_TTL_DAYS (default 14) — beyond TTL row treated as
    # miss but kept (analytics; manual SQL purge if needed)。
    # 双文件注册:本文件 + backend/services/feature_flag_service.py。
    # Alembic head: 9a4f7e8c1d6b。
    ENABLE_SIMULATION_CACHE: bool = False
    SIMULATION_CACHE_TTL_DAYS: int = 14
    SIMULATION_CACHE_ONLY_SUCCESS: bool = True   # only cache success results by default

    # ----- R8 Hierarchical RAG (Phase 3, 2026-05-18) -----
    # 4-layer fall-through retriever (RAG#0 exact pattern_hash → RAG#1
    # pillar/theme → RAG#2 family_signature → RAG#3 field-level). Each
    # layer fills remaining_budget; cap stops orchestrator. Layer impls
    # in backend/agents/services/rag_service.py (additive overlay,
    # legacy query() preserved when flag OFF).
    # Default OFF — flag ON dispatch query_hierarchical (新入口), OFF
    # byte-equivalent legacy。
    # 双文件注册:本文件 + backend/services/feature_flag_service.py。
    # plan: ~/.claude/plans/phase3-r8-hierarchical-rag-2026-05-18.md v1.0
    # Alembic head: b3c8d9e2f4a1 (KB meta_data GIN index)
    ENABLE_HIERARCHICAL_RAG: bool = False
    RAG_HIER_LAYER0_BUDGET: int = 5   # exact pattern_hash matches
    RAG_HIER_LAYER1_BUDGET: int = 5   # pillar/theme matches
    RAG_HIER_LAYER2_BUDGET: int = 5   # family_signature matches
    RAG_HIER_LAYER3_BUDGET: int = 5   # field-level matches
    RAG_HIER_TOTAL_CAP: int = 20      # orchestrator hard cap
    RAG_HIER_CACHE_TTL_SEC: int = 300 # Redis cache TTL per layer query
    RAG_HIER_CROSS_REGION_DECAY: float = 0.7  # 跨 region 命中 score 折扣

    # R8-v2 #3 (2026-05-18): R5 composite_score ranking for L2 SUCCESS。
    # layer2_family fetched 候选会 JOIN r1a_attribution_log.r5_composite_score
    # AVG GROUP BY expression_hash (sha256[:64] of KB pattern,匹配
    # evaluation.py:2631 R1a hook 写入约定),按 R5 mean score 降序重排;有样
    # 本 row 用 0.45+0.4*avg 折算 relevance_score (range [0.45,0.85])。零样本
    # row 保原 0.65 默认。Soft-fail SQL error → 原顺序。前置 R5 ENABLE_LLM_JUDGE
    # ON 累积 r1a_attribution_log r5_composite_score 非 NULL。
    # (Retired ENABLE_R5_L2_RANKING flag 2026-05-19 — hard-wired ON;subsumed
    #  into ENABLE_HIERARCHICAL_RAG main switch.)
    R5_L2_RANKING_MIN_SAMPLES: int = 1   # 至少 N r1a sample 才参与重排
    R5_L2_RANKING_LOOKBACK_DAYS: int = 30  # AVG 窗口

    # ----- Phase 3 Q10: pyqlib local pre-screen (Multi-Fidelity Layer 0) -----
    # 2026-05-18 — plan: ~/.claude/plans/phase3-q10-pyqlib-prescreen-2026-05-18.md v1.3
    # Slot in front of BRAIN simulate: translate BRAIN expression → qlib DSL,
    # evaluate on local OHLCV snapshot for approximate Sharpe/IC; reject ones
    # below floor (saves BRAIN sim cost).
    # 3-stage rollout: shadow (log only) → soft (warn metric) → hard (reject).
    # 3-tier engine degrade: pyqlib_live → pyqlib_snapshot → pandas_snapshot
    # → disabled. Coverage ~30-45% T1 traffic (price-volume-only alphas;
    # fnd*/analyst_*/group_neutralize/trade_when are untranslatable by design).
    # 双文件注册:本文件 + backend/services/feature_flag_service.py。
    ENABLE_QLIB_PRESCREEN: bool = False
    QLIB_PRESCREEN_MODE: str = "shadow"  # shadow | soft | hard
    QLIB_PRESCREEN_SHARPE_FLOOR: float = 0.3
    QLIB_PRESCREEN_IC_FLOOR: float = 0.005
    QLIB_PRESCREEN_TIMEOUT_MS: int = 1500    # hard per-alpha timeout
    QLIB_DATA_DIR: str = os.getenv("QLIB_DATA_DIR", "backend/data/qlib_data")
    QLIB_SNAPSHOT_DIR: str = os.getenv("QLIB_SNAPSHOT_DIR", "backend/data/qlib_ohlcv_snapshot")
    QLIB_ENGINE_PREFER_PANDAS: bool = False  # force tier-3 for testing

    # ===== Phase 3 R1b CoSTEER loop activation (2026-05-18) =====
    # Plan: ~/.claude/plans/phase3-r1b-costeer-loop-2026-05-18.md v1.3
    # Each sub-phase has its own flag for independent rollout per
    # [[feedback_light_wiring_deferred_gate]]. Default OFF — every flag
    # registered in backend/services/feature_flag_service.py SUPPORTED_FLAGS.
    # ----- R1b.1 — implementation retry loop -----
    ENABLE_R1B_RETRY_LOOP: bool = False
    R1B_MAX_RETRIES_PER_ALPHA: int = 3
    R1B_RETRY_MODEL: str = "claude-haiku-4-5-20251001"
    # ----- R1b.2 — hypothesis mutation loop -----
    # BOTH attribution → mutate dominates retry per plan [V1.0-A2-3].
    ENABLE_R1B_HYPOTHESIS_MUTATE: bool = False
    R1B_MAX_MUTATIONS_PER_DATASET_CYCLE: int = 2
    # R1b.2 review MEDIUM (2026-05-18): per-round cap was inadequate against
    # cross-round mutation chain spirals (round N mutate → round N+1 inject →
    # fail (hyp attribution) → round N+1 mutate → round N+2 inject → ...).
    # The Hypothesis row stores r1b_mutation_depth (bumped at INSERT in
    # _insert_mutated_hypothesis); node_hypothesis_mutate now reads the
    # parent's depth and refuses when >= this cap to prevent runaway BRAIN
    # + LLM cost on shallow hypothesis spaces.
    R1B_MAX_MUTATION_DEPTH: int = 3
    R1B_MUTATE_MODEL: str = "claude-haiku-4-5-20251001"
    # ----- R1b.3 — cross-round failure trees -----
    ENABLE_R1B_FAILURE_TREE: bool = False
    R1B_FAILURE_TREE_MAX_DEPTH: int = 4
    # R1b.3 review LOW (2026-05-18): retention TTL for FAILURE_PITFALL rows
    # that carry meta_data->'failure_tree'. record_failure_tree dedupes on
    # root_skeleton(200 chars) via UPSERT but the table still grows linearly
    # at scale (50 alpha/round × N rounds × multi-root mutations). The
    # weekly Sunday 04:00 SH beat task ``run_failure_tree_pruner`` deletes
    # rows older than this value. Operational tunable — no feature flag.
    R1B_FAILURE_TREE_RETENTION_DAYS: int = 90
    # ----- R1b.4 — typed AlphaMiningPipeline route -----
    # hypothesis_centric_variant=3 task opt-in;coexists with R1b.1/R1b.2.
    ENABLE_R1B_TYPED_PIPELINE: bool = False
    R1B_TYPED_NUM_ITER_PER_ROUND: int = 3  # how many run_iteration calls per outer round
    # ----- R1b.5 — R6 DAG retry-aware reward -----
    # Pre-req R1b.1+R1b.2 GO gates + ≥14d observation.
    ENABLE_R1B_DAG_RETRY_REWARD: bool = False
    # ----- Shared budget guard -----
    R1B_TOKEN_COST_CEILING_USD_PER_ALPHA: float = 0.05
    # ----- R1b.1 review LOW 2 — per-round cost cap -----
    # Soft cap on cumulative R1b LLM cost (retry + mutate) within a single
    # LangGraph invocation (one round). When state.r1b_cost_this_round +
    # estimated_next_call_cost would exceed this, the retry/mutate node
    # skips the LLM call + logs info (alpha NOT failed — just left as-is).
    # Worst-case envelope without cap: $0.05 × 3 retries × 50 alphas = $7.50
    # /round × 100 rounds/day = $750/day. With 5.00 default a round soft-caps
    # at $5; 100 rounds/day worst case $500/day — still bounded.
    # 双文件注册:本文件 + backend/services/feature_flag_service.py。
    R1B_MAX_COST_USD_PER_ROUND: float = 5.00

    # R8-v2 #2 (2026-05-18): per-layer Redis cache for hierarchical RAG。
    # cache_key = sha256[:16](layer + sorted params),TTL = RAG_HIER_CACHE_TTL_SEC
    # (default 300s)。无显式 invalidation — KB 写入频率 3-50/h,5-min stale window
    # 在 plan §10 GO gate 容忍范围。redis 不可用 → soft-fall direct layer call。
    # (Retired ENABLE_HIERARCHICAL_RAG_CACHE flag 2026-05-19 — hard-wired ON;
    #  subsumed into ENABLE_HIERARCHICAL_RAG main switch.)

    # ----- R8 query-level telemetry (2026-05-18 follow-up) -----
    # Per-call layer_hits + cache_hit + had_failure_tree_elevation row in
    # r8_query_log. Default OFF — zero overhead on hot RAG path until
    # operator promotes (typical use: enable during 7d obs window after
    # ENABLE_HIERARCHICAL_RAG flip to measure layer fall-through patterns).
    # 双文件注册:本文件 + backend/services/feature_flag_service.py。
    ENABLE_R8_QUERY_LOG: bool = False
    # R8 query telemetry review LOW (2026-05-18): TTL for r8_query_log
    # rows. With ENABLE_R8_QUERY_LOG=False default this is a no-op, but
    # long-term ON promotion would let the table grow unbounded (one row
    # per query_hierarchical call). Weekly Sunday 04:30 SH beat task
    # ``run_r8_query_log_pruner`` deletes rows older than this value.
    # Operational tunable — no feature flag.
    R8_QUERY_LOG_RETENTION_DAYS: int = 90

    # phase15-D PR3c (2026-05-18): ENABLE_CASCADE_LEGACY flag RETIRED.
    # Cascade dispatch + router + watchdog probe now refuse
    # unconditionally — the flag became a no-op so it's removed
    # alongside the cascade code paths it gated. Existing
    # FeatureFlagOverride rows for this name silently noop when
    # read via feature_flag_service (orphan flag — see
    # SUPPORTED_FLAGS in backend/services/feature_flag_service.py).

    # ----- Phase 2 R5: Hypothesis-Alignment LLM judge (2026-05-18) -----
    # AlphaAgent Eq. 7: C(h, d, f) = α·c₁(h, d) + (1-α)·c₂(d, f), α=0.5
    # c₁ judges hypothesis ↔ description; c₂ judges description ↔ expression
    # 失败时 attribution 标 AttributionType.hypothesis/implementation/both
    # 与 R1a heuristic 互补:R5 verdict 非 None 时 OVERWRITE R1a 字段(R5 wins
    # per plan v1.0 [V1.0-A2-3])。R5 None(both PASS / low confidence)时
    # 保 R1a。原 R1a verdict 存 r5_agrees_r1a 供分析。
    # 成本:haiku-4-5 med effort ~$0.01/call,GO gate $0.05/call 满足。
    # 双文件注册:本文件 + backend/services/feature_flag_service.py。
    # plan: ~/.claude/plans/phase2-r5-llm-judge-2026-05-18.md v1.0
    ENABLE_LLM_JUDGE: bool = False
    R5_JUDGE_MODEL: str = "claude-haiku-4-5-20251001"   # cheaper than opus, med effort
    R5_JUDGE_LOW_CONF: float = 0.55                      # below this R5 abstains → R1a wins

    # ----- Phase 2 R7 Co-STEER self-correct 半接受机制 (2026-05-18) -----
    # 防 self_correct 把 alpha 改成另一个 broken expression — LLM 修正后用
    # alpha_semantic_validator 快速 re-validate;新版本必须 VALID OR
    # 严格少 hard findings 才 accept,否则保原 + 标 _r7_self_correct_rejected。
    # 来源:rd_agent §6 R7 Co-STEER `should_use_new_evo`(原 spec 比 score,
    # 这里 validation-time 比 finding count;score-based 需 simulation 数据
    # 留 R7-v2 等 R6 DAG ship 后再做)。
    # 默认 OFF — flag ON 后 retry_count 行为不变(LLM 仍 1-3 次,reject 占 1 次)。
    ENABLE_SELF_CORRECT_SEMI_ACCEPT: bool = False

    # ----- Phase 2 R6 DAG Trace (MCTS-lite, 2026-05-18) -----
    # 替换 linear T1→T2→T3 cascade_phase scalar 推进为 DAG-structured
    # multi-branch trace persisted in experiment_runs.runtime_state["dag"] JSONB
    # (Phase 1.5-A 已加 col,无 Alembic 需要)。R6-v1 简化-MCTS(best-path expand
    # + reward backprop);full UCT 推 R6-v2。
    # 落地:backend/agents/graph/dag_state.py 纯函数 helpers + mining_tasks.py
    # 集成 + _resolve_cascade_phase 3-层 fallback(DAG → current_tier → cascade_phase)。
    # 默认 OFF — 翻 OFF 时 phase15-C 路径不变 byte-equivalent。
    # 双文件注册:本文件 + backend/services/feature_flag_service.py。
    # plan: ~/.claude/plans/phase2-r6-dag-2026-05-18.md v1.0
    ENABLE_DAG_TRACE: bool = False
    DAG_MAX_NODES: int = 100               # write-side hard cap (~25KB at avg 250B/node)
    DAG_MAX_DEPTH: int = 10                # parent-chain depth cap
    DAG_UCB_EXPLORATION_C: float = 1.4     # UCB1 c parameter (warm leaves)
    DAG_COLD_THRESHOLD: int = 3            # n_pulls < this → Thompson sampling
    DAG_PRUNE_LRU_KEEP_FRACTION: float = 0.7  # prune keeps top 70% by reward/LRU

    # ----- Phase 2 R10 Family-cap (Hubble v2 Table 1, 2026-05-18) -----
    # 同 pillar 同 family(operator-sequence signature)只保留 top-K=2 by score。
    # 防止一个 op pipeline 在评估批次刷榜挤掉异质 alpha。
    # 落地:evaluation.py R1a/R5 hook 之后调 family_classifier.apply_family_cap,
    # 超出 K 的标 quality_status="FAIL" + metrics["_r10_family_cap_dropped"]=True。
    # 双文件注册:本文件 + backend/services/feature_flag_service.py。
    ENABLE_FAMILY_CAP: bool = False
    FAMILY_CAP_TOP_K: int = 2          # Hubble v2 default;OFF + flip K=5 用于探索期

    # ----- Phase 1.5-C: TaskSchema v2 cut-over (2026-05-18) -----
    # 切 read paths 从 legacy cols (mining_mode / cascade_phase / agent_mode)
    # 到 new authoritative cols (schedule / starting_tier / runtime_state).
    # 默认 OFF 保留 legacy 行为 — dual-write 已在 Phase 1.5-B 启 → flag 翻 ON
    # 应 byte-equivalent 在 Revision B 之后创建的 task 上。
    # Gray rollout: staging → single task → region 全量(plan §3.5)。
    # 双文件注册:本文件 + backend/services/feature_flag_service.py。
    ENABLE_TASK_SCHEMA_V2: bool = False

    # ----- Flat evaluation thresholds (post tier-system removal, 2026-05-18) -----
    # Single threshold band replaces the old TIER1/TIER2/TIER3 ladder. Values
    # chosen at the strict end of the old ladder (≈ old T3) per master plan
    # decision: "alpha 产出变少但质量高". The PROVISIONAL gap to PASS is 0.25
    # sharpe, matching the old T2→T3 gap.
    #
    # brain_role_snapshot.effective_sharpe_submit_min still overrides
    # EVAL_SHARPE_MIN at runtime via the _eval_thresholds helper in
    # backend/agents/graph/nodes/evaluation.py, so Consultant-mode tasks keep
    # their elevated bar.
    EVAL_SHARPE_MIN: float = 1.5
    EVAL_FITNESS_MIN: float = 1.2
    EVAL_TURNOVER_MIN: float = 0.01
    EVAL_TURNOVER_MAX: float = 0.4
    EVAL_SUBUNIV_MIN: float = 0.2
    EVAL_SELF_CORR_MAX: float = 0.7

    EVAL_PROVISIONAL_SHARPE_MIN: float = 1.25
    EVAL_PROVISIONAL_FITNESS_MIN: float = 1.0
    EVAL_PROVISIONAL_TURNOVER_MAX: float = 0.55
    EVAL_PROVISIONAL_SUBUNIV_MIN: float = 0.15

    EVAL_SCORE_PASS: float = 0.8
    EVAL_SCORE_OPTIMIZE: float = 0.3

    # V-19 Persistent Mining Service mode (2026-05-10) — session-loop pacing.
    # CASCADE_PAUSE_POLL_SEC is shared with FLAT_CONTINUOUS sessions (the
    # round-end PAUSED check) post tier-removal.
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
    # gate. Catches alphas where the LLM picked the right field/op but the
    # wrong sign convention. Bounded by T1_FLIP_RETRY_CAP per round.
    # (Retired ENABLE_T1_SIGN_FLIP_RETRY flag 2026-05-19 — hard-wired ON.)
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

    # P2-D (2026-05-15): Negative-knowledge nudge + daily extract task.
    # 来源: docs/alphagbm_skills_research_2026-05-15.md skills `take-profit`
    # + `health-check`. Default OFF — the nudge block in
    # backend/agents/graph/nodes/generation.py is skipped byte-for-byte when
    # ENABLE_NEGATIVE_KNOWLEDGE_NUDGE is False (PromptContext.failure_pitfalls
    # = state.pitfalls[:5] unchanged). Switch to True after observing 1-2 days
    # of docs/negative_knowledge/<sh-date>.json.
    ENABLE_NEGATIVE_KNOWLEDGE_NUDGE: bool = False
    NEGATIVE_KNOWLEDGE_TOP_K: int = 5            # max pitfalls fetched per call
    NEGATIVE_KNOWLEDGE_MIN_FAIL_COUNT: int = 3   # promote-to-LLM threshold
    NEGATIVE_KNOWLEDGE_RETROSPECTIVE_WINDOW_HOURS: int = 24

    # P2-A (2026-05-16): Macro-narrative RAG nudge + daily extract task.
    # 来源: docs/alphagbm_skills_research_2026-05-15.md skill `macro-view`.
    # Both flags default OFF (M9: token budget guard + P2-B/P2-D precedent).
    # GUIDANCE flag gates the prompt injection inside node_hypothesis;
    # EXTRACT flag gates the LLM-batch fill-in inside the daily extract
    # task — seed UPSERTs run unconditionally (idempotent, no cost).
    # Both flip-on validated independently after 1-2 days of seed-only run.
    ENABLE_MACRO_NARRATIVE_GUIDANCE: bool = False    # prompt 注入
    ENABLE_MACRO_NARRATIVE_EXTRACT: bool = False     # LLM 批生成
    MACRO_NARRATIVE_FIELD_TOP_K: int = 3
    MACRO_NARRATIVE_LLM_BATCH_SIZE: int = 20
    MACRO_NARRATIVE_LLM_MAX_PER_DAY: int = 500
    MACRO_NARRATIVE_CACHE_TTL_SECONDS: int = 600

    # P2-C (2026-05-16): regime-aware threshold gating + style preset encoding.
    # 来源: docs/alphagbm_skills_research_2026-05-15.md skills vix-status /
    # duan-analysis. Default OFF (S1 + P2-A/B/D 惯例). regime_at_eval stamp
    # 仅当 strategy.regime 被注入时触发(stage ≥ "thresholds" 即可)。
    #
    # Consolidated 2026-05-19 — single switch ENABLE_REGIME + REGIME_STAGE str
    # replaces the previous 3 booleans (ENABLE_REGIME_INFERENCE /
    # ENABLE_REGIME_AWARE_THRESHOLDS / ENABLE_STYLE_PRESET_GUIDANCE). The 3
    # legacy names remain as read-only @property derivations so all callers
    # keep working byte-for-byte. Staged rollout (mirrors Q10/G3):
    #   - REGIME_STAGE="inference"  → 攒 1-2 天 docs/regime_state/<sh-date>.json
    #   - REGIME_STAGE="thresholds" → 倍率生效 + 数据采集 stamp
    #   - REGIME_STAGE="style"      → 注入投资哲学 block 进 hypothesis prompt
    ENABLE_REGIME: bool = False
    REGIME_STAGE: str = "inference"  # one of: inference / thresholds / style
    REGIME_INFERENCE_WINDOW_DAYS: int = 7
    REGIME_EWMA_ALPHA: float = 0.3
    REGIME_CACHE_TTL_SECONDS: int = 86400   # 24h

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
    # (Retired ENABLE_LLM_THESIS_SCORE_ON_PROMOTED + ENABLE_LLM_THESIS_SCORE_ON_TRIGGER
    #  flags 2026-05-19 — hard-wired ON. ON_PROMOTED had no reader; ON_TRIGGER
    #  was a degenerate guard alongside `if self.llm is None`.)
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

    # PR7 — wrapper-aware simulation settings. node_simulate buckets expressions
    # by per-alpha settings (chosen via smart_simulation_settings based on
    # expression form + field category) and calls simulate_batch per bucket.
    # Root cause for the flip: backfill found 0% of mining-produced alphas can
    # submit — double-neutralization (group_* wrapper + BRAIN neut=SUBINDUSTRY)
    # and decay-vs-trade_when conflicts.
    # (Retired ENABLE_SMART_SIM_SETTINGS flag 2026-05-19 — hard-wired ON.)

    # Plan v5+ #3 (2026-05-07): pre-simulate skeleton classifier filter.
    # node_simulate runs each candidate through the trained sklearn
    # LogisticRegression and skips alphas with P(PASS) < threshold BEFORE BRAIN
    # simulate. Saves BRAIN concurrent-slot time on likely-fails.
    # Model: AUC=0.813 on 2737 historical alphas (451 PASS).
    # V-24.B (2026-05-13): conservative threshold 0.10:
    #   0.05  → 98.9% PASS recall, skips  2.2% FAIL (negligible savings)
    #   0.10  → 98.0% PASS recall, skips  7.1% FAIL  ← current default
    #   0.15  → 96.5% PASS recall, skips 17.0% FAIL  ← recommended after audit
    # (Retired ENABLE_PRE_SIMULATE_FILTER flag 2026-05-19 — hard-wired ON.)
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
    # 历史 KB-referenced alpha 的 metrics 不会随时间变化（IS 冻结），
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

    # ----- G2 Phase A — per-call LLM cost telemetry (2026-05-19) -----
    # Light wiring per [[feedback_light_wiring_deferred_gate]]: Phase A logs
    # every LLMService.call to llm_call_log table (task_id / round_idx /
    # node_key contextvar-resolved), no behavior change. Phase C (~7d obs
    # later) will promote to cost-aware throttling using LLM_PRICING + a new
    # COST_CEILING_USD_PER_TASK_DAY check.
    # Default OFF — flip via FeatureFlagOverride (双文件注册 per
    # [[feedback_enable_flag_double_file]]). Soft-fail: tracker exception
    # never breaks LLM hot path.
    ENABLE_COST_TELEMETRY: bool = False
    # Pricing dict — blended per-1k-token rate by model prefix. Used at
    # round-flush time to derive cost_usd from raw tokens (Phase A keeps it
    # simple; if prompt vs completion split ever matters we can swap dict
    # values to {"prompt": x, "completion": y} without schema change).
    # Source: provider public pages as of 2026-05-19. Unknown models fall
    # back to 0.0 (cost_usd=None in the row), tokens still recorded.
    LLM_PRICING_USD_PER_1K_TOKENS: dict = {
        "deepseek-chat": 0.00027,        # DeepSeek V3 blended input/output
        "deepseek-reasoner": 0.00055,    # DeepSeek R1 blended
        "claude-haiku-4-5": 0.00125,     # Anthropic Haiku 4.5 blended
        "claude-sonnet-4-6": 0.0075,     # Anthropic Sonnet 4.6 blended
        "claude-opus-4-7": 0.0375,       # Anthropic Opus 4.7 blended
    }
    # 90-day retention for llm_call_log — weekly Sunday 04:45 SH beat task
    # ``run_llm_call_log_pruner`` deletes rows older than this value (rounded
    # up to the day). Match R8_QUERY_LOG_RETENTION_DAYS pattern. Operational
    # tunable — no feature flag.
    LLM_CALL_LOG_RETENTION_DAYS: int = 90

    # ----- G5 Phase A — Trajectory crossover (2026-05-19) -----
    # QuantaAlpha arxiv 2602.07085 (2026-02). Combine 2 high-reward PASS
    # alpha "siblings" into hybrid offspring via LLM. Offspring persist on
    # task.config["g5_pending_offspring"] via R1b.2-v2 same mechanism; next
    # round consumes + injects into MiningState.g5_offspring_candidates;
    # node_code_gen prepends them to pending_alphas so they walk full
    # validate → simulate → evaluate → save_results pipeline.
    # OFF byte-for-byte legacy. Soft-fail全链 — crossover 异常永不 block round。
    # 双文件注册:本文件 + backend/services/feature_flag_service.py。
    ENABLE_G5_CROSSOVER: bool = False
    # 选 sibling pair 的过滤 — 要求两个 parent 都 PASS sharpe ≥ X 才 trigger。
    # Lookback 是 SQL window(最近 X round 内本 task PASS alpha 池)。
    # max_pair_pillar_overlap True 时不允许两 parent 同 pillar(强制 diversity)。
    G5_CROSSOVER_MIN_PARENT_SHARPE: float = 1.25
    G5_CROSSOVER_LOOKBACK_ROUNDS: int = 10
    G5_CROSSOVER_TOP_K_OFFSPRING: int = 2
    G5_CROSSOVER_REQUIRE_DIFFERENT_PILLAR: bool = True
    # LLM model override — None / "" uses LLMService default。同 LLM_MUTATE_MODEL 模式
    LLM_CROSSOVER_MODEL: str = ""

    # ----- G8 Phase A — Hypothesis forest cross-task reference (2026-05-19) -----
    # RD-Agent NeurIPS 2025 假设森林。当前 Hypothesis 表 region-scoped
    # 无 task_id,本就全局共享;但 node_hypothesis 只做 V-22.13 cross-round
    # reuse(同 task),不 cross-task。G8 Phase A = prompt-level reference,
    # 不 hard reuse hypothesis_id 避免 V-27.45 terminal race 复杂度。
    # OFF 时 byte-for-byte legacy 渲染。soft-fail:fetch 异常 → 注入空 list
    # → prompt 不变。Phase C(7d+ obs)再考虑 hard reuse(parent_hypothesis_id
    # 跨 task chain)。
    # 双文件注册:本文件 + backend/services/feature_flag_service.py
    # (per [[feedback_enable_flag_double_file]])。
    ENABLE_HYPOTHESIS_FOREST_REUSE: bool = False
    # 过滤阈值:只把 ≥N pass + sharpe_avg ≥X 的 hypothesis 加进 prompt。
    # 默认 conservative 防早期 noise dominate(production 用 PROMOTED 状态
    # 已 implicit 满足这两个 ≥1 PASS 的 lifecycle 约束,但显式 gate
    # 保护未来 status 语义漂移)。
    HYPOTHESIS_FOREST_MIN_PASS_COUNT: int = 2
    HYPOTHESIS_FOREST_MIN_SHARPE_AVG: float = 1.0
    HYPOTHESIS_FOREST_TOP_K: int = 5

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

    # ----------------------------------------------------------------------
    # BRAIN Consultant Mode — effective_* synthesizers (P3-Brain)
    # ----------------------------------------------------------------------
    # 合成 ENABLE_BRAIN_CONSULTANT_MODE 与各 *_MIN/*_PERIOD/*_REGION 字段。
    # 只读 — caller 拿到的是 "当下" 值。Task 启动时把这些值冻结到
    # MiningTask.config["brain_role_snapshot"] 透传到 MiningState,后续
    # round 内读快照而非 settings(避免 running task 中途切换被新阈值重判)。
    #
    # IMPORTANT: 不要把任何 effective_* 名注册进 FeatureFlagService.SUPPORTED_FLAGS。
    # _env_default() 用 object.__getattribute__ 取 env 默认值,会触发 property
    # 描述符执行 — 对 effective_* 返回的是合成值而非 env default,语义不对。
    # effective_* 永远只读、永远是合成器,不是可翻的开关。
    @property
    def effective_default_test_period(self) -> str:
        """sim 默认 test_period — Consultant=P0Y / User=P2Y0M。"""
        if self.ENABLE_BRAIN_CONSULTANT_MODE:
            return self.CONSULTANT_DEFAULT_TEST_PERIOD
        return self.FULL_TEST_PERIOD                          # "P2Y0M" — line 598

    @property
    def effective_sharpe_submit_min(self) -> float:
        """提交 sharpe 门槛 — Consultant 取 max(SHARPE_MIN, 1.58)。"""
        if self.ENABLE_BRAIN_CONSULTANT_MODE:
            return max(self.SHARPE_MIN, self.CONSULTANT_SHARPE_SUBMIT_MIN)
        return self.SHARPE_MIN

    @property
    def effective_region_universes(self) -> dict:
        """sync_datasets 遍历的 (region, universe) 字典 — Consultant phase1 全球 5
        region,User 只 USA。"""
        if self.ENABLE_BRAIN_CONSULTANT_MODE:
            return dict(self.CONSULTANT_REGION_UNIVERSES)
        return {"USA": "TOP3000"}

    # ---- Regime staged-rollout derivations (post 2026-05-19 consolidation) ----
    # 3 legacy ENABLE_REGIME_* names kept as read-only properties so existing
    # callers (mining_agent / generation / evaluation / regime_infer) and
    # tests stay byte-for-byte. New single switch lives in ENABLE_REGIME +
    # REGIME_STAGE; stage progression: inference → thresholds → style.
    # The Settings.__getattribute__ hook is bypassed for these names because
    # they are not in SUPPORTED_FLAGS (no override row); fall-through hits
    # the @property descriptor below.
    @property
    def ENABLE_REGIME_INFERENCE(self) -> bool:
        return bool(self.ENABLE_REGIME) and self.REGIME_STAGE in (
            "inference", "thresholds", "style",
        )

    @property
    def ENABLE_REGIME_AWARE_THRESHOLDS(self) -> bool:
        return bool(self.ENABLE_REGIME) and self.REGIME_STAGE in (
            "thresholds", "style",
        )

    @property
    def ENABLE_STYLE_PRESET_GUIDANCE(self) -> bool:
        return bool(self.ENABLE_REGIME) and self.REGIME_STAGE == "style"

    class Config:
        case_sensitive = True
        env_file = ".env"
        extra = "ignore"

    # ----------------------------------------------------------------------
    # Runtime feature-flag override hook (P3 — ops dashboard, 2026-05-16)
    # ----------------------------------------------------------------------
    # Intercepts attribute reads ONLY for names beginning with "ENABLE_". For
    # those, if a runtime override exists in ``_flag_override_cache`` (set
    # by FeatureFlagService.set + the cross-process refresher), return the
    # override; otherwise fall through to the env/default value Pydantic
    # already loaded. Every other attribute access — including all of
    # Pydantic's own internal reads of ``__class__`` / ``__fields__`` /
    # ``model_config`` etc — bypasses the hook entirely.
    #
    # Why ``object.__getattribute__`` and not ``super()``: Pydantic's
    # BaseSettings has a custom ``__getattribute__`` that we don't want to
    # call recursively from inside ours. The ``object.__getattribute__``
    # call goes straight to CPython's resolution and avoids any chance of
    # re-entry into this hook.
    #
    # Performance: the prefix check is a single startswith call (~50 ns).
    # We do NOT do an isinstance check on `name` — Python guarantees attribute
    # names are str, and validating it costs more than just running startswith.
    def __getattribute__(self, name: str) -> Any:  # noqa: D401
        if name.startswith("ENABLE_"):
            # Single .get under GIL — avoid the `in`-then-`[]` race against
            # `_flag_override_cache.clear()` from load_overrides_into_cache.
            # The sentinel comparison also lets us honour an override value
            # of None (cleared override path stores nothing in cache).
            val = _flag_override_cache.get(name, _UNSET_FLAG)
            if val is not _UNSET_FLAG:
                return val
        return object.__getattribute__(self, name)


# Sentinel for __getattribute__ override-cache lookup. Module-level so the
# hot path doesn't allocate per call.
_UNSET_FLAG = object()

settings = Settings()
