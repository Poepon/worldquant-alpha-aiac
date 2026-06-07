"""
AIAC 2.0 Configuration
Centralized settings management using Pydantic
"""

import os
import json
import logging
from pydantic_settings import BaseSettings
from typing import Any, Optional, Dict, Tuple

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


def _load_llm_function_model_map() -> Dict[str, Dict[str, str]]:
    """Load the per-functional-block (node_key) → {model, provider, ...} routing map.

    Default mirrors the live all-kimi override (2026-06-05 redesigned per-node
    benchmark + hypothesis serial A/B; see docs/llm_per_node_benchmark_2026-06-05_
    full.json + docs/phase_c_hypothesis_ab_2026-06-05.json).
    This is only the STARTUP default — the ops console writes overrides into
    ``_flag_override_cache["LLM_FUNCTION_MODEL_MAP"]`` (a json feature-flag) which
    ``resolve_model_for`` consults FIRST. The whole map is consulted only when
    ENABLE_PER_FUNCTION_LLM_ROUTING is ON (default OFF → byte-for-byte legacy).

    Mirrors _load_thinking_overrides: module-level + fault-tolerant so a malformed
    LLM_FUNCTION_MODEL_MAP env never crashes Settings() construction. env keys
    merge OVER the defaults (partial-override).
    """
    # 2026-06-04 HARDENING: startup default now MIRRORS the live DB override
    # (LLM_FUNCTION_MODEL_MAP, ops_console 2026-06-04) — all nodes route to the
    # aliyun_coding_plan provider via provider_ref. Rationale: the token-plan
    # gateway (aliyun_maas / token-plan.cn-beijing.maas.aliyuncs.com) ran OUT OF
    # BUDGET, so production switched to aliyun_coding_plan (coding.dashscope). The
    # base "openai" provider resolves to api.openai.com with an EMPTY key (no
    # credential:openai_* / no .env OPENAI_*), i.e. a DEAD endpoint — so a
    # cache-cold window, a deleted override, or a flipped-OFF flag must NOT fall
    # back to it. Mirroring live here + the __default__ catch-all (captures
    # unmapped node_keys AND untagged node_key=None calls) + the routing flag
    # defaulting ON (below) keeps every fallback path on a LIVE provider.
    # NOTE: ALL nodes route to non-reasoning kimi-k2.5 (2026-06-05). The
    # redesigned per-node usability+cost benchmark + a hypothesis serial A/B
    # showed the reasoning model qwen3.6-plus costs 1.8–5.1× quota tokens
    # (reasoning_share 0.79–0.97) for USABILITY-IDENTICAL output (parse/schema_ok/
    # grounding/correct all 1.0) with no significant online edge — consistent with
    # reference_routing_reasoning_models_no_online_edge_2026_06_01. kimi-k2.5 is
    # the cheap, fast, online-validated workhorse, so it's also the __default__
    # catch-all (unmapped node_keys + untagged node_key=None calls). Secret keys
    # stay in CredentialsService (credential:llm_provider_aliyun_coding_plan).
    _CP = "aliyun_coding_plan"
    _K = "kimi-k2.5"
    defaults: Dict[str, Dict[str, str]] = {
        "hypothesis":          {"model": _K, "provider_ref": _CP},
        "code_gen":            {"model": _K, "provider_ref": _CP},
        "self_correct":        {"model": _K, "provider_ref": _CP},
        "r1b_retry":           {"model": _K, "provider_ref": _CP},
        "llm_mutate_alpha":    {"model": _K, "provider_ref": _CP},
        "llm_crossover_alpha": {"model": _K, "provider_ref": _CP},
        "r1b_mutate":          {"model": _K, "provider_ref": _CP},
        "r5_alignment_c1":     {"model": _K, "provider_ref": _CP},
        "r5_alignment_c2":     {"model": _K, "provider_ref": _CP},
        "attribution":         {"model": _K, "provider_ref": _CP},
        "distill_context":     {"model": _K, "provider_ref": _CP},
        "__default__":         {"model": _K, "provider_ref": _CP},
    }
    env_val = os.getenv("LLM_FUNCTION_MODEL_MAP")
    if not env_val:
        return defaults
    try:
        parsed = json.loads(env_val)
        if not isinstance(parsed, dict):
            raise ValueError("LLM_FUNCTION_MODEL_MAP must be a JSON object")
        return {**defaults, **parsed}
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        _config_logger.warning(
            "[config] LLM_FUNCTION_MODEL_MAP parse failed, using defaults | error=%s", e,
        )
        return defaults


# Per-provider supported-model catalog (2026-06-05). SINGLE SOURCE OF TRUTH for
# which models each Alibaba-Cloud plan actually serves — routing a node to a model
# NOT in its provider's list 401s/fails (the qwen3.6-flash-on-coding-plan incident,
# task 3981). Keep LLM_FUNCTION_MODEL_MAP entries within their provider's list.
# Test guard: test_llm_provider_catalog asserts the startup routing map conforms.
_PROVIDER_MODEL_CATALOG: Dict[str, list] = {
    # aliyun_coding_plan — coding.dashscope.aliyuncs.com — ACTIVE since 2026-06-04.
    # Authoritative: from the Coding Plan 订阅套餐 model page (2026-06-05).
    "aliyun_coding_plan": [
        "qwen3.6-plus", "qwen3.5-plus", "qwen3-max-2026-01-23",
        "qwen3-coder-next", "qwen3-coder-plus", "glm-5", "glm-4.7",
        "kimi-k2.5", "MiniMax-M2.5",
    ],
    # aliyun_maas — token-plan.cn-beijing.maas.aliyuncs.com — OUT OF BUDGET 2026-06-04.
    # From the Token Plan 订阅套餐 model page (2026-06-05), text-generation models
    # only. EXCLUDED: image-gen (qwen-image-2.0/-pro, wan2.7-image/-pro — not LLMs)
    # and deepseek-v3.2 (stale — superseded by deepseek-v4-pro/-flash).
    "aliyun_maas": [
        "qwen3.7-max", "qwen3.6-plus", "qwen3.6-flash",
        "deepseek-v4-pro", "deepseek-v4-flash",
        "kimi-k2.6", "kimi-k2.5", "glm-5.1", "glm-5", "MiniMax-M2.5",
    ],
}

# The currently-active provider (drives the ops-console model dropdown default).
_ACTIVE_LLM_PROVIDER = "aliyun_coding_plan"


def _load_llm_available_models() -> list:
    """Candidate model list for the ops-console dropdown (PR4). Defaults to the
    ACTIVE provider's catalog (_PROVIDER_MODEL_CATALOG). Override via
    LLM_AVAILABLE_MODELS env (JSON array). Fault-tolerant; never crashes."""
    defaults = list(_PROVIDER_MODEL_CATALOG.get(_ACTIVE_LLM_PROVIDER, []))
    env_val = os.getenv("LLM_AVAILABLE_MODELS")
    if not env_val:
        return defaults
    try:
        parsed = json.loads(env_val)
        return [str(m) for m in parsed] if isinstance(parsed, list) else defaults
    except (json.JSONDecodeError, ValueError, TypeError):
        return defaults


def _load_llm_providers() -> Dict[str, Dict[str, str]]:
    """Load the named LLM-provider registry (2026-06-04). A provider profile is a
    pre-configured ``endpoint + sdk`` entity that a routing entry references by
    name via ``provider_ref`` (see resolve_model_for). The provider's *secret*
    API key is NOT stored here — it lives encrypted in CredentialsService under
    the credential key ``llm_provider_<name>``, resolved at call time through the
    existing api_key_ref mechanism. Shape:

        {
          "moonshot": {"label": "Moonshot官方", "sdk": "openai",
                       "base_url": "https://api.moonshot.cn/v1"},
          "anthropic_official": {"label": "Anthropic", "sdk": "anthropic",
                                 "base_url": ""}
        }

    This is only the STARTUP seed; the ConfigCenter UI writes the LLM_PROVIDERS
    json feature-flag into _flag_override_cache (which wins). 2026-06-04 HARDENING:
    seed now MIRRORS the live override (3 providers) instead of empty, so a
    cache-cold window can still resolve the provider_ref entries in the
    LLM_FUNCTION_MODEL_MAP startup default (above) — otherwise an unresolved ref
    falls through to the DEAD base "openai" endpoint. Secret keys are NOT here —
    they live encrypted in CredentialsService under credential:llm_provider_<name>.
    Module-level + fault-tolerant, mirroring _load_llm_function_model_map."""
    defaults: Dict[str, Dict[str, str]] = {
        "aliyun_coding_plan": {"label": "阿里云CodingPlan", "sdk": "openai",
                               "base_url": "https://coding.dashscope.aliyuncs.com/v1"},
        "aliyun_maas": {"label": "阿里云TokenPlan", "sdk": "openai",
                        "base_url": "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"},
        "anthropic": {"label": "anthropic", "sdk": "anthropic",
                      "base_url": "https://ai.yaspost.com"},
    }
    env_val = os.getenv("LLM_PROVIDERS")
    if not env_val:
        return defaults
    try:
        parsed = json.loads(env_val)
        if not isinstance(parsed, dict):
            raise ValueError("LLM_PROVIDERS must be a JSON object")
        return {**defaults, **parsed}
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        _config_logger.warning(
            "[config] LLM_PROVIDERS parse failed, using defaults | error=%s", e,
        )
        return defaults


# Module-level caches — same Pydantic-bypass rationale as the thinking table.
_LLM_FUNCTION_MODEL_MAP_CACHE: Dict[str, Dict[str, str]] = _load_llm_function_model_map()
_LLM_AVAILABLE_MODELS_CACHE: list = _load_llm_available_models()
_LLM_PROVIDERS_CACHE: Dict[str, Dict[str, str]] = _load_llm_providers()


# Marginal-contribution scorecard calibration (consumed by
# backend/marginal_analysis.py via Settings.MARGINAL_* / marginal_scales()).
# Module-level so they're a single editable source and dodge Pydantic env-JSON
# validation. Defaults = USA can_submit calibration (2026-05-24: scale ≈ 2×
# median|Δ| over 20 live alphas). Δmargin / pnl_norm are intentionally absent
# (collinear with turnover / returns — see marginal_analysis docstring).
_MARGINAL_DIM_SCALES: Dict[str, float] = {
    "sharpe": 0.12,
    "returns": 0.004,
    "fitness": 0.08,            # NOT yet calibrated (Δ unsampled) — provisional
    "drawdown": 0.003,
    "turnover": 0.045,
    "recent_yearly_sharpe": 0.05,
}
_MARGINAL_DIM_WEIGHTS: Dict[str, float] = {
    "sharpe": 1.0,
    "returns": 0.8,
    "fitness": 0.5,
    "drawdown": 0.6,
    "turnover": 0.5,
    "recent_yearly_sharpe": 0.0,   # display + decay-guardrail only (collinear w/ sharpe)
}
# Per-region scale overrides, merged over the default (USA) set by
# Settings.marginal_scales(region). Empty until other regions are calibrated
# (the before/after portfolio is USA-dominated today). e.g. {"CHN": {"turnover": 0.06}}
_MARGINAL_SCALE_OVERRIDES: Dict[str, Dict[str, float]] = {}


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
    # User-Agent sent on every Anthropic request. Defaults to the Claude CLI
    # signature so traffic is presented as `claude-cli`. Override via env when
    # a different identity is needed; set empty to keep the SDK's default UA.
    ANTHROPIC_USER_AGENT: str = os.getenv(
        "ANTHROPIC_USER_AGENT", "claude-cli/2.1.150 (external, sdk-cli)"
    )
    # `x-app` header sent on every Anthropic request — the Claude CLI sends
    # "cli". Pure identity marker; the standard Anthropic API ignores it.
    # Empty → header omitted.
    ANTHROPIC_X_APP: str = os.getenv("ANTHROPIC_X_APP", "cli")
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

    # Per-functional-block LLM model routing (PR1, 2026-05-29). ENABLE_ prefix
    # so the __getattribute__ hook honours a runtime override (the map body
    # itself is NOT read via settings.X — resolve_model_for reads
    # _flag_override_cache directly, since non-ENABLE_ names bypass the hook).
    # Default OFF (byte-for-byte legacy). The live DB override has been ON since
    # 2026-05-31; the cross-process refresher (feature_flag_runtime) WARMS the
    # cache synchronously at FastAPI lifespan / Celery worker_process_init BEFORE
    # the first task runs, so production sees the DB flag ON with no cache-cold
    # window — defaulting this ON would add ~zero prod safety yet make every test
    # that constructs a real LLMService route to a live endpoint. The coding-plan
    # startup map + provider seed below ARE hardened (so an override-deleted /
    # flag-on-cache-warm path still degrades to a live endpoint, never the dead
    # base "openai"). A brand-new env with NO DB override needs a one-time ops flip.
    ENABLE_PER_FUNCTION_LLM_ROUTING: bool = (
        os.getenv("ENABLE_PER_FUNCTION_LLM_ROUTING", "false").strip().lower()
        in ("1", "true", "yes")
    )

    # Startup defaults for the routing map + dropdown roster (module-level,
    # Pydantic-bypassed). Runtime overrides live in _flag_override_cache via the
    # LLM_FUNCTION_MODEL_MAP / LLM_AVAILABLE_MODELS json feature-flags.
    @property
    def LLM_FUNCTION_MODEL_MAP(self) -> Dict[str, Dict[str, str]]:
        return _LLM_FUNCTION_MODEL_MAP_CACHE

    @property
    def LLM_AVAILABLE_MODELS(self) -> list:
        return _LLM_AVAILABLE_MODELS_CACHE

    # Named LLM-provider registry (2026-06-04). name → {label, sdk, base_url}.
    # Secret keys live in CredentialsService (llm_provider_<name>), NOT here.
    # Runtime overrides live in _flag_override_cache via the LLM_PROVIDERS json
    # feature-flag — resolve_model_for reads that cache directly (non-ENABLE_
    # name bypasses the __getattribute__ hook, same as LLM_FUNCTION_MODEL_MAP).
    @property
    def LLM_PROVIDERS(self) -> Dict[str, Dict[str, str]]:
        return _LLM_PROVIDERS_CACHE

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

    # Cross-process BRAIN re-auth coalescing (方向1, 2026-05-20). The single
    # shared BRAIN session (Redis key brain_session:cookies) is used by 3 solo
    # Celery workers + uvicorn; without a fleet-wide lock each process re-auths
    # independently on a 401, and BRAIN's single-active-session invalidates the
    # others' cookie → mutual-invalidation thrash → repeated 300s circuit trips.
    # _distributed_reauth wraps authenticate() in a Redis lock so only one
    # process re-auths per token-expiry window; the rest wait + reload.
    # LOCK_TTL must exceed a normal authenticate() round-trip (a healthy auth is
    # <5s; the retry-storm path is covered by the circuit breaker, not this).
    BRAIN_REAUTH_LOCK_TTL_SEC: int = 90
    BRAIN_REAUTH_WAIT_TIMEOUT_SEC: float = 60.0
    BRAIN_REAUTH_POLL_INTERVAL_SEC: float = 1.5

    # ===== Sim-poll liveness / Zombie-simulation reclaim (2026-05-24) =====
    # brain_adapter._wait_for_simulation / _wait_for_multisim poll BRAIN with
    # the Retry-After protocol. A "zombie" sim keeps returning HTTP 200 + a
    # valid Retry-After header indefinitely (observed: task 3329 RUNNING ~11h
    # with 0 alphas, rooted in a stale/thrashing BRAIN session). The poll
    # loop's max_wait was a DEAD parameter — never compared against elapsed —
    # so a zombie polled forever. These caps enforce it: on exceeding the cap
    # the adapter re-auths once + rechecks (Zombie Protocol), then abandons the
    # handle with retryable=True so node_simulate holds the alpha at PENDING
    # and the next round re-tries (vs. polling a dead handle forever).
    # Thresholds are deliberately GENEROUS — well above any healthy sim
    # duration (a P0Y full-history single sim, or an N-child multi-sim) — so a
    # trigger almost certainly means a genuine zombie, not a slow-but-live sim.
    BRAIN_SIM_MAX_WAIT_SEC: int = 1800        # single REGULAR sim poll ceiling (30 min)
    BRAIN_MULTISIM_MAX_WAIT_SEC: int = 3600   # multi-sim (N children) poll ceiling (60 min)

    # ===== Phase 4 Sprint 0 (2026-05-19) =====
    # Plan: docs/phase4_a_b_plan_v5_2026-05-19.md
    # ----- PR0 LLM_API_CIRCUIT (Sprint 0) -----
    # 防 DeepSeek/Anthropic outage silent burn — 复用 backend/circuit_breaker.py
    # framework + N-consecutive-fail trip pattern。Default ON(防御机制 default ON
    # 与 BRAIN_AUTH_CIRCUIT 一致)。Soft-fail Redis blip 永不 brown-out。
    # 双文件注册:本文件 + backend/services/feature_flag_service.py。
    ENABLE_LLM_API_CIRCUIT: bool = True
    LLM_API_CIRCUIT_FAIL_THRESHOLD: int = 5    # 60s 内连续 N 次 5xx/timeout 跳闸
    LLM_API_CIRCUIT_FAIL_WINDOW_SEC: int = 60  # 失败计数器 TTL
    LLM_API_CIRCUIT_COOLDOWN_SEC: int = 300    # 跳闸冷却(同 BRAIN_AUTH_CIRCUIT)

    # ----- Hard deadlines on external calls (2026-05-21) -----
    # 根因:LLM HTTP 调用无显式 timeout → 死 socket 让 asyncio loop 永久 park 在
    # select(py-spy 实证),Windows --pool=solo 单线程被独占 → worker 永久僵死。
    # wait_for + client timeout 双层兜底;round-level deadline 兜任何非 LLM 的
    # 未约束 await。Windows solo 上 Celery task_time_limit 不生效,这是唯一可靠机制。
    LLM_CALL_TIMEOUT_SEC: float = 180.0     # 单次 LLM HTTP 调用硬上限(非流式)
    LLM_STREAM_TIMEOUT_SEC: float = 600.0   # anthropic thinking 流式调用硬上限
    MINING_ROUND_TIMEOUT_SEC: int = 1200    # per-round 兜底(20min;健康 round 5-13min)
    # 2026-05-25: 单轮 round 失败(尤其 wait_for 超时 cancel 在 asyncpg DB IO 中途
    # 触发 greenlet_spawn,毒化共享 AsyncSession)后,_run_flat_iteration 重建一个
    # 干净 session 隔离毒化、继续后续轮,而不是让整个 FLAT session 暴毙(task 3504
    # 实证:it5 跑满 1200s 超时 → 共享 session 毒化 → 整个 task FAILED、cursor 之后
    # 全丢)。连续失败达到此上限则优雅退出(cursor 已保存,可 resume),避免无限重建。
    FLAT_MAX_CONSECUTIVE_ROUND_FAILURES: int = 3

    # ----- Sprint 0 spike calibration (2026-05-19) -----
    # Production baseline (last 30d, scripts/sprint0_baseline_spike.py):
    #   - finalized_n=8,658  pass_n=131  author_pass_rate_30d=0.0151 (1.51%)
    #   - hypothesis_round_stats 28 rounds: p5=0.0000 / p10=0.0000 / p50=0.0000
    # → Half of all rounds yield ZERO PASS, so an EMA-style PASS_RATE_FLOOR
    #   at 5% would auto-pause virtually every task. The dominant R14 trigger
    #   in production will be CONSECUTIVE_FAIL_ROUNDS (3 consecutive
    #   zero-PASS rounds), not EMA floor. Keep floor very low so EMA only
    #   fires on truly degenerate distributions, not normal noise.
    # R12 GO gate (Sprint 末): assistant_pass_rate >= 0.0151 * 0.90 = 0.01359
    #   with bootstrap 80% CI not crossing 0. Given the ~131 PASS over 30d
    #   spread across author/assistant, sample size is the binding constraint
    #   — expect the GO gate to need 30d *minimum* and likely 45-60d to
    #   accumulate enough samples for a tight CI.

    # ----- PR0.5 ENABLE_R8_L0 sub-flag (Sprint 0,Phase 4 R12 sentinel 前置) -----
    # 默认 True(R8 hierarchical RAG 已 LIVE,L0 是 4 层之一)。R12 sentinel ON
    # 时全局 set False,跳过 L0(exact pattern_hash match)进 L1 pillar/L2 family/L3 field。
    # 双 entry skip:`backend/agents/hierarchical_rag.py:query_hierarchical` +
    # `backend/agents/services/rag_service.py:query()` legacy entry。
    # 双文件注册:本文件 + feature_flag_service.py SUPPORTED_FLAGS。
    ENABLE_R8_L0: bool = True

    # ===== Phase 4 Sprint 1 (2026-05-19+) =====
    # ----- A2 R14 task_stop_loss -----
    # Millennium 5%/7.5% hard stop-loss 工业模式 — task 累计 PASS rate 低于
    # EMA floor OR 连续 N round 0 PASS → auto-pause task。
    # Spike-calibrated (2026-05-19, scripts/sprint0_baseline_spike.py):
    # production p50 round PASS rate = 0% (28 rounds, hypothesis_round_stats
    # 30d window)。半数 round 是 0 PASS,EMA floor 设到 0.005 (0.5%) 才不会
    # false-trigger;主 trigger 走 CONSECUTIVE_FAIL_ROUNDS=3。
    # Race fix (Round S0-A finding):flat loop 已在 CB-skip 时 `continue`,
    # 自动满足 EXCLUDE_CB_SKIPPED;flag 保留作 defense-in-depth(若未来其他
    # caller 路径不 continue,service 仍可 skip 计数器)。
    # 双文件注册:本文件 + backend/services/feature_flag_service.py。
    # plan: docs/phase4_a_b_plan_v5_2026-05-19.md §6.2
    ENABLE_TASK_STOP_LOSS: bool = False
    TASK_STOP_LOSS_EMA_ALPHA: float = 0.3
    TASK_STOP_LOSS_MIN_ROUNDS: int = 5         # warmup — 前 N round 不 trigger
    TASK_STOP_LOSS_PASS_RATE_FLOOR: float = 0.005    # Spike-calibrated 0.5% (production p50=0)
    TASK_STOP_LOSS_CONSECUTIVE_FAIL_ROUNDS: int = 3  # 主 trigger
    TASK_STOP_LOSS_EXCLUDE_CB_SKIPPED: bool = True   # race fix (defense-in-depth)

    # ----- A3 flat-F4 cross-region quota (Sprint 1) -----
    # Millennium 320 pods / Citadel 5 业务线 multi-strategy 启示 — AIAC 当前
    # region 严重偏 USA(production 数据印证)。flat-F4 在 POST 时校验新 task
    # 加入后的 region 分布是否越过 FLAT_CROSS_REGION_QUOTA。
    # ENFORCE=True → POST 拒绝越界(400);ENFORCE=False(default)→ 仅 warn log。
    # Phase A 真效果(per [[feedback_按效果选择]]):default ENFORCE=False 配合
    # warn 阶段先观察 7d,然后翻 ENFORCE=True 真改 mining 决策。
    # 双文件注册:本文件 + backend/services/feature_flag_service.py。
    # plan: docs/phase4_a_b_plan_v5_2026-05-19.md §6.3
    FLAT_CROSS_REGION_QUOTA: dict = {
        "USA": 0.30,
        "CHN": 0.20,
        "JPN": 0.15,
        "EUR": 0.20,
        "HKG": 0.15,
    }
    FLAT_CROSS_REGION_ENFORCE: bool = False
    FLAT_CROSS_REGION_LOOKBACK_DAYS: int = 30  # last-N-days window for share computation

    # ----- A1.1 R12 LLM_MODE=assistant — service + state machine (Sprint 1) -----
    # Critical path 工业派共识吸收 — "LLM 是 research assistant 不是 expression-
    # author"(Citadel / Two Sigma / Bridgewater AIA)。A1 拆 4 sub-PR:
    #   A1.1 (this PR)  — service + state machine + drain residue keys
    #   A1.2            — sentinel guard 联动 6 LIVE flag + audit + restore
    #   A1.3            — code_gen branching + assistant template library
    #   A1.4            — ops endpoint + bootstrap CI GO gate
    # Default OFF — task.config["llm_mode"]='assistant' opt-in 灰度。
    # NOT yet in SUPPORTED_FLAGS (A1.2 will register + 联动)。
    # plan: docs/phase4_a_b_plan_v5_2026-05-19.md §6.1 (v3.0/v5.0 critical path)
    ENABLE_LLM_ASSISTANT_MODE: bool = False
    # Sentinel flags A1.2 will force OFF when ENABLE_LLM_ASSISTANT_MODE=True.
    # Declared here so the llm_mode_service can reason about expected
    # cross-flag state without circular import on feature_flag_service.
    LLM_ASSISTANT_SENTINEL_FLAGS: list = [
        "ENABLE_R1B_HYPOTHESIS_MUTATE",
        "ENABLE_G5_CROSSOVER",
        "ENABLE_HYPOTHESIS_FOREST_REUSE",
        "ENABLE_R8_L0",
        "ENABLE_AST_ORIGINALITY_GATE",
        "ENABLE_SIMULATION_CACHE",
    ]
    # F13 review fix (Sprint 4 R3): Sprint-4 flag sentinel-membership
    # rationale (the next reviewer will otherwise see the asymmetry vs
    # G8/AST-gate as an oversight):
    #   - ENABLE_G10_LOGIC_INJECT: NOT a sentinel. It's pure prompt
    #     information (distilled logic text); the LLM still authors the
    #     hypothesis in assistant mode — same class as macro/style blocks
    #     which are also not sentinel-listed. Assistant mode benefits from
    #     the prior, so leave it ON.
    #   - ENABLE_GRAMMAR_VALIDATOR: NOT a sentinel. It validates DSL
    #     syntax regardless of who authored it (template library in
    #     assistant mode OR LLM in author mode); a malformed expression is
    #     malformed either way. Leaving it ON in assistant mode is correct
    #     (catches broken template composition too). NB: distinct from
    #     ENABLE_AST_ORIGINALITY_GATE (sentinel) which gates *originality*
    #     — an author-mode-specific concern G3-v2 deliberately does NOT
    #     duplicate.
    # Task.config keys drained when LLM mode flips (per Round S0-A F-A5):
    # 4 cross-round / inject staging keys that would carry author-mode
    # state into an assistant-mode round (or vice versa) and produce
    # silent zombie consumption. Kept declarative so future audits don't
    # have to hunt them down in service code.
    # NOT drained: brain_role_snapshot (Consultant-mode survives mode flip),
    # stop_loss_state (R14 cross-round accumulator), flat_cursor (lives
    # on run.runtime_state not task.config).
    LLM_ASSISTANT_RESIDUE_KEYS: list = [
        "g5_pending_offspring",
        "__pending_hypothesis",
        "__g5_consumed_offspring",
        "__r1b_consumed_pending_hypothesis",
        "contextual_bandit_v1",  # mining_agent._BANDIT_CONFIG_KEY value
    ]

    # ----- B1 R11 alpha_capacity_estimator (Sprint 2, 2026-05-20) -----
    # 工业派 capacity-cap 思维(RenTec Medallion $10B cap / Bridgewater AIA
    # $5B 软上限)— 高 sharpe 低 capacity 的 alpha 应降权。把单 alpha 估算的
    # USD capacity 作为 composite_score 第 5 维(原 4 维:sharpe / fitness /
    # turnover / robustness),log-scale 5 桶 normalize 到 [0,1]。
    # Phase A 真效果(per [[feedback_按效果选择]]):flag ON 直接改 composite
    # ranking,不是 stamp-only。Default OFF 保 byte-identical regression;
    # ENABLE_CAPACITY_SCORE=True 时 composite normalize sum=1.0(原 4 维 weight
    # × 0.9 + capacity × 0.10 = 1.0,见 alpha_scoring.evaluate_alpha_comprehensive)。
    # Estimator 公式:capacity_usd = ADV(region+universe) × universe_size ×
    #   (1 - turnover_decay_factor) — 粗估,精度优先于完美。
    # 双文件注册:本文件 + backend/services/feature_flag_service.py。
    # plan: docs/phase4_a_b_plan_v5_2026-05-19.md §6.8 / v2 §4.5
    ENABLE_CAPACITY_SCORE: bool = False
    CAPACITY_SCORE_WEIGHT: float = 0.10
    # Log-scale buckets — alpha capacity USD 落入哪桶决定 normalize 后的值
    # [0.0, 0.25, 0.50, 0.75, 1.0]。<$1M → 0.0,>$10B → 1.0。
    CAPACITY_LOG_BUCKETS: list = [1e6, 1e7, 1e8, 1e9, 1e10]

    # ----- B3 R10-v2 family hard-ban shadow (Sprint 2, 2026-05-20) -----
    # 工业派 Citadel / Bridgewater 内部 portfolio-construction 经验:同
    # family 的 alpha 即使 sharpe 都达 PASS 阈值,若 PnL 时间序列高度相关
    # (pairwise corr ≥ τ),纳入组合后边际贡献接近 0 → hard-ban 低分者。
    # R10-v2 是 R10 family-cap(纯 structural top-K)的 fine-grain 补充。
    # Shadow mode:family_classifier.apply_family_hard_ban 不直接 set FAIL,
    # 只 stamp `metrics["_r10v2_hard_banned"]=True`;evaluation 末统一
    # finalize 段 scan stamp → FAIL,允许 R10/R10-v2 双 stamp 共存以便
    # plan v5 §6.10 互验 SQL 计算 false-positive rate。
    # Default OFF + production wire pending τ 校准 — operator 先跑
    # scripts/calibrate_r10_pairwise_corr.py 出 region-specific τ,再
    # flip ENABLE_FAMILY_HARD_BAN + 上游 state.r10v2_pnl_corr_matrix 填充
    # 路径接通(fast-follow)。
    # 双文件注册:本文件 + backend/services/feature_flag_service.py。
    # plan: docs/phase4_a_b_plan_v5_2026-05-19.md §6.10
    ENABLE_FAMILY_HARD_BAN: bool = False
    # τ ∈ [0, 1]。default 0.65 = 保守初值;R10-calib spike 输出 p95-p99 中位
    # 会校准到 region-specific(USA 通常较高,emerging market 较低)。
    FAMILY_BAN_MIN_PAIRWISE_CORR: float = 0.65

    # ----- B2 R13 factor_decomposition shadow (Sprint 2, 2026-05-20) -----
    # Two Sigma 18-factor lens / AQR autoencoder asset pricing intuition —
    # AIAC evaluation 只看 sharpe/fitness/turnover/self-corr,无 style factor
    # neutralization。R13 把 alpha 的 daily returns 对 5 个 style factor
    # (size/value/momentum/quality/low_vol) OLS 分解,产 residual_sharpe +
    # factor_exposures + r_squared。
    # 三阶段 rollout(per [[feedback_light_wiring_deferred_gate]]):
    #   1. shadow(default):default OFF;flip ON → log + stamp,无 quality
    #      _status 改动
    #   2. soft:7d obs ≥30 alpha residual → flip MODE='soft',
    #      residual<τ → quality_status='PASS_PROVISIONAL'
    #   3. hard:再 7d obs PASS_PROVISIONAL 中 ≥80% can_submit=True →
    #      flip MODE='hard',residual<τ → quality_status='FAIL'
    # Path:R13-spike GO OLS 路径(backend/services/factor_lens_service.py)。
    # 数据依赖:backend/data/factor_returns_snapshot/{region}.parquet
    # (operator manual maintenance,每月 refresh)。stale >90d → /ops/r13/
    # snapshot-stale-check 告警(fast-follow)。
    # 双文件注册:本文件 + backend/services/feature_flag_service.py。
    # plan: docs/phase4_a_b_plan_v5_2026-05-19.md §6.9 / v2 §4.6
    ENABLE_FACTOR_LENS: bool = False
    FACTOR_LENS_MODE: str = "shadow"  # "shadow" | "soft" | "hard"
    FACTOR_LENS_FACTORS: list = ["size", "value", "momentum", "quality", "low_vol"]
    FACTOR_LENS_RESIDUAL_SHARPE_MIN: float = 0.5  # hard 模式 < τ → FAIL
    FACTOR_LENS_OLS_LOOKBACK_DAYS: int = 504  # ~2y daily
    FACTOR_LENS_MIN_OVERLAP_DAYS: int = 60  # < N 天交集 → 跳过 decompose

    # ----- B5 R8-v3 cognitive layer 7-layer (Sprint 3, 2026-05-20) -----
    # 7 research-lens prompts (macro/behavioral/technical/value/microstructure/
    # cross_sectional/time_series_mean_reversion) — 每 round 选 1 个 splice
    # 进 hypothesis prompt,nudge LLM 朝该 lens 思考。3 个 select 策略:
    #   - bandit:Beta-Bernoulli Thompson sample,exploit > 0.5 优势 layer
    #   - round_robin:固定顺序轮转
    #   - deficit_aware:挑 PASS rate 最低的 boost coverage
    # Token budget guard:hypothesis prompt 总 token ≤ 8k,超出时按
    # _DROP_ORDER 删 dedup_blacklist → cross_task_forest → macro_narrative
    # (cognitive_layer 块绝不删,这是 R8-v3 整个目的)。
    # 双文件注册:本文件 + backend/services/feature_flag_service.py。
    # plan: docs/phase4_a_b_plan_v5_2026-05-19.md §6.11 / v4 §6.11
    ENABLE_COGNITIVE_LAYER_PROMPT: bool = False
    COGNITIVE_LAYER_SELECT_MODE: str = "round_robin"  # bandit | round_robin | deficit_aware
    COGNITIVE_LAYER_PROMPT_TOKEN_BUDGET: int = 8000
    # Tier E E1: trailing window the weekly bandit-reward cron aggregates
    # _cognitive_layer_used PASS/FAIL over (cumulative upsert).
    COGNITIVE_LAYER_BANDIT_WINDOW_DAYS: int = 7

    # ----- A5.1 G10 logic-as-asset (Sprint 3, 2026-05-20) -----
    # RD-Agent NeurIPS 2025 "logic-as-asset" + Citadel internal "research
    # diary" — past 7d PASS alpha 周末 LLM 蒸馏成 1-3 句话的 logic 总结,
    # 写 distilled_logic_library 表。PR2 (Sprint 4) 注入回 hypothesis
    # prompt 形成正反馈;PR1 (本次) 只蒸馏 + 建库。
    # Cost guard:LOGIC_DISTILL_MAX_COST_USD_PER_WEEK $5 上限,LLM call
    # cost 累计超过即停;Top-K alpha per (pillar, region) bucket 限制
    # prompt 大小;< min_pass_count alpha 的 bucket skip。
    # Schedule:Sunday 03:00 Asia/Shanghai (off-peak,与其他 weekly cron
    # 错开)。Alembic n5e6f7g8h9i0_distilled_logic.
    # 双文件注册:本文件 + backend/services/feature_flag_service.py。
    # plan: docs/phase4_a_b_plan_v5_2026-05-19.md §6.12 / v4 §6.12
    ENABLE_G10_LOGIC_DISTILL: bool = False
    LOGIC_DISTILL_MAX_COST_USD_PER_WEEK: float = 5.00
    LOGIC_DISTILL_TOP_K_PER_GROUP: int = 10
    LOGIC_DISTILL_MIN_PASS_COUNT: int = 3
    LOGIC_DISTILL_LOOKBACK_DAYS: int = 7
    LOGIC_DISTILL_SIMILARITY_THRESHOLD: float = 0.70

    # ----- A5.2 G10 PR2 (Sprint 4, 2026-05-20) - prompt injection -----
    # PR1 (Sprint 3) 建库;PR2 注入回 hypothesis prompt 形成正反馈。
    # ENABLE_G10_LOGIC_INJECT 独立 flag(可单独启用 inject 不启用 distill,
    # 比如已有库时只 inject 不重蒸馏)。
    # G10_LOGIC_INJECT_TOP_K 注入到 prompt 的 entry 数。
    # plan: docs/phase4_a_b_plan_v5_2026-05-19.md §6.13
    ENABLE_G10_LOGIC_INJECT: bool = False
    G10_LOGIC_INJECT_TOP_K: int = 5

    # ----- B4.1 G3-v2 grammar-aware validator (Sprint 4, 2026-05-20) -----
    # lark-based parser for a subset of BRAIN DSL — catches structurally
    # malformed alphas (unbalanced parens / unexpected tokens) BEFORE the
    # LLM-generated text reaches G3 originality or BRAIN simulator. New
    # path; G3 shadow code (alpha_originality.py) stays unchanged behind
    # ENABLE_AST_ORIGINALITY_GATE @deprecated_pending_r12_decision —
    # B4.2 in Sprint 5 conditionally retires per R12 decision.
    # When validate() returns ok=False, retry_with_whole_output_hint
    # gives a terse parse error → node_code_gen re-emits.
    # 双文件注册:本文件 + backend/services/feature_flag_service.py。
    # plan: docs/phase4_a_b_plan_v5_2026-05-19.md §6.14
    ENABLE_GRAMMAR_VALIDATOR: bool = False
    # F4 review fix (Sprint 4 R3): RESERVED — not yet wired. node_code_gen
    # currently BUFFERS parse-fail candidates + degrades-open above a 50%
    # drop floor; it does NOT re-emit via LLM. A future PR may wire
    # retry_with_whole_output_hint into a bounded re-emit loop reading this
    # setting. Until then this is a no-op knob (kept so the future wire
    # doesn't need a config migration).
    GRAMMAR_VALIDATOR_RETRY_MAX: int = 2  # RESERVED — see comment above

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
    # 前置:ENABLE_FLAT_CONTINUOUS ON + flat-F1 2 周灰度 PASS。决策 5A lock。
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
    # 2026-05-21: L1 candidate pool size for relevance-first selection. The
    # over-fetched pool is scored by dataset-category overlap + quality, then
    # truncated to the layer budget. Kill-switch: set == budget to degenerate
    # back to recency-only behavior with no code change.
    RAG_HIER_L1_CANDIDATE_CAP: int = 40
    # 2026-05-21: RAG category-overlap A/B experiment harness (see
    # feature_flag_service ENABLE_RAG_CATEGORY_AB). OFF = no A/B, category
    # overlap always on (current P0 behavior). Registered in SUPPORTED_FLAGS.
    ENABLE_RAG_CATEGORY_AB: bool = False

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
    # Pipeline-only (2026-05-28 — task 3735 amplification): a HARD per-session cap
    # on R1b mutate regenerations in the sim-pipeline. The DB depth cap
    # (R1B_MAX_MUTATION_DEPTH) is SKIPPED when the failed alpha has no
    # current_hypothesis_id (parent=None) — common on fresh FLAT alphas — and the
    # classifier's statement-dedupe can't catch ever-changing LLM statements, so
    # without this cap many distinct failing hypotheses each spawn a FULL (~95s
    # LLM) regeneration with no overall bound. This guarantees the mutate feedback
    # loop terminates regardless of depth-chaining. 0 disables.
    R1B_PIPELINE_MAX_MUTATIONS: int = 20
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
    # Mining orchestrator config retired in Phase 1c-delete follow-up
    # (tasks/orchestrator.py removed; resident pool needs no auto-launch).
    # ----- R1b outcome reconciliation (Break 2 fix, 2026-05-22) -----
    # Max pending r1b_retry_log rows reconcile_r1b_outcomes processes per run.
    R1B_RECONCILE_MAX_ROWS: int = 1000
    # ----- Self-healing data-field prune (2026-05-22) -----
    DATAFIELD_PRUNE_WINDOW_DAYS: int = 14
    DATAFIELD_PRUNE_MAX_PER_RUN: int = 500
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
    # The names without `_DELAY{0,1}` suffix are the **delay-1** values
    # (historical default; legacy callers that don't yet pass delay land here).
    # `_DELAY0` companions are the delay-0 band — BRAIN's actual submission
    # gates are stricter on delay-0 (alpha 15621 empirical, 2026-05-28:
    # LOW_SHARPE limit=2.0 / LOW_FITNESS=1.3 / LOW_SUB_UNIVERSE_SHARPE=0.81
    # vs the looser delay-1 gates). Use `Settings.eval_thresholds(delay)`
    # below to pick the right band — never read these constants directly
    # in code that knows the alpha's delay.
    EVAL_SHARPE_MIN: float = 1.5
    EVAL_FITNESS_MIN: float = 1.2
    EVAL_TURNOVER_MIN: float = 0.01
    EVAL_TURNOVER_MAX: float = 0.4
    EVAL_SUBUNIV_MIN: float = 0.2
    EVAL_SELF_CORR_MAX: float = 0.7

    # PROVISIONAL = "近 pass 中间档"(2026-06-02 实证回退,见 commit 注释)。
    # 旧 T1 PROVISIONAL (5-15→5-19 active) 是 0.8/0.6/0.85/0.0;`8e92905`
    # tier 退役 big-bang 把这 4 个值全设成"旧某 tier PASS"等价 (1.25/1.0/
    # 0.55/0.15),导致 prov_*_min 跟 PASS sharpe_min/fitness_min 完全一样,
    # `evaluation.py:near_pass` 路径死路,产能从 89% 崩到 1%。回退到旧 T1
    # PROVISIONAL 等价值 — 用户当时只选 PASS strictest band,没说 PROV 要跟齐。
    EVAL_PROVISIONAL_SHARPE_MIN: float = 0.8     # was 1.25 (8e92905 oversight)
    EVAL_PROVISIONAL_FITNESS_MIN: float = 0.6    # was 1.0
    EVAL_PROVISIONAL_TURNOVER_MAX: float = 0.85  # was 0.55
    EVAL_PROVISIONAL_SUBUNIV_MIN: float = 0.0    # was 0.15

    # Delay-0 PASS band — matches BRAIN's stricter delay-0 checks (15621
    # empirical). Set above the BRAIN limits so a locally-PASS alpha actually
    # clears BRAIN's gate. turnover band is the same as delay-1 (BRAIN's
    # turnover gate is delay-agnostic per the 15621 response).
    EVAL_SHARPE_MIN_DELAY0: float = 2.0          # BRAIN LOW_SHARPE limit=2.0
    EVAL_FITNESS_MIN_DELAY0: float = 1.3         # BRAIN LOW_FITNESS limit=1.3
    EVAL_TURNOVER_MIN_DELAY0: float = 0.01
    EVAL_TURNOVER_MAX_DELAY0: float = 0.7        # BRAIN HIGH_TURNOVER limit=0.7
    EVAL_SUBUNIV_MIN_DELAY0: float = 0.81        # BRAIN LOW_SUB_UNIVERSE_SHARPE=0.81
    EVAL_SELF_CORR_MAX_DELAY0: float = 0.7       # self-corr is content-based, not delay-based

    # Delay-0 PROVISIONAL — sits just below the hard PASS band (same 0.25 sharpe
    # gap as the delay-1 band: 2.0→1.75).
    EVAL_PROVISIONAL_SHARPE_MIN_DELAY0: float = 1.75
    EVAL_PROVISIONAL_FITNESS_MIN_DELAY0: float = 1.15
    EVAL_PROVISIONAL_TURNOVER_MAX_DELAY0: float = 0.7
    EVAL_PROVISIONAL_SUBUNIV_MIN_DELAY0: float = 0.7

    EVAL_SCORE_PASS: float = 0.8
    EVAL_SCORE_OPTIMIZE: float = 0.3

    # ── Optimization closure (Stage A — 2026-05-28)
    # Plan: docs/optimization_closure_plan_v1_2026-05-28.md §6. ALL gated by
    # ENABLE_OPTIMIZATION_LOOP — flag default OFF; flipping ON wires the 6h
    # beat task and starts spending OPT_DAILY_SIM_BUDGET sims/day on Stage A
    # (settings_sweep) variant generation. NEVER auto-submits — Stage A
    # SubmitPolicy returns "queue" for every winner (manual review via
    # ops/submit-backlog). Stage B is the auto-submit upgrade and is gated
    # on Stage A's 14-day GO/STOP conversion-rate decision.
    ENABLE_OPTIMIZATION_LOOP: bool = False
    OPT_BEAT_INTERVAL_HOURS: int = 6        # 4 cycles/day @ 02/08/14/20 SH
    OPT_CANDIDATES_PER_CYCLE: int = 10      # near-gate alphas picked per beat
    OPT_DAILY_SIM_BUDGET: int = 400         # 10 × 10 × 4 = 400 sim/day; <= 40% of BRAIN cap
    OPT_NEAR_GATE_BAND: float = 0.5         # sharpe distance from hard_gate to qualify
    OPT_SIM_TIMEOUT_SECONDS: int = 600      # per-sim hard timeout (BRAIN p95 ≈ 90-120s)
    # Manual blueprint-optimization (2026-06-03): user-triggered single-alpha
    # cycle via POST /alphas/{id}/optimize → trigger_source="manual". Runs
    # INDEPENDENTLY of ENABLE_OPTIMIZATION_LOOP (that flag only gates the 6h
    # beat). Default budget covers the full ~10-variant SettingsSweepGenerator
    # grid; a caller override is clamped to [1, MAX].
    OPT_MANUAL_SIM_BUDGET: int = 16         # default sims per manual cycle
    OPT_MANUAL_SIM_BUDGET_MAX: int = 30     # clamp ceiling for caller override
    OPT_MANUAL_INFLIGHT_MINUTES: int = 40   # per-alpha concurrency-guard window
    # Robustness止血 (2026-06-03 methodology review / industry survey L3): deflate
    # sweep winners against multiple-testing (SR0 expected-max-Sharpe) + lone-peak
    # overfitting (plateau gate) BEFORE persisting. A settings sweep crowning the
    # best of N variants is textbook backtest overfitting; this rejects winners
    # that don't beat the luckiest-of-N-noise expectation or sit on a lone spike.
    OPT_ROBUSTNESS_FILTER: bool = True      # apply RobustnessFilter post-WinnerSelector
    OPT_PLATEAU_BAND: float = 0.15          # same-neut sibling must reach sharpe_min - this

    # ── Auto-submit (2026-06-04) — automate the orthogonal backlog drain.
    # System is execution-limited (67+ clean alphas, ~12 ever submitted). This 6h
    # beat picks the top of the GET /ops/submit-backlog/drain-order sequence and
    # submits via AlphaService.submit_alpha — which KEEPS all its irreversible
    # gates (can_submit / live self_corr<0.7 / Redis lock / re-check). On top, the
    # beat applies a full FAIL-CLOSED guard stack (auto_submit_selector).
    # SHADOW-FIRST: ENABLE_AUTO_SUBMIT default OFF; even when ON, AUTO_SUBMIT_MODE
    # defaults to 'shadow' = log a would-submit list WITHOUT submitting. Flip to
    # 'live' only after the shadow list is human-verified for N days. ENABLE_
    # prefix → flag is runtime-overridable (config.py __getattribute__ hook).
    ENABLE_AUTO_SUBMIT: bool = False              # master kill-switch (default OFF)
    AUTO_SUBMIT_MODE: str = "shadow"              # off | shadow | live
    AUTO_SUBMIT_DAILY_CAP: int = 4                # AIAC-side per-UTC-day live submit cap
    AUTO_SUBMIT_PER_RUN_CAP: int = 2              # max live submits per beat firing
    AUTO_SUBMIT_MARGIN_BPS_MIN: float = 5.0       # economic gate (bps); <0 hard SKIP
    AUTO_SUBMIT_CANSUBMIT_MAX_AGE_H: int = 12     # can_submit freshness window (live mode)
    AUTO_SUBMIT_REQUIRE_FRESH_CANSUBMIT: bool = True  # live: stale/unknown freshness → skip
    AUTO_SUBMIT_REGIONS: str = "USA"              # CSV; drained one region at a time
    AUTO_SUBMIT_REQUIRE_RECON_VERDICT: str = "supported"  # Stage1 only 'supported'; 'weak' widens
    AUTO_SUBMIT_CORR_THRESHOLD: float = 0.7       # self/among-set corr ceiling (= MAX_CORRELATION)
    AUTO_SUBMIT_BEAT_INTERVAL_HOURS: int = 6

    # ── can_submit periodic refresh (2026-06-04) — keeps the can_submit verdict
    # (and its _brain_can_submit_at freshness stamp, read by auto-submit G4) ≤
    # CAN_SUBMIT_REFRESH window by re-checking the can_submit=True / unsubmitted
    # backlog against BRAIN, stalest-first. Also demotes alphas BRAIN now rejects
    # (e.g. self_corr crept ≥0.7 vs the growing submitted pool) out of the backlog.
    # Read-only BRAIN GETs (no submission), paced 1 req/s. Default OFF.
    ENABLE_CAN_SUBMIT_REFRESH: bool = False        # master switch (default OFF)
    CAN_SUBMIT_REFRESH_MAX_PER_RUN: int = 200      # cap BRAIN GETs per firing (stalest-first)

    # ── Marginal-contribution submit recommendation (backend/marginal_analysis.py)
    # Calibration for the multi-dimensional before-and-after scorecard. Scalar
    # tunables here; per-dim scales/weights are dict properties below (loaded as
    # module-level constants to dodge Pydantic env-JSON validation, same pattern
    # as THINKING_EFFORT_OVERRIDES). Defaults = USA can_submit calibration
    # (2026-05-24, scale ≈ 2 × median|Δ|). Recalibrate via
    # `python scripts/iqc_marginal_audit.py --calibrate` and put per-region scale
    # overrides in _MARGINAL_SCALE_OVERRIDES.
    MARGINAL_NORM_CAP: float = 1.5          # clip |normalized| to ±this
    MARGINAL_NOISE_FLOOR: float = 0.15      # |normalized| <= this → abstains
    MARGINAL_T_SUBMIT: float = 0.25         # composite >= this → SUBMIT
    MARGINAL_T_SKIP: float = -0.25          # composite <= this → SKIP
    MARGINAL_GR_RISK: float = -1.2          # drawdown/turnover norm this bad → cap NEUTRAL
    MARGINAL_GR_RETURN: float = -1.2        # returns norm this bad → cap NEUTRAL
    MARGINAL_GR_YEARLY: float = -1.0        # recent-year sharpe norm this bad → cap NEUTRAL
    MARGINAL_MARGIN_FLOOR: float = 0.0005   # alpha margin (ratio) < this → cap NEUTRAL; <0 → SKIP

    @property
    def MARGINAL_DIM_SCALES(self) -> Dict[str, float]:
        return _MARGINAL_DIM_SCALES

    @property
    def MARGINAL_DIM_WEIGHTS(self) -> Dict[str, float]:
        return _MARGINAL_DIM_WEIGHTS

    def marginal_scales(self, region: Optional[str] = None) -> Dict[str, float]:
        """Per-dim scales for `region`, region overrides merged over the default
        (USA-calibrated) set. Unknown region → defaults."""
        scales = dict(_MARGINAL_DIM_SCALES)
        if region:
            scales.update(_MARGINAL_SCALE_OVERRIDES.get(region, {}))
        return scales

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
    # call BRAIN /{scope}/alphas/{id}/before-and-after-performance and stash the
    # deltas in alpha.metrics._iqc_marginal. The marginal signal is the
    # standalone-vs-merged stats deltas (sharpe/fitness/turnover/...) PLUS the
    # competition `score` delta. NOTE (2026-05-24): BRAIN had dropped `score`
    # while IQC2026S1 was down; it RETURNED with the IQC2026S2 season (verified
    # live 2026-05-26 under competitions/IQC2026S2). score.before/after is the
    # team leaderboard rank score, so delta_score is an active, NON-collinear
    # signal again — an alpha can lift Δsharpe yet drop Δscore (observed live).
    #
    # Scope (2026-05-26): IQC2026S2 is the live competition. The auto-audit runs
    # against competitions/IQC2026S2 so `score` is populated (the team / users
    # scope omits it). Use `iqc_audit_scope()` — the single source of truth — to
    # resolve the active (competition, team_id): competition wins when non-empty,
    # else the team scope; both empty disables the audit entirely.
    IQC_AUTO_AUDIT_COMPETITION: str = "IQC2026S2"  # live competition → populates `score`
    IQC_AUTO_AUDIT_TEAM: str = "deLkl06"           # fallback scope (no `score`)

    def iqc_audit_scope(self) -> Tuple[Optional[str], Optional[str]]:
        """Resolve the active IQC marginal-audit scope as (competition, team_id).

        Competition wins when set (a live competition still ranks teams by the
        merged score); otherwise the team scope is used. Both empty → the
        auto-audit is disabled and callers treat (None, None) as "skip". Keeps
        the BRAIN before-and-after scope in one place so callers stop hard-coding
        the deleted IQC2026S1 competition.
        """
        comp = (self.IQC_AUTO_AUDIT_COMPETITION or "").strip()
        if comp:
            return comp, None
        team = (self.IQC_AUTO_AUDIT_TEAM or "").strip()
        if team:
            return None, team
        return None, None

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
    # Orthogonality-steered exploration Phase A (2026-06-05, plan
    # docs/orthogonality_steered_exploration_plan_2026-06-05.md): inject the
    # SUBMITTED-pool pillar-coverage profile as a SOFT NUDGE into the hypothesis
    # prompt so the LLM explores mechanisms orthogonal to what's already submitted
    # (negative-knowledge steering). OFF → backend.submitted_pool_profile is never
    # called + PromptContext.submitted_pool_profile stays None → byte-for-byte
    # legacy prompt. ENABLE_ prefix → hot-flippable. Shadow-first, then A/B.
    ENABLE_ORTHOGONAL_PROMPT_STEERING: bool = False
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

    # Soft regularizer (P1, 2026-05-23): AlphaAgent-style soft penalty over
    # code-gen candidates instead of a hard field/operator cap. Two legs in
    # P1 — complexity + originality (alignment=R5 reserved for P2, w=0).
    # Acts at the pre_simulate_filter call site (evaluation.py). Mode mirrors
    # QLIB_PRESCREEN_MODE / FACTOR_LENS_MODE:
    #   "off"    → block inert (no compute, byte-for-byte legacy)
    #   "shadow" → compute + stamp alpha.metrics['_soft_reg_*'] only (DEFAULT)
    #   "soft"   → also down-weight pre-sim P(PASS): p*(1-lambda*penalty)
    # No "hard" mode — by design we never reject on complexity (per decision
    # 2026-05-23: dropped the hard 3/8 cap in favour of soft regularization).
    CODE_GEN_SOFT_REG_MODE: str = "shadow"  # off | shadow | soft
    CODE_GEN_SOFT_REG_LAMBDA: float = 0.5   # max P(PASS) fraction a fully-penalized cand loses
    CODE_GEN_SOFT_REG_W_COMPLEXITY: float = 0.5
    CODE_GEN_SOFT_REG_W_ORIGINALITY: float = 0.5
    CODE_GEN_SOFT_REG_W_ALIGNMENT: float = 0.0   # reserved for P2 (R5 c1/c2)
    # Complexity ramp: complexity_score = n_operators + 0.5*n_fields.
    # 0 penalty at/below C0 (free), linear up to 1 at CMAX (saturates beyond).
    CODE_GEN_SOFT_REG_COMPLEXITY_C0: float = 6.0
    CODE_GEN_SOFT_REG_COMPLEXITY_CMAX: float = 16.0
    # P2 alignment leg (R5 c1/c2). Master switch is W_ALIGNMENT > 0 (default 0
    # = leg dormant, zero LLM cost). R5 = 2 LLM calls/candidate, so only the
    # most-promising candidates (ranked by the cheap complexity+originality
    # effective P(PASS)) are judged: TOPK in 'soft' mode, SHADOW_SAMPLE in
    # 'shadow' (small, just to accrue the alignment distribution for
    # calibration). 0 disables R5 entirely in that mode.
    CODE_GEN_SOFT_REG_ALIGNMENT_TOPK: int = 3
    CODE_GEN_SOFT_REG_ALIGNMENT_SHADOW_SAMPLE: int = 1

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

    # --- Dataset-steering value bandit v1 (2026-05-22, breadth direction) ---
    # plan dataset_steering_bandit_plan_v3 Tier A. Turns the dormant
    # DatasetMetadata.mining_weight into a discounted Beta-Bernoulli posterior
    # over per-dataset book-marginal yield, then weight-samples the FLAT
    # mining loop. OFF (default) → byte-for-byte legacy (job no-ops, FLAT
    # round-robins). Registered in feature_flag_service.SUPPORTED_FLAGS so it
    # can be flipped from /ops without redeploy.
    ENABLE_DATASET_VALUE_BANDIT: bool = False
    DATASET_BANDIT_GAMMA: float = 0.95          # pull-indexed discount per real sim
    # v6 grid-tuned (2026-05-23): low floor so the rare can_submit signal (pv1
    # 10.6%) isn't drowned by exploration; proven submitters stay above the weak
    # 0-submit datasets. fc=0.03 keeps weak (~0.06) below pv1 (0.108).
    DATASET_BANDIT_FLOOR_C: float = 0.03        # anti-starvation floor amplitude
    DATASET_BANDIT_FLOOR_TAU: float = 100.0     # floor decay constant (cumulative sims)
    DATASET_BANDIT_WINDOW_DAYS: int = 7         # watermark fallback window on first run
    # v6: pessimistic cold-start prior β for zero-history catalog datasets seeded
    # on a re-seed run. mean=1/(1+β); β=5 → 0.167 so an untested source (e.g.
    # pv96 — burned quota once on a stale field) explores via the floor but
    # doesn't outrank the proven analyst4/news12 band.
    DATASET_BANDIT_COLDSTART_BETA: float = 5.0
    
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
    # Per-round generation batch — how many candidates one CODE_GEN call
    # produces (and the round then simulates). Option A (2026-05-27): bumped
    # 4→10. Timing analysis showed SIMULATE is 63% of round wall-time while the
    # ~175s LLM gen chain runs with 0 sim slots busy; a single gen call
    # amortizes that fixed cost over more sims, lifting USER-3-slot utilization
    # ~63%→~84% (~1.5-1.8x throughput) at near-zero risk. Used as the fallback
    # when a task has no explicit daily_goal. See
    # docs/delay0_sim_pipeline_design_2026-05-27.md.
    ALPHAS_PER_ROUND: int = 10
    # code_gen asks the LLM for ALPHAS_PER_ROUND alpha objects in ONE JSON
    # response; each carries a 5-slot reasoning chain + explanation (~400-500
    # output tokens). The LLM call's default max_tokens is 4096 — fine at the
    # old batch of 4 (~2k tokens) but a batch of 10 (~5k) overruns it, the JSON
    # is truncated, json.loads fails, and the round yields ZERO candidates. So
    # code_gen now sizes max_tokens to the batch: max(4096, BASE + PER_ALPHA*n),
    # capped at CEILING. CEILING must stay ≤ the deployed model's max output
    # (deepseek-chat = 8192). See node_code_gen.
    CODE_GEN_MAX_TOKENS_PER_ALPHA: int = 512
    CODE_GEN_MAX_TOKENS_CEILING: int = 8000
    # node_hypothesis completion budget. Default was the call()'s 4096, which
    # TRUNCATED verbose models (deepseek-v4-pro ~6000 completion under the rich
    # production prompt) → incomplete JSON → 0 hypotheses → 0 alphas (2026-05-31
    # global-routing rollout). Bumped to 6000 so per-function routing can use the
    # benchmark's hypothesis pick (deepseek-v4-pro, pillar_div 0.80 leading)
    # without truncation. Concise models (kimi-k2.6 ~2800) are unaffected.
    HYPOTHESIS_MAX_TOKENS: int = 6000

    # --- Mining pipeline (producer-consumer) ---
    # Decouples LLM generation from BRAIN simulation so sim slots stay
    # saturated. The pipeline is the SOLE FLAT path since the serial loop was
    # retired (2026-05-29); the old ENABLE_SIM_PIPELINE toggle is gone.
    # See docs/sim_pipeline_impl_plan_2026-05-27.md.
    # work_queue capacity (producer backpressure). 0 = auto = 2× sim-slot limit.
    SIM_PIPELINE_QUEUE_MAXSIZE: int = 0
    # number of producer coroutines. 1 at USER 3 slots (gen >> sim consumption);
    # scaled up only at CONSULTANT 80 slots where generation becomes the
    # bottleneck (Sub-phase 2).
    SIM_PIPELINE_PRODUCER_COUNT: int = 1
    # persister drains every N completed sim results (1 = persist each
    # immediately; higher = micro-batch). Keep small to bound crash-loss of
    # already-simulated-but-unpersisted results (wasted BRAIN quota).
    SIM_PIPELINE_PERSIST_EVERY: int = 1
    # Drain-and-refresh the shared BRAIN httpx client every N completed sims
    # (0 = disabled). Combats the long-session client-rot sim-hang (d650222);
    # the refresh only runs with zero sims in flight. ~32 ≈ a few rounds.
    SIM_PIPELINE_CLIENT_REFRESH_EVERY: int = 32
    # Diversity steering (option C / MAP-Elites coverage axis = dataset): the
    # pipeline producer generates THIS many candidates per dataset visit and
    # picks the LEAST-covered dataset each visit, so one session spreads across
    # many distinct datasets (breadth = data sources, per competitive-analysis
    # v3) instead of concentrating ~ALPHAS_PER_ROUND on the first dataset. Kept
    # SMALL: unlike the legacy serial loop (where ALPHAS_PER_ROUND=10 amortizes
    # the LLM gen over more sims — Option A), the pipeline overlaps gen with sim,
    # so a small per-dataset batch + continuous generation still saturates the
    # slots while covering ~target_candidates/this_value distinct datasets.
    SIM_PIPELINE_DATASET_BATCH: int = 4
    # Option C step-2 — economic-quality dataset steering (ε-greedy). With this
    # probability the producer EXPLORES (picks the least-covered dataset, C-step1
    # breadth); otherwise it EXPLOITS (picks the dataset with the highest mean
    # alpha margin so far this session — economic value, denser/less-noisy than
    # PASS-rate, anchored to the real ~5bps cost-positive floor, not fit to
    # history). Kept explore-heavy (default 0.6) so breadth is preserved while
    # budget tilts toward cost-positive datasets. Cold start (no margins yet) →
    # always explore.
    SIM_PIPELINE_EXPLORE_PROB: float = 0.6
    # Per-OPERATION hard deadline for the pipeline (2026-05-27 — task 3735 hang).
    # The legacy round path wraps each round in wait_for(MINING_ROUND_TIMEOUT_SEC);
    # the pipeline runs ONE long session, so instead every network await (a
    # producer gen round / a feedback handler / a consumer sim / a consumer
    # evaluate-with-self_corr) is individually bounded. A hung socket then fails
    # that one operation cleanly (logged + counted) instead of parking the
    # asyncio loop in select forever. 0 disables (tests).
    # INVARIANT (2026-05-28 — task 3736): this MUST stay comfortably below the
    # watchdog dead-threshold (CASCADE_WATCHDOG_DEAD_MIN min). The watchdog's
    # liveness signal is the latest trace_step; when BRAIN stalls every sim, no
    # trace is written until each hung op times out and flushes a failure-trace.
    # If op_timeout ≈ the watchdog threshold (was 1200s vs 1500s — 5min margin),
    # trace can go stale past the threshold and the watchdog spuriously revives a
    # still-live session (→ duplicate runs). _pipeline_op_timeout() also hard-caps
    # it below the watchdog window. 600s gives a 15min margin under the 25min default.
    SIM_PIPELINE_OP_TIMEOUT_SEC: int = 600
    # Sub-phase 3 — the producer is split at HYPOTHESIS into two stages joined by
    # an internal hyp_q (stage-1 hyp-producer rag→distill→hypothesis owns the DB
    # session; stage-2 code-producers code_gen→validate→[self_correct] are
    # DB-free). The seam makes the hypothesis SOURCE pluggable (e.g. paper-derived
    # hypotheses pushed onto the same hyp_q). This is the ONLY generation path
    # (the prior single-stage producer was removed 2026-05-28).
    # Number of stage-2 code-producers draining hyp_q (DB-free, like sim
    # consumers). 1 keeps the seam with no extra concurrency; >1 parallelises
    # code_gen (only useful once generation is the bottleneck — i.e. CONSULTANT
    # 80-slot; at USER the sim wall dominates and 1 suffices).
    SIM_PIPELINE_CODE_PRODUCER_COUNT: int = 1
    # Session-level liveness watchdog for the pipeline (2026-06-03 redesign,
    # docs/heartbeat_liveness_redesign_2026-06-03.md). REPLACES the old
    # progress-signal heartbeat: instead of "no alpha produced for N seconds"
    # (which mis-fired on legitimate 0-output rounds — task 3930, a producer
    # busy with LLM retries got killed), the watchdog now tracks PER-COROUTINE
    # liveness — each monitored coroutine stamps a timestamp every time it
    # RETURNS from a `_with_timeout` await (= it yielded and re-entered = alive).
    # A coroutine PARKED on a bare await (single-conn cleanup hang under NullPool
    # / queue deadlock) stops stamping → stale → abort. IDLE (blocked on an
    # empty/full queue) is a SEPARATE exempt state. The effective abort window is
    # derived as op_timeout + LIVENESS_GRACE (see _pipeline_heartbeat_timeout),
    # so a coroutine still inside a bounded op never trips it; only a bare-await
    # park does. Hard-capped < watchdog CASCADE_WATCHDOG_DEAD_MIN*60. Set 0 to
    # disable. This base value is the floor; the derived window is max(base, op+grace).
    SIM_PIPELINE_HEARTBEAT_TIMEOUT_SEC: int = 900
    # L1_DEAD = op_timeout + this grace. A coroutine inside a bounded op (≤600s)
    # plus grace must NOT trip the liveness watchdog; only a never-returning bare
    # await does. 120s grace covers op-boundary scheduling jitter.
    SIM_PIPELINE_LIVENESS_GRACE_SEC: int = 120

    # Optimization Chain Settings
    # MAX_OPTIMIZATION_VARIANTS caps the SettingsSweepGenerator grid (2026-06-04:
    # wired via build_optimization_service → generator truncates _GRID[:N] before
    # dedup). Default 10 = the full hand-picked grid; the grid's 10 cells are the
    # hard ceiling (raise past 10 only by adding _GRID rows). Lower it for cheaper
    # sweeps (fewer BRAIN sims/cycle).
    MAX_OPTIMIZATION_VARIANTS: int = 10
    MAX_SETTINGS_VARIANTS: int = 5           # UNUSED (no wiring) — reserved/legacy
    OPTIMIZATION_BUDGET_PER_ALPHA: int = 20  # Max simulations per optimization target
    
    # Field Screening Settings
    FIELD_SCREENING_ENABLED: bool = True
    FIELD_SCREENING_TOP_K: int = 20
    FIELD_SCREENING_TEMPLATES: int = 4  # Number of templates to test per field
    
    # Rate Limiting
    # MAX_SIMULATIONS_PER_DAY removed in Phase 1c-delete follow-up (0 readers;
    # BRAIN_DAILY_SIMULATE_LIMIT + the budget:sims counter are the live caps).
    MAX_TOKENS_PER_DAY: int = 500000

    # ----- Pool pipeline budgets (four-pool decoupling) — Phase 0 calibration.
    # INERT in Phase 0 (nothing reads these yet); consumed by the HG/S/E pools'
    # three-segment token reserve/correct gate in Phase 1b. Kept SEPARATE from
    # MAX_TOKENS_PER_DAY (macro_narrative_extract.py:101 still owns that — do NOT
    # repurpose it). Full standing of the numbers:
    # docs/pool_token_calibration_2026-06-05.md (measured from llm_call_log,
    # 11,301 calls / 17d). See plan §2 (cost single-source).
    #
    # Provisional daily LLM-token ceiling for the pools (~8x current ~1M/day
    # burn) as a runaway backstop; recalibrate in Phase 1b against observed
    # continuous burn. Enforced as a Redis budget:tokens:YYYYMMDD counter that
    # SETs the pool drain key when exceeded (NOT a DB pause).
    POOL_TOKEN_BUDGET_PER_DAY: int = 8_000_000
    # p95/p99-per-node pessimistic pre-reserve for the token gate. Heavy nodes
    # (code_gen, hypothesis) saturate near their ~17k output cap → reserve at
    # p99; light nodes round up p99. Feedback-cluster nodes (r1b_*/r5_*/
    # attribution/llm_crossover) are EXCLUDED — removed in Phase 1c (pure-forward
    # Phase 1). __default__ catches any unmapped LLM node.
    POOL_NODE_TOKEN_RESERVE: dict = {
        "code_gen": 17000,        # p95 12568 / p99 16921 — saturates output cap
        "hypothesis": 14000,      # p95 9836 / p99 13780 (7d p99 ~17.2k)
        "distill_context": 4500,  # p95 3262 / p99 4513
        "self_correct": 5100,     # p95 3765 / p99 5034
        "__default__": 5000,      # any unmapped LLM node
    }
    # Master gate for the resident HG/S/E pool pipeline (four-pool decoupling).
    # Default OFF — the pool beats no-op and the supervisor starts no workers
    # until this flips (Phase 1c-flip). NOT hot-flippable from the ops console: it
    # is intentionally NOT registered in SUPPORTED_FLAGS, and the standalone
    # supervisor / run_worker processes read it straight off Pydantic env at start
    # with no FeatureFlagOverride cache-refresher — so the ONLY valid flip is an
    # `.env` edit + full `run.bat --restart` (every process re-reads env). See the
    # plan §4 1c-flip 3-step manual runbook. (Registering it in SUPPORTED_FLAGS to
    # regain a console hot-OFF kill-switch is a deferred P2 — would also need the
    # supervisor to warm the override cache.)
    ENABLE_POOL_PIPELINE: bool = False
    # How many hyp_intent rows the pool scheduler beat inserts per firing.
    POOL_SCHEDULER_BATCH: int = 5
    # Pool sizing (§6 #6 decision): USER=3 BRAIN slots → 2 S workers + 1 E + leave
    # 1 slot for opt/auto-submit; 1 HG worker (LLM-bound, fans out N/intent). The
    # supervisor launches these counts. CONSULTANT(80) upgrade scales via formula.
    POOL_K_S: int = 2          # S (simulate) workers — hold BRAIN slots
    POOL_K_E: int = 1          # E (evaluate) workers — compute-bound
    POOL_N_HG: int = 1         # HG (hypothesis+generation) workers — LLM-bound
    # Claim/lease tunables.
    POOL_LEASE_MAX_ATTEMPTS: int = 3       # attempts before poison-pill
    POOL_SUPERVISOR_POLL_SEC: float = 5.0  # supervisor liveness poll interval
    # min gap before a role is respawned again — PER-ROLE batch, not per-worker:
    # if several workers of one role die together, the whole role waits one backoff
    # before respawning (a correlated-death gap, bounded + self-healing).
    POOL_RESPAWN_BACKOFF_SEC: float = 10.0
    # Lease-recycle reclaims at most this many expired in-flight rows per beat
    # (oldest-expired first) — bounds the FOR UPDATE set so one beat can't lock a
    # pathological backlog in one txn. The live in-flight set is ~worker-count, so
    # 200 is generous headroom; a larger backlog drains over successive beats.
    POOL_RECYCLE_BATCH: int = 200

    # ----- Pool Phase 2 (1c) — cognitive reconcile beat -----
    # The async cognitive engine for the pool: a beat that scans recently-landed
    # alphas (watermark on alphas.created_at + grace) and drives the hypotheses
    # lifecycle (auto-activate PROPOSED→ACTIVE, refresh can_submit_count /
    # submitted_count, PROMOTE on can_submit_count>0 — NOT pass_count, which would
    # promote on PASS_PROVISIONAL — and a cheap heuristic attribution stamp). This
    # replaces FLAT's synchronous in-graph CoSTEER. Default OFF → the beat no-ops
    # (returns skipped). Registered in SUPPORTED_FLAGS → hot-flippable (the celery
    # beat + the pool worker both warm the override cache). See plan §7 Track C.
    ENABLE_POOL_COGNITIVE_RECONCILE: bool = False
    # Grace period (seconds) subtracted from the scan upper-bound so a just-landed
    # alpha whose can_submit is still being refreshed (the 30s post-sim countdown)
    # is NOT read as can_submit=NULL → CENSORED before its label lands. ≥2× the 30s
    # refresh countdown (plan §7 R3 (i) / guard #15). Tune from observed p95
    # label-write latency once 1d telemetry accrues.
    POOL_RECONCILE_GRACE_SEC: int = 60
    # Fallback scan window (days) used on the FIRST reconcile run (no watermark
    # yet) — bounds the initial backfill so a cold start doesn't scan all history.
    POOL_RECONCILE_WINDOW_DAYS: int = 7

    # ----- Pool Phase 2 (R1a-v1) — skeleton-frequency soft de-prioritization -----
    # A generation-side prior: mine recent SUCCESS_PATTERN skeletons, build a
    # frequency histogram, and inject a SOFT nudge into the hypothesis prompt that
    # de-prioritizes the most-crowded structural skeletons (steer breadth without a
    # hard forbidden list). Re-anchored at the live build_hypothesis_prompt
    # injection point — NOT the dead FLAT recent_dedup_skeletons. Sample-size-gated
    # + [:5] cap + field-aware. Default OFF → byte-for-byte legacy prompt. Do NOT
    # promote past OFF until it has soaked AND the already-live pillar nudge's own
    # A/B reports (plan §7 Track B / guards #12, #14). Registered in SUPPORTED_FLAGS.
    ENABLE_R1A_KB_SKELETON_FREQUENCY: bool = False
    # Lookback window (days) for the SUCCESS_PATTERN frequency histogram.
    SKELETON_FREQUENCY_WINDOW_DAYS: int = 30
    # Minimum #SUCCESS_PATTERN rows in-window before the nudge renders anything
    # (below this the histogram is noise → render ""). Plan §7 Track B / guard #15.
    SKELETON_FREQUENCY_MIN_SAMPLES: int = 3

    # ----- Field hygiene (#25c, 2026-06-07) — only offer the code-gen LLM NUMERIC
    # SIGNAL fields -----
    # ROOT CAUSE of the 2026-05-20 submit-yield collapse: the pool field path
    # (_get_dataset_fields) handed the LLM the FULL active roster including
    # NON-SIGNAL metadata — UTC timestamps (*_time_utc), dates (*_date_utc),
    # ISO/entity codes (*iso_code*), universe-membership flags (field_type
    # UNIVERSE: top500/top200), symbols (SYMBOL). The LLM dutifully built
    # degenerate expressions on them (ts_zscore(entity_country_iso_code_4),
    # subtract(top500,top500)=0) → 0/negative sharpe. These are never alpha
    # signals. Default ON (correctness fix); flip OFF for the legacy roster.
    # Verified high-precision over the live catalog: 19/8365 fields excluded,
    # 0 false positives. Registered in SUPPORTED_FLAGS for a hot kill-switch.
    ENABLE_FIELD_HYGIENE: bool = True
    FIELD_HYGIENE_EXCLUDE_TYPES: list = ["UNIVERSE", "SYMBOL"]
    # case-insensitive substrings in field_id marking non-signal metadata.
    FIELD_HYGIENE_EXCLUDE_ID_SUBSTRINGS: list = ["_time_utc", "_date_utc", "iso_code"]

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
    # F2-4 pipeline-only: hard cap on total G5 crossovers per pipeline session.
    # A crossover offspring that PASSes can itself trigger another crossover with
    # the growing PASS pool; at a low PASS rate this converges, but the cap
    # guarantees the feedback loop terminates (quiescence) regardless of rate.
    # Pair-dedup (each parent pair crossed once) bounds it further.
    G5_PIPELINE_MAX_CROSSOVERS: int = 20
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

    # ----------------------------------------------------------------------
    # Persistence-Ontology refactor (2026-05-19) — plan
    # ~/.claude/plans/alpha-persistence-ontology-refactor-2026-05-19.md v1.3.1
    # ----------------------------------------------------------------------
    # P1: 把 BRAIN 接受过的 FAIL alpha (alpha_id 存在 + 真 sim 成功) 写 alphas
    # 表;OFF 时回到 PASS-only legacy 行为。Mining-time write filter 修复:
    # alpha_failures.QUALITY_CHECK_FAILED 不再丢 BRAIN handle。
    # 双文件注册: 本文件 + backend/services/feature_flag_service.py per
    # [[feedback_enable_flag_double_file]]
    ENABLE_FAIL_ALPHA_PERSIST: bool = False

    # P4: R1b mutate prompt v2 — parent context 富化为 failure-metrics-with-
    # diagnosis;tri-state 因 shadow-mode A/B 需求 [V1.1-S2]:
    #   'off'    — byte-equivalent legacy prompt(default)
    #   'shadow' — 生成 OLD+NEW 两份 prompt,把 NEW 写 llm_call_log 但只发
    #              OLD 给 LLM(零行为变化,纯 cost/parse-failure 对比)
    #   'active' — 只生成 NEW prompt 发给 LLM
    ENABLE_R1B_MUTATE_PROMPT_V2: str = "off"

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
        """提交 sharpe 门槛 — Consultant 取 max(SHARPE_MIN, 1.58)。

        **DEPRECATED in delay-aware code**: this property assumes delay=1.
        Call sites that know the alpha's delay should use
        ``effective_sharpe_submit_min_for(delay)`` instead. Kept as a
        delay-1 default for legacy callers that don't yet thread delay.
        """
        if self.ENABLE_BRAIN_CONSULTANT_MODE:
            return max(self.SHARPE_MIN, self.CONSULTANT_SHARPE_SUBMIT_MIN)
        return self.SHARPE_MIN

    def effective_sharpe_submit_min_for(self, delay: int) -> float:
        """Delay-aware BRAIN submit-gate sharpe minimum (2026-05-28 fix).

        delay=0 → 2.0 (BRAIN's stricter delay-0 gate, 15621 empirical);
        delay=1 → SHARPE_MIN (1.5 default). Consultant mode bumps either
        branch via max(., CONSULTANT_SHARPE_SUBMIT_MIN).
        """
        base = self.EVAL_SHARPE_MIN_DELAY0 if int(delay) == 0 else self.SHARPE_MIN
        if self.ENABLE_BRAIN_CONSULTANT_MODE:
            return max(base, self.CONSULTANT_SHARPE_SUBMIT_MIN)
        return base

    def eval_thresholds(self, delay: int = 1) -> dict:
        """Delay-aware PASS / PROVISIONAL band (2026-05-28 fix for delay-0).

        Single source of truth for the local-PASS evaluation band. delay-0
        returns the strict BRAIN-aligned band (sharpe>=2.0/fit>=1.3/sub-univ
        >=0.81, matching the actual BRAIN checks observed on alpha 15621);
        delay-1 returns the legacy band (1.5/1.2/0.2).

        Returns the same keys ``_eval_thresholds()`` in evaluation.py used to
        return, plus the nested ``provisional`` dict. Callers should pass
        ``state.delay`` (mining) or ``alpha.delay`` (sync/refresh).
        """
        d = 0 if int(delay) == 0 else 1
        if d == 0:
            return {
                "sharpe_min": self.EVAL_SHARPE_MIN_DELAY0,
                "fitness_min": self.EVAL_FITNESS_MIN_DELAY0,
                "turnover_min": self.EVAL_TURNOVER_MIN_DELAY0,
                "turnover_max": self.EVAL_TURNOVER_MAX_DELAY0,
                "subuniv_min": self.EVAL_SUBUNIV_MIN_DELAY0,
                "self_corr_max": self.EVAL_SELF_CORR_MAX_DELAY0,
                "check_self_corr": True,
                "check_concentrated": True,
                "score_pass": self.EVAL_SCORE_PASS,
                "score_optimize": self.EVAL_SCORE_OPTIMIZE,
                "provisional": {
                    "sharpe_min": self.EVAL_PROVISIONAL_SHARPE_MIN_DELAY0,
                    "fitness_min": self.EVAL_PROVISIONAL_FITNESS_MIN_DELAY0,
                    "turnover_max": self.EVAL_PROVISIONAL_TURNOVER_MAX_DELAY0,
                    "subuniv_min": self.EVAL_PROVISIONAL_SUBUNIV_MIN_DELAY0,
                },
            }
        return {
            "sharpe_min": self.EVAL_SHARPE_MIN,
            "fitness_min": self.EVAL_FITNESS_MIN,
            "turnover_min": self.EVAL_TURNOVER_MIN,
            "turnover_max": self.EVAL_TURNOVER_MAX,
            "subuniv_min": self.EVAL_SUBUNIV_MIN,
            "self_corr_max": self.EVAL_SELF_CORR_MAX,
            "check_self_corr": True,
            "check_concentrated": True,
            "score_pass": self.EVAL_SCORE_PASS,
            "score_optimize": self.EVAL_SCORE_OPTIMIZE,
            "provisional": {
                "sharpe_min": self.EVAL_PROVISIONAL_SHARPE_MIN,
                "fitness_min": self.EVAL_PROVISIONAL_FITNESS_MIN,
                "turnover_max": self.EVAL_PROVISIONAL_TURNOVER_MAX,
                "subuniv_min": self.EVAL_PROVISIONAL_SUBUNIV_MIN,
            },
        }

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
